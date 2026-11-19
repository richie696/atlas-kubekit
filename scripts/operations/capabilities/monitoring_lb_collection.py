# SPDX-License-Identifier: MIT
"""Plan and publish the registered LB HAProxy metrics scrape path."""
import copy

from ..adapters.kubernetes import resource_name
from ..configuration import normalize, revision
from ..models import OperationError
from ..monitoring import lb_collection


def _manifests(values):
    try:
        return lb_collection.manifests(values["namespace"], values["release"], values["nodes"])
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    required = {"configuration", "namespace", "release", "prometheusName", "nodes", "expected"}
    if not isinstance(value, dict) or set(value) != required:
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result["configuration"] = normalize(value["configuration"])
    for key in ("namespace", "prometheusName"):
        resource_name(value[key])
    if not isinstance(value["release"], str) or not isinstance(value["nodes"], list):
        raise OperationError("ops.change.invalid_parameters")
    configured_nodes = [{"name": node["name"], "address": node["address"]}
                        for node in result["configuration"]["nodes"] if node["name"].startswith("lb")]
    if (not result["configuration"]["cluster"]["statsAllowedCIDR"]
            or result["nodes"] != configured_nodes
            or not isinstance(value["expected"], dict)
            or set(value["expected"]) != {"prometheus", "resources"}
            or not isinstance(value["expected"].get("prometheus"), dict)
            or set(value["expected"]["prometheus"]) != {"uid", "specHash"}
            or not all(isinstance(item, str) and item for item in value["expected"]["prometheus"].values())
            or not isinstance(value["expected"].get("resources"), dict)
            or set(value["expected"]["resources"]) != {"service", "endpoints", "servicemonitor"}):
        raise OperationError("ops.change.invalid_parameters")
    for identity in value["expected"]["resources"].values():
        if identity is not None and (
            not isinstance(identity, dict)
            or set(identity) != {"uid", "resourceVersion", "specHash"}
            or not all(isinstance(item, str) and item for item in identity.values())
        ):
            raise OperationError("ops.change.invalid_parameters")
    _manifests(result)
    return result


def from_arguments(args, configuration, environment):
    config, _ = environment.verify(configuration)
    if not config["cluster"]["statsAllowedCIDR"]:
        raise OperationError("ops.monitoring.lb_access_required")
    nodes = [{"name": node["name"], "address": node["address"]}
             for node in config["nodes"] if node["name"].startswith("lb")]
    values = {"configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "nodes": nodes}
    service, endpoints, monitor = _manifests(values)
    values["expected"] = lb_collection.capture(environment.client, service, endpoints, monitor,
                                                values["prometheusName"])
    return parameters(values)


def preview(value):
    values = parameters(value)
    return (("ops.monitoring.lb_collection_preview", (
        len(values["nodes"]), values["namespace"], values["prometheusName"],
        values["configuration"]["cluster"]["statsAllowedCIDR"])),
        ("ops.monitoring.lb_collection_nodes", (", ".join(
            row["name"] + "=" + row["address"] for row in values["nodes"]),)),
        ("ops.monitoring.lb_collection_warning", ()))


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    service, endpoints, monitor = _manifests(values)
    expected = lb_collection.capture(environment.client, service, endpoints, monitor, values["prometheusName"])
    if expected != values["expected"]:
        raise OperationError("ops.change.stale")
    return {"configurationRevision": revision(config), "expected": expected,
            "nodes": values["nodes"]}


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    service, endpoints, monitor = _manifests(values)
    checkpoint("monitoring-lb-collection")
    return lb_collection.publish(environment.client, service, endpoints, monitor,
                                 values["prometheusName"], values["expected"], checkpoint)
