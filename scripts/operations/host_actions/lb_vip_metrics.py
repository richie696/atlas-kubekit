# SPDX-License-Identifier: MIT
"""Install and observe the fixed, loopback-only LB VIP telemetry service."""
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import subprocess

from ..collector import identity_facts
from ..models import OperationError
from . import common, stage_assets


SCRIPT = Path("/usr/local/libexec/atlas-kubekit/lb-vip-metrics.py")
ENV = Path("/etc/default/atlas-lb-vip-metrics")
UNIT = Path("/etc/systemd/system/atlas-lb-vip-metrics.service")
MARKER = "# Managed by Atlas KubeKit"
LEGACY_UNIT = ("[Unit]\nDescription=Atlas Keepalived VIP metrics\n"
               "After=network-online.target keepalived.service\nWants=network-online.target\n\n"
               "[Service]\nType=simple\nUser=nobody\nGroup=nogroup\n"
               "EnvironmentFile=/etc/default/atlas-lb-vip-metrics\n"
               "ExecStart=/usr/bin/python3 /usr/local/libexec/atlas-kubekit/lb-vip-metrics.py --serve\n"
               "Restart=on-failure\nRestartSec=2s\nNoNewPrivileges=true\nPrivateTmp=true\n"
               "ProtectSystem=strict\nProtectHome=true\nProtectKernelTunables=true\n"
               "ProtectKernelModules=true\nProtectControlGroups=true\n"
               "RestrictAddressFamilies=AF_INET AF_UNIX AF_NETLINK\nLockPersonality=true\n"
               "MemoryDenyWriteExecute=true\n\n[Install]\nWantedBy=multi-user.target\n").encode()


