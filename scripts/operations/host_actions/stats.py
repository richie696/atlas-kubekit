# SPDX-License-Identifier: MIT
"""Change only the generated stats frontend, preserving all application routes."""
import ipaddress
import json
from pathlib import Path
import re

from ..collector import identity_facts
from ..configuration import management_cidr as cidr
from ..models import OperationError
from . import common

HAPROXY = Path("/etc/haproxy/haproxy.cfg")
MANAGED_STATE = Path("/etc/atlas-kubekit/lb-manager.json")
SECTION = r"^frontend stats[ \t]*\n(.*?)(?=^(?:frontend|backend|listen|global|defaults)\b|^# BEGIN ATLAS KUBEKIT MANAGED ROUTES$|\Z)"
METRICS_SECTION = r"^frontend prometheus-metrics[ \t]*\n(.*?)(?=^(?:frontend|backend|listen|global|defaults)\b|^# BEGIN ATLAS KUBEKIT MANAGED ROUTES$|\Z)"
VIP_BACKEND_SECTION = r"^backend atlas-vip-metrics[ \t]*\n(.*?)(?=^(?:frontend|backend|listen|global|defaults)\b|^# BEGIN ATLAS KUBEKIT MANAGED ROUTES$|\Z)"


def render(text, allowed):
    allowed = cidr(allowed)
    matches = list(re.finditer(SECTION, text, re.M | re.S))
    if len(matches) != 1:
        raise OperationError("ops.host.layout")
    match = matches[0]
    body = match[1]
    # A foreign ACL/HTTP rule cannot be safely reordered around the source deny.
    remaining = []
    for row in body.splitlines():
        line = row.strip()
        if (
            not line
            or line.startswith("#")
            or re.fullmatch(r"bind (?:\*|127\.0\.0\.1|0\.0\.0\.0):7000", line)
            or re.fullmatch(r"acl stats_source_allowed src [0-9./ ]+", line)
            or line == "http-request deny if !stats_source_allowed"
        ):
            continue
        if line not in ("mode http", "stats enable", "stats uri /", "stats refresh 10s"):
            raise OperationError("ops.host.layout")
        remaining.append(row)
    if set(r.strip() for r in remaining) != {
        "mode http",
        "stats enable",
        "stats uri /",
        "stats refresh 10s",
    }:
        raise OperationError("ops.host.layout")
    binding = (
        "    bind *:7000\n    acl stats_source_allowed src "
        + allowed
        + "\n    http-request deny if !stats_source_allowed\n"
        if allowed
        else "    bind 127.0.0.1:7000\n"
    )
    replacement = "frontend stats\n" + binding + "\n".join(remaining) + "\n\n"
    text = text[: match.start()] + replacement + text[match.end() :]

    matches = list(re.finditer(METRICS_SECTION, text, re.M | re.S))
    if len(matches) > 1:
        raise OperationError("ops.host.layout")
    metrics_binding = (
        "    bind *:8405\n    acl metrics_source_allowed src "
        + allowed
        + "\n    http-request deny if !metrics_source_allowed\n"
        if allowed
        else "    bind 127.0.0.1:8405\n"
    )
    metrics_body = (
        "frontend prometheus-metrics\n" + metrics_binding
        + "    mode http\n    option socket-stats\n"
        "    http-request use-service prometheus-exporter if { path /metrics }\n"
        "    use_backend atlas-vip-metrics if { path /vip-metrics }\n"
        "    no log\n\n"
    )
    if matches:
        metrics_match = matches[0]
        existing = [line.strip() for line in metrics_match[1].splitlines() if line.strip() and not line.strip().startswith("#")]
        allowed_lines = {
            "mode http",
            "option socket-stats",
            "http-request use-service prometheus-exporter if { path /metrics }",
            "use_backend atlas-vip-metrics if { path /vip-metrics }",
            "no log",
        }
        legacy_lines = allowed_lines - {"use_backend atlas-vip-metrics if { path /vip-metrics }"}
        bind_lines = [line for line in existing if line in ("bind *:8405", "bind 127.0.0.1:8405")]
        acl_lines = [line for line in existing if re.fullmatch(r"acl metrics_source_allowed src [0-9./ ]+", line)]
        deny_lines = [line for line in existing if line == "http-request deny if !metrics_source_allowed"]
        rest = [line for line in existing if line not in bind_lines + acl_lines + deny_lines]
        if (len(bind_lines) != 1 or len(acl_lines) > 1 or len(deny_lines) > 1
                or any(line not in allowed_lines for line in rest)
                or (set(rest) not in (allowed_lines, legacy_lines))
                or len(rest) != len(set(rest))
                or bool(acl_lines) != bool(deny_lines)):
            raise OperationError("ops.host.layout")
        text = text[:metrics_match.start()] + metrics_body + text[metrics_match.end():]
    else:
        marker = "# BEGIN ATLAS KUBEKIT MANAGED ROUTES"
        offset = text.index(marker) if marker in text else len(text)
        text = text[:offset].rstrip() + "\n\n" + metrics_body + text[offset:]

    backend_body = (
        "backend atlas-vip-metrics\n"
        "    mode http\n"
        "    http-request set-path /metrics\n"
        "    option httpchk GET /metrics\n"
        "    http-check expect status 200\n"
        "    server local-vip-metrics 127.0.0.1:8406 check inter 5s fall 2 rise 2\n\n"
    )
    backends = list(re.finditer(VIP_BACKEND_SECTION, text, re.M | re.S))
    if len(backends) > 1:
        raise OperationError("ops.host.layout")
    if backends:
        existing = [line.strip() for line in backends[0][1].splitlines()
                    if line.strip() and not line.strip().startswith("#")]
        expected = ["mode http", "http-request set-path /metrics", "option httpchk GET /metrics",
                    "http-check expect status 200",
                    "server local-vip-metrics 127.0.0.1:8406 check inter 5s fall 2 rise 2"]
        legacy = ["mode http", "option httpchk GET /metrics", "http-check expect status 200",
                  "server local-vip-metrics 127.0.0.1:8406 check inter 5s fall 2 rise 2"]
        if existing not in (expected, legacy):
            raise OperationError("ops.host.layout")
        match = backends[0]
        text = text[:match.start()] + backend_body + text[match.end():]
    else:
        marker = "# BEGIN ATLAS KUBEKIT MANAGED ROUTES"
        offset = text.index(marker) if marker in text else len(text)
        text = text[:offset].rstrip() + "\n\n" + backend_body + text[offset:]
    return text


