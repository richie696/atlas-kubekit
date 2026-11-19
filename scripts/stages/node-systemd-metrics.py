#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Install a host-side systemd timer that exports Kubernetes unit state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time


UNITS = ("kubelet", "containerd")
METRICS_DIRECTORY = Path("/var/lib/atlas-kubekit/node-exporter-textfile")
INSTALLED_SCRIPT = Path("/usr/local/libexec/atlas-node-systemd-metrics.py")
SERVICE_PATH = Path("/etc/systemd/system/atlas-node-systemd-metrics.service")
TIMER_PATH = Path("/etc/systemd/system/atlas-node-systemd-metrics.timer")
METRICS_FILE = "atlas_systemd_units.prom"
METRICS_MARKER = "# HELP atlas_systemd_unit_probe_success Whether systemd state was read successfully."
MARKER = "# Managed by Atlas KubeKit"
SAFE_VALUE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")


def _unit_state(unit, runner=subprocess.run):
    """Read only fixed systemd properties; tolerate a not-installed unit."""
    if unit not in UNITS:
        raise ValueError("unsupported systemd unit")
    try:
        result = runner(
            ["systemctl", "show", "--property=LoadState", "--property=ActiveState", "--value", unit],
            check=False, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"probe_success": 0, "load_state": "unknown", "active_state": "unknown"}
    values = result.stdout.splitlines()
    if result.returncode != 0 or len(values) != 2:
        return {"probe_success": 0, "load_state": "unknown", "active_state": "unknown"}
    load_state, active_state = values
    if not SAFE_VALUE.fullmatch(load_state) or not SAFE_VALUE.fullmatch(active_state):
        return {"probe_success": 0, "load_state": "unknown", "active_state": "unknown"}
    return {"probe_success": 1, "load_state": load_state, "active_state": active_state}


def render_metrics(states):
    """Render bounded Prometheus text exposition from the two expected units."""
    if not isinstance(states, dict) or set(states) != set(UNITS):
        raise ValueError("invalid systemd unit state set")
    lines = [
        METRICS_MARKER,
        "# TYPE atlas_systemd_unit_probe_success gauge",
        "# HELP atlas_systemd_unit_active Whether the systemd unit is active.",
        "# TYPE atlas_systemd_unit_active gauge",
        "# HELP atlas_systemd_unit_state Systemd active state, one-hot encoded.",
        "# TYPE atlas_systemd_unit_state gauge",
        "# HELP atlas_systemd_unit_load_state Systemd load state, one-hot encoded.",
        "# TYPE atlas_systemd_unit_load_state gauge",
    ]
    for unit in UNITS:
        state = states[unit]
        if (not isinstance(state, dict) or state.get("probe_success") not in (0, 1) or
                not SAFE_VALUE.fullmatch(state.get("load_state", "")) or
                not SAFE_VALUE.fullmatch(state.get("active_state", ""))):
            raise ValueError("invalid systemd unit state")
        lines.append(f'atlas_systemd_unit_probe_success{{unit="{unit}"}} {state["probe_success"]}')
        lines.append(f'atlas_systemd_unit_active{{unit="{unit}"}} {int(state["active_state"] == "active")}')
        lines.append(f'atlas_systemd_unit_state{{unit="{unit}",state="{state["active_state"]}"}} 1')
        lines.append(f'atlas_systemd_unit_load_state{{unit="{unit}",state="{state["load_state"]}"}} 1')
    return "\n".join(lines) + "\n"


