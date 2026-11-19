# SPDX-License-Identifier: MIT
"""Remove the Atlas systemd metrics collector from one registered node."""
import copy
import re

from ..configuration import normalize
from ..environment import check_identity
from ..models import OperationError
from . import monitoring_systemd


def parameters(value):
    if not isinstance(value, dict) or set(value) != {"configuration", "node", "expected"}:
        raise OperationError("ops.change.invalid_parameters")
    config = normalize(value["configuration"])
    name = value["node"]
    expected = value["expected"]
    if (name not in monitoring_systemd.node_names(config) or
            not isinstance(expected, dict) or set(expected) != {"identityFingerprint", "files"} or
            not _digest(expected["identityFingerprint"]) or
            not isinstance(expected["files"], dict) or set(expected["files"]) != {"collector", "service", "timer"} or
            any(item is not None and not _digest(item) for item in expected["files"].values())):
        raise OperationError("ops.change.invalid_parameters")
    return {"configuration": config, "node": name, "expected": copy.deepcopy(expected)}


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _snapshot(configuration, name, environment):
    config, _ = environment.verify(configuration)
    if name not in monitoring_systemd.node_names(config):
        raise OperationError("ops.change.invalid_parameters")
    row = environment.host(config, name).call("systemd-metrics-observe-removal")
    check_identity(config, name, row["identity"])
    return {"identityFingerprint": row["identity"]["identityFingerprint"], "files": row["files"]}


def from_arguments(args, configuration, environment):
    return parameters({"configuration": configuration, "node": args.node,
                       "expected": _snapshot(configuration, args.node, environment)})


def preview(value):
    values = parameters(value)
    present = sum(item is not None for item in values["expected"]["files"].values())
    return (("ops.monitoring.systemd_remove_preview", (values["node"], present)),
            ("ops.monitoring.systemd_remove_warning", ()))


def observe(value, environment):
    values = parameters(value)
    observed = _snapshot(values["configuration"], values["node"], environment)
    if observed != values["expected"]:
        raise OperationError("ops.change.stale")
    return observed


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    config, name, expected = values["configuration"], values["node"], values["expected"]
    checkpoint("systemd-metrics-" + name + "-remove-start")
    result = environment.host(config, name).call("systemd-metrics-remove", {
        "expected": expected["files"],
        "identity": {"hostname": name, "identityFingerprint": expected["identityFingerprint"]},
    }, run_id, timeout=180)
    check_identity(config, name, result["identity"])
    if result["files"] != dict.fromkeys(expected["files"]):
        raise OperationError("ops.host.failed")
    checkpoint("systemd-metrics-" + name + "-remove-complete")
    return {"state": "removed", "node": name}


def continue_parameters(value, environment):
    values = parameters(value)
    current = _snapshot(values["configuration"], values["node"], environment)
    if current["identityFingerprint"] != values["expected"]["identityFingerprint"]:
        raise OperationError("ops.change.wrong_identity")
    return parameters({**values, "expected": current})