def observe():
    # A manager state file establishes tool ownership without exposing its content.
    state = json.loads(common.read_owned(MANAGED_STATE))
    if not isinstance(state, dict) or not {"vips", "routes"} <= set(state):
        raise OperationError("ops.host.layout")
    data = common.read_owned(HAPROXY)
    render(data.decode(), "")
    code, _ = common.run(["systemctl", "is-active", "haproxy"], check=False)
    _, raw = common.run(["ip", "-j", "-4", "addr", "show"])
    addresses = sorted(
        a["local"]
        for row in json.loads(raw)
        for a in row.get("addr_info", [])
        if a.get("family") == "inet"
    )
    return dict(
        identity=identity_facts(),
        configurationSha=common.digest(data),
        active=code == 0,
        addresses=addresses,
    )


def apply(parameters, run_id):
    if not isinstance(parameters, dict) or set(parameters) != {"cidr", "expectedSha"}:
        raise OperationError("ops.change.invalid_parameters")
    allowed = cidr(parameters["cidr"])
    before = observe()
    if before["configurationSha"] != parameters["expectedSha"]:
        raise OperationError("ops.change.stale")
    if not before["active"]:
        raise OperationError("ops.host.inactive")
    original = common.read_owned(HAPROXY)
    mode = HAPROXY.stat().st_mode & 0o777
    candidate = render(original.decode(), allowed).encode()
    if candidate == original:
        return dict(changed=False, configurationSha=before["configurationSha"])
    directory = common.journal(run_id, "lb-stats")
    backup = directory / "before.cfg"
    common.replace(backup, original)
    candidate_file = directory / "candidate.cfg"
    common.replace(candidate_file, candidate)
    common.run(["haproxy", "-c", "-f", str(candidate_file)])
    if common.digest(common.read_owned(HAPROXY)) != parameters["expectedSha"]:
        raise OperationError("ops.change.stale")
    try:
        common.replace(HAPROXY, candidate, mode)
        common.run(["systemctl", "reload", "haproxy"])
        common.run(["systemctl", "is-active", "haproxy"])
        common.record(directory, "lb-stats", "succeeded", configurationSha=common.digest(candidate))
    except BaseException:
        common.replace(HAPROXY, original, mode)
        try:
            common.run(["systemctl", "reload", "haproxy"])
            common.record(directory, "lb-stats", "rolled-back")
        except OperationError:
            common.record(directory, "lb-stats", "rollback-failed")
            raise OperationError("ops.host.rollback") from None
        raise
    return dict(changed=True, configurationSha=common.digest(candidate))