def collect(directory=METRICS_DIRECTORY, runner=subprocess.run):
    """Atomically replace the node-exporter textfile sample."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("metrics directory is missing or unsafe")
    destination = directory / METRICS_FILE
    if destination.exists() and not destination.read_text(encoding="utf-8").startswith(
            "# HELP atlas_systemd_unit_probe_success"):
        raise ValueError("refusing to replace a foreign metrics file")
    states = {unit: _unit_state(unit, runner) for unit in UNITS}
    payload = render_metrics(states)
    descriptor, temporary = tempfile.mkstemp(prefix=".atlas-systemd-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return destination


def _write_managed(path, content):
    """Atomically install a managed systemd unit, refusing foreign files."""
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlinked systemd unit: {path}")
    if path.exists() and MARKER not in path.read_text(encoding="utf-8"):
        raise ValueError(f"refusing to replace foreign systemd unit: {path}")
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".atlas-unit-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def install():
    """Install the collector and a least-privilege systemd oneshot timer."""
    if os.geteuid() != 0:
        raise PermissionError("run node systemd metrics installation as root")
    systemctl = shutil.which("systemctl")
    if not systemctl:
        raise RuntimeError("systemctl is unavailable")
    if METRICS_DIRECTORY.is_symlink():
        raise ValueError("metrics directory must not be a symlink")
    METRICS_DIRECTORY.mkdir(mode=0o755, parents=True, exist_ok=True)
    directory_info = METRICS_DIRECTORY.stat()
    if not METRICS_DIRECTORY.is_dir() or directory_info.st_uid != 0:
        raise ValueError("metrics directory must be a root-owned directory")
    if METRICS_DIRECTORY.is_symlink():
        raise ValueError("metrics directory must not be a symlink")
    if directory_info.st_mode & 0o777 != 0o755:
        os.chmod(METRICS_DIRECTORY, 0o755)
    source = Path(__file__).resolve()
    INSTALLED_SCRIPT.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if INSTALLED_SCRIPT.is_symlink():
        raise ValueError("refusing to replace symlinked node metrics collector")
    source_bytes = source.read_bytes()
    if INSTALLED_SCRIPT.exists() and b"Atlas KubeKit" not in INSTALLED_SCRIPT.read_bytes():
        raise ValueError("refusing to replace a foreign node metrics collector")
    if not INSTALLED_SCRIPT.exists() or INSTALLED_SCRIPT.read_bytes() != source_bytes:
        descriptor, temporary = tempfile.mkstemp(prefix=".atlas-collector-", dir=INSTALLED_SCRIPT.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(source_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o755)
            os.replace(temporary, INSTALLED_SCRIPT)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    service = f"""{MARKER}
[Unit]
Description=Export kubelet and containerd systemd state for node-exporter
After=systemd-journald.service

[Service]
Type=oneshot
User=root
Group=root
ExecStart=/usr/bin/python3 {INSTALLED_SCRIPT} --collect
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={METRICS_DIRECTORY}
"""
    timer = f"""{MARKER}
[Unit]
Description=Refresh Kubernetes systemd state metrics

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s
AccuracySec=5s
Unit=atlas-node-systemd-metrics.service

