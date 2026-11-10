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
    return text[: match.start()] + replacement + text[match.end() :]


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
