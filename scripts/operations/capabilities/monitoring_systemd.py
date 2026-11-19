# SPDX-License-Identifier: MIT
"""Install host-side kubelet/containerd telemetry through a resumable plan."""
import copy

from ..configuration import normalize, revision
from ..environment import check_identity
from ..models import OperationError


def node_names(configuration):
    return sorted(node["name"] for node in configuration["nodes"] if not node["name"].startswith("lb"))


def _snapshot(value, environment):
    config, _ = environment.verify(value["configuration"])
    nodes = {}
    for name in node_names(config):
        row = environment.host(config, name).call("systemd-metrics-observe")
        check_identity(config, name, row["identity"])
        metrics = row["metrics"]
        nodes[name] = {key: metrics[key] for key in ("collectorInstalled", "timerActive", "timerEnabled")}
    return {"configurationRevision": revision(config), "nodes": nodes}


def parameters(value):
    if not isinstance(value, dict) or set(value) != {"configuration", "expected"}:
        raise OperationError("ops.change.invalid_parameters")
    config = normalize(value["configuration"])
    expected = value["expected"]
    names = node_names(config)
    if (not isinstance(expected, dict) or set(expected) != {"configurationRevision", "nodes"} or
            not isinstance(expected["configurationRevision"], str) or
            not isinstance(expected["nodes"], dict) or set(expected["nodes"]) != set(names)):
        raise OperationError("ops.change.invalid_parameters")
    for row in expected["nodes"].values():
        if (not isinstance(row, dict) or set(row) != {"collectorInstalled", "timerActive", "timerEnabled"} or
                any(type(row[key]) is not bool for key in row)):
            raise OperationError("ops.change.invalid_parameters")
    return {"configuration": config, "expected": copy.deepcopy(expected)}


def from_arguments(args, configuration, environment):
    config, _ = environment.verify(configuration)
    value = {"configuration": config}
    value["expected"] = _snapshot(value, environment)
    return parameters(value)


def preview(value):
    values = parameters(value)
    names = node_names(values["configuration"])
    already = sum(1 for row in values["expected"]["nodes"].values()
                  if row["collectorInstalled"] and row["timerActive"] and row["timerEnabled"])
    return (("ops.monitoring.systemd_preview", (len(names), ", ".join(names), already)),
            ("ops.monitoring.systemd_warning", ()))


def observe(value, environment):
    values = parameters(value)
    observed = _snapshot(values, environment)
    if observed != values["expected"]:
        raise OperationError("ops.change.stale")
    return observed


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    results = {}
    for name in node_names(values["configuration"]):
        checkpoint("systemd-metrics-" + name + "-start")
        results[name] = environment.host(values["configuration"], name).call(
            "systemd-metrics-apply", {}, run_id, timeout=180
        )
        check_identity(values["configuration"], name, results[name]["identity"])
        checkpoint("systemd-metrics-" + name + "-complete")
    return {"state": "installed", "nodes": results,
            "verification": "Host timer and metric freshness still require independent observation"}


def continue_parameters(value, environment):
    values = parameters(value)
    refreshed = copy.deepcopy(values)
    refreshed["expected"] = _snapshot(refreshed, environment)
    return parameters(refreshed)