[Install]
WantedBy=timers.target
"""
    _write_managed(SERVICE_PATH, service)
    _write_managed(TIMER_PATH, timer)
    subprocess.run([systemctl, "daemon-reload"], check=True, timeout=30)
    subprocess.run([systemctl, "enable", "--now", "atlas-node-systemd-metrics.timer"], check=True, timeout=30)
    collect()


def observe():
    """Return safe installation facts without exposing host file contents."""
    try:
        active = subprocess.run(["systemctl", "is-active", "atlas-node-systemd-metrics.timer"],
                                check=False, capture_output=True, text=True, timeout=10)
        enabled = subprocess.run(["systemctl", "is-enabled", "atlas-node-systemd-metrics.timer"],
                                 check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        active, enabled = None, None
    installed = INSTALLED_SCRIPT.is_file() and not INSTALLED_SCRIPT.is_symlink()
    if installed:
        try:
            installed = b"Atlas KubeKit" in INSTALLED_SCRIPT.read_bytes()
        except OSError:
            installed = False
    metric_path = METRICS_DIRECTORY / METRICS_FILE
    metrics_age = None
    if metric_path.is_file() and not metric_path.is_symlink():
        try:
            metrics_age = max(0, int(time.time() - metric_path.stat().st_mtime))
        except OSError:
            metrics_age = None
    return {
        "collectorInstalled": installed,
        "timerActive": bool(active and active.returncode == 0 and active.stdout.strip() == "active"),
        "timerEnabled": bool(enabled and enabled.returncode == 0 and enabled.stdout.strip() == "enabled"),
        "metricsAgeSeconds": metrics_age,
        "units": {unit: _unit_state(unit) for unit in UNITS},
    }


def _owned_file(path, marker, *, marker_line=False):
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or
            metadata.st_mode & 0o022 or metadata.st_size > 65536):
        raise ValueError(f"unsafe collector file: {path}")
    content = path.read_bytes()
    owned = marker in content.splitlines() if marker_line else content.startswith(marker + b"\n")
    if not owned:
        raise ValueError(f"foreign collector file: {path}")
    return hashlib.sha256(content).hexdigest()


def _managed_paths():
    return {
        "timer": (TIMER_PATH, MARKER.encode(), False),
        "service": (SERVICE_PATH, MARKER.encode(), False),
        "collector": (INSTALLED_SCRIPT, b'MARKER = "# Managed by Atlas KubeKit"', True),
    }


def _check_parent_directories(*paths):
    directories = {directory for path in paths for directory in path.parents}
    for directory in sorted(directories, key=lambda item: (len(item.parts), str(item))):
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or
                metadata.st_mode & 0o022):
            raise ValueError(f"unsafe collector directory: {directory}")


def _systemctl(*arguments):
    return subprocess.run(["systemctl", *arguments], check=True, timeout=20,
                          capture_output=True, text=True).stdout


def removal_snapshot():
    """Capture stable ownership evidence. Textfile contents change on every collection."""
    paths = _managed_paths()
    metric_path = METRICS_DIRECTORY / METRICS_FILE
    _check_parent_directories(metric_path, *(path for path, _, _ in paths.values()))
    files = {key: _owned_file(path, marker, marker_line=line)
             for key, (path, marker, line) in paths.items()}
    _owned_file(metric_path, METRICS_MARKER.encode())
    for key in ("timer", "service"):
        path = paths[key][0]
        properties = _systemctl("show", "--property=FragmentPath,DropInPaths,LoadState,ActiveState,UnitFileState",
                                path.name)
        status = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
        fragment = status.get("FragmentPath")
        if (fragment not in ("", str(path)) or status.get("DropInPaths") != "" or
                status.get("LoadState") not in ("loaded", "not-found") or
                (not files[key] and (status.get("ActiveState") != "inactive" or
                                     status.get("UnitFileState") not in ("", "not-found")))):
            raise ValueError(f"unexpected systemd configuration for {path.name}")
        if files[key] and status["LoadState"] == "loaded" and fragment != str(path):
            raise ValueError(f"unexpected systemd fragment for {path.name}")
    return files


def uninstall(expected):
    """Remove only the collector files that can be proven to be Atlas-owned."""
    if os.geteuid() != 0:
        raise PermissionError("run node systemd metrics removal as root")
    if removal_snapshot() != expected:
        raise ValueError("collector files changed after planning")
    if expected["timer"]:
        _systemctl("disable", "--now", TIMER_PATH.name)
    if expected["service"]:
        _systemctl("stop", SERVICE_PATH.name)
    if removal_snapshot() != expected:
        raise ValueError("collector files changed during removal")
    for key, (path, _, _) in _managed_paths().items():
        if expected[key]:
            path.unlink()
    metric_path = METRICS_DIRECTORY / METRICS_FILE
    if _owned_file(metric_path, METRICS_MARKER.encode()):
        metric_path.unlink()
    _systemctl("daemon-reload")
    files = removal_snapshot()
    if _owned_file(metric_path, METRICS_MARKER.encode()):
        raise ValueError("collector metrics still present after removal")
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--collect", action="store_true")
    mode.add_argument("--observe", action="store_true")
    mode.add_argument("--removal-snapshot", action="store_true")
    mode.add_argument("--uninstall", metavar="EXPECTED_JSON")
    args = parser.parse_args(argv)
    if args.install:
        install()
    elif args.collect:
        collect()
    elif args.uninstall is not None:
        print(json.dumps(uninstall(json.loads(args.uninstall)), sort_keys=True))
    elif args.removal_snapshot:
        print(json.dumps(removal_snapshot(), sort_keys=True))
    else:
        sys.stdout.write(json.dumps(observe(), sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, PermissionError, subprocess.SubprocessError) as error:
        print(f"node systemd metrics: {error}", file=sys.stderr)
        raise SystemExit(1)
