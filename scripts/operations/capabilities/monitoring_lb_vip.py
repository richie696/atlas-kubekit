# SPDX-License-Identifier: MIT
"""Plan and install the private VIP ownership exporter on registered LBs."""
import copy

from ..configuration import normalize, revision
from ..environment import check_identity
from ..models import OperationError


def _vips(configuration):
    cluster = normalize(configuration)["cluster"]
    return sorted({value for value in (cluster["apiVIP"], cluster["appVIP"]) if value})


def _nodes(configuration):
    return [node for node in normalize(configuration)["nodes"] if node["name"].startswith("lb")]


def _snapshot(configuration, environment):
    config = normalize(configuration)
    expected_nodes = {}
    for node in _nodes(config):
        result = environment.host(config, node["name"]).call("lb-vip-metrics-observe")
        check_identity(config, node["name"], result["identity"])
        state = result["state"]
        if (not isinstance(state, dict) or set(state) != {"installed", "active", "enabled", "digest"}
                or any(type(state[key]) is not bool for key in ("installed", "active", "enabled"))
                or (state["digest"] is not None and not isinstance(state["digest"], str))):
            raise OperationError("ops.host.failed")
        expected_nodes[node["name"]] = state
    return {"configurationRevision": revision(config), "nodes": expected_nodes}


def parameters(value):
    if not isinstance(value, dict) or set(value) != {"configuration", "expected"}:
        raise OperationError("ops.change.invalid_parameters")
    config = normalize(value["configuration"])
    expected = value["expected"]
    names = {node["name"] for node in _nodes(config)}
    if (not isinstance(expected, dict) or set(expected) != {"configurationRevision", "nodes"}
            or not isinstance(expected["configurationRevision"], str)
            or not isinstance(expected["nodes"], dict) or set(expected["nodes"]) != names):
        raise OperationError("ops.change.invalid_parameters")
    for state in expected["nodes"].values():
        if (not isinstance(state, dict) or set(state) != {"installed", "active", "enabled", "digest"}
                or any(type(state[key]) is not bool for key in ("installed", "active", "enabled"))
                or (state["digest"] is not None and not isinstance(state["digest"], str))):
            raise OperationError("ops.change.invalid_parameters")
    return {"configuration": config, "expected": copy.deepcopy(expected)}


def from_arguments(args, configuration, environment):
    config, _ = environment.verify(configuration)
    value = {"configuration": config}
    value["expected"] = _snapshot(config, environment)
    return parameters(value)


def preview(value):
    values = parameters(value)
    names = [node["name"] for node in _nodes(values["configuration"])]
    installed = sum(1 for state in values["expected"]["nodes"].values()
                    if state["installed"] and state["active"] and state["enabled"])
    return (("ops.monitoring.lb_vip_exporter_preview",
             (len(names), ", ".join(names), installed, ", ".join(_vips(values["configuration"]))),),)


def observe(value, environment):
    values = parameters(value)
    observed = _snapshot(values["configuration"], environment)
    if observed != values["expected"]:
        raise OperationError("ops.change.stale")
    return observed


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    config = values["configuration"]
    environment.verify(config)
    results = {}
    vips = _vips(config)
    for node in _nodes(config):
        name = node["name"]
        checkpoint("monitoring-lb-vip-exporter-" + name + "-start")
        result = environment.host(config, name).call("lb-vip-metrics-apply", {
            "node": name, "interface": node["interface"], "vips": vips,
            "expectedState": values["expected"]["nodes"][name],
        }, run_id, timeout=180)
        check_identity(config, name, result["identity"])
        results[name] = result["state"]
        checkpoint("monitoring-lb-vip-exporter-" + name + "-complete")
    return {"state": "installed", "nodes": results,
            "verification": "Prometheus target discovery and rule evaluation require independent observation"}


def continue_parameters(value, environment):
    values = parameters(value)
    refreshed = copy.deepcopy(values)
    refreshed["expected"] = _snapshot(values["configuration"], environment)
    return parameters(refreshed)