def _validated(value):
    if not isinstance(value, dict) or set(value) not in ({"node", "interface", "vips"},
                                                          {"node", "interface", "vips", "expectedState"}):
        raise OperationError("ops.change.invalid_parameters")
    node, interface, vips = value["node"], value["interface"], value["vips"]
    if (not isinstance(node, str) or not re.fullmatch(r"lb[1-9][0-9]*", node)
            or not isinstance(interface, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", interface)
            or not isinstance(vips, list) or not 1 <= len(vips) <= 16):
        raise OperationError("ops.change.invalid_parameters")
    try:
        normalized = sorted({str(ipaddress.IPv4Address(vip)) for vip in vips},
                            key=lambda item: int(ipaddress.IPv4Address(item)))
    except (ValueError, TypeError):
        raise OperationError("ops.change.invalid_parameters") from None
    if any(ipaddress.IPv4Address(vip).is_loopback or ipaddress.IPv4Address(vip).is_unspecified
           or ipaddress.IPv4Address(vip).is_multicast for vip in normalized):
        raise OperationError("ops.change.invalid_parameters")
    result = {"node": node, "interface": interface, "vips": normalized}
    if "expectedState" in value:
        state = value["expectedState"]
        if (not isinstance(state, dict) or set(state) != {"installed", "active", "enabled", "digest"}
                or any(type(state[key]) is not bool for key in ("installed", "active", "enabled"))
                or (state["digest"] is not None and not isinstance(state["digest"], str))):
            raise OperationError("ops.change.invalid_parameters")
        result["expectedState"] = state
    return result


def _read(path):
    path = Path(path)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.geteuid():
        raise OperationError("ops.host.layout")
    data = path.read_bytes()
    if path == SCRIPT and b"Expose local Keepalived state" not in data:
        raise OperationError("ops.host.layout")
    return data


def _legacy_layout(files):
    """Recognize only the exact pre-marker layout emitted by setup-lb.sh."""
    script, env, unit = (files[path] for path in (SCRIPT, ENV, UNIT))
    if (not script.startswith(b"#!/usr/bin/env python3\n")
            or b"Expose local Keepalived state to HAProxy on loopback only." not in script
            or b"PORT = 8406" not in script):
        return False
    try:
        text = env.decode("ascii")
    except UnicodeDecodeError:
        return False
    if not re.fullmatch(
            r"ATLAS_LB_NAME=lb[1-9][0-9]*\n"
            r"ATLAS_LB_VIPS=(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?:,(?:[0-9]{1,3}\.){3}[0-9]{1,3})*\n"
            r"ATLAS_LB_INTERFACE=[A-Za-z0-9_.:-]{1,64}\n", text):
        return False
    return unit == LEGACY_UNIT


def _desired(value, source):
    node, interface = value["node"], value["interface"]
    env = (MARKER + "\nATLAS_LB_NAME=" + node + "\nATLAS_LB_INTERFACE=" + interface
           + "\nATLAS_LB_VIPS=" + ",".join(value["vips"]) + "\n").encode()
    unit = (MARKER + "\n[Unit]\nDescription=Atlas Keepalived VIP metrics\n"
            "After=network-online.target keepalived.service\nWants=network-online.target\n\n"
            "[Service]\nType=simple\nUser=nobody\nGroup=nogroup\n"
            "EnvironmentFile=/etc/default/atlas-lb-vip-metrics\n"
            "ExecStart=/usr/bin/python3 /usr/local/libexec/atlas-kubekit/lb-vip-metrics.py --serve\n"
            "NoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
            "ProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n"
            "RestrictAddressFamilies=AF_INET AF_UNIX AF_NETLINK\nRestrictNamespaces=yes\n"
            "LockPersonality=yes\nMemoryDenyWriteExecute=yes\n\n[Install]\nWantedBy=multi-user.target\n").encode()
    return {SCRIPT: source, ENV: env, UNIT: unit}


def _desired_state(value):
    with stage_assets.installed() as assets:
        source = (assets / "stages/lb-vip-metrics.py").read_bytes()
    desired = _desired(value, source)
    digest = hashlib.sha256(b"\0".join(desired[path] for path in (SCRIPT, ENV, UNIT))).hexdigest()
    return {"installed": True, "active": True, "enabled": True, "digest": digest}


def _state(files):
    if all(files[path] is None for path in (SCRIPT, ENV, UNIT)):
        installed = False
        digest = None
    else:
        if not all(files[path] is not None for path in (SCRIPT, ENV, UNIT)):
            raise OperationError("ops.host.layout")
        marked = (MARKER.encode() in files[ENV] and MARKER.encode() in files[UNIT]
                  and (MARKER.encode() in files[SCRIPT] or
                       b"Expose local Keepalived state to HAProxy on loopback only." in files[SCRIPT]))
        if not marked and not _legacy_layout(files):
            raise OperationError("ops.host.layout")
        installed = True
        digest = hashlib.sha256(b"\0".join(files[path] for path in (SCRIPT, ENV, UNIT))).hexdigest()
    active = subprocess.run(["systemctl", "is-active", "--quiet", "atlas-lb-vip-metrics"],
                            check=False).returncode == 0
    enabled = subprocess.run(["systemctl", "is-enabled", "--quiet", "atlas-lb-vip-metrics"],
                             check=False).returncode == 0
    return {"installed": installed, "active": active, "enabled": enabled, "digest": digest}


def observe(parameters, run_id):
    if parameters != {}:
        raise OperationError("ops.change.invalid_parameters")
    files = {path: _read(path) for path in (SCRIPT, ENV, UNIT)}
    return {"identity": identity_facts(), "state": _state(files)}


def desired(parameters, run_id):
    """Return the exact managed content fingerprint without changing the host."""
    value = _validated(parameters)
    return {"identity": identity_facts(), "state": _desired_state(value)}


def apply(parameters, run_id):
    value = _validated(parameters)
    if os.geteuid() != 0:
        raise OperationError("ops.error.root")
    with stage_assets.installed() as assets:
        source = (assets / "stages/lb-vip-metrics.py").read_bytes()
    desired = _desired(value, source)
    before = {path: _read(path) for path in desired}
    # Refuse partial or foreign installations rather than repair ambiguous state.
    observed = _state(before)
    desired_digest = hashlib.sha256(b"\0".join(desired[path] for path in (SCRIPT, ENV, UNIT))).hexdigest()
    expected_state = value.get("expectedState")
    if expected_state is None or (observed != expected_state and not
                                  (observed["installed"] and observed["digest"] == desired_digest)):
        raise OperationError("ops.change.stale")
    changed = any(before[path] != data for path, data in desired.items())
    for folder in (SCRIPT.parent, ENV.parent, UNIT.parent):
        folder.mkdir(mode=0o755, parents=True, exist_ok=True)
    old_modes = {path: (path.stat().st_mode & 0o777) if path.exists() else None for path in desired}
    enabled_before = subprocess.run(["systemctl", "is-enabled", "--quiet", "atlas-lb-vip-metrics"],
                                    check=False).returncode == 0
    active_before = subprocess.run(["systemctl", "is-active", "--quiet", "atlas-lb-vip-metrics"],
                                   check=False).returncode == 0
    try:
        for path, data in desired.items():
            if before[path] != data:
                common.replace(path, data, 0o755 if path == SCRIPT else 0o640 if path == ENV else 0o644)
        if changed:
            common.run(["systemctl", "daemon-reload"])
        common.run(["systemctl", "enable", "atlas-lb-vip-metrics"])
        if changed or not active_before:
            common.run(["systemctl", "restart", "atlas-lb-vip-metrics"])
        common.run(["systemctl", "is-active", "atlas-lb-vip-metrics"])
    except BaseException:
        try:
            for path, data in before.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    common.replace(path, data, old_modes[path] or 0o644)
            common.run(["systemctl", "daemon-reload"])
            if active_before:
                common.run(["systemctl", "restart", "atlas-lb-vip-metrics"])
            else:
                common.run(["systemctl", "stop", "atlas-lb-vip-metrics"], check=False)
            if not enabled_before:
                common.run(["systemctl", "disable", "atlas-lb-vip-metrics"], check=False)
        except BaseException:
            raise OperationError("ops.host.rollback") from None
        raise
    files = {path: _read(path) for path in desired}
    state = _state(files)
    if state != {"installed": True, "active": True, "enabled": True,
                 "digest": hashlib.sha256(b"\0".join(desired[path] for path in (SCRIPT, ENV, UNIT))).hexdigest()}:
        raise OperationError("ops.host.failed")
    return {"identity": identity_facts(), "state": state, "changed": changed}
