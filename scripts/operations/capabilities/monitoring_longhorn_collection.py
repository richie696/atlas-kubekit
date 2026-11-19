# SPDX-License-Identifier: MIT
"""Plan and publish Longhorn metrics discovery and scoped ingress."""
import copy
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import longhorn_collection
from .monitoring_rules import _expected


def _manifests(values):
    try:
        return longhorn_collection.manifests(values["namespace"], values["release"],
            values["prometheusName"], values["longhornNamespace"])
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    fields = {"configuration", "namespace", "release", "prometheusName", "longhornNamespace", "expected"}
    if not isinstance(value, dict) or set(value) != fields or not isinstance(value["expected"], dict):
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result["configuration"] = normalize(value["configuration"])
    expected = result["expected"]
    if set(expected) != {"prometheus", "service", "monitor", "policy"}:
        raise OperationError("ops.change.invalid_parameters")
    _expected({"prometheus": expected["prometheus"], "rule": expected["monitor"]})
    _expected({"prometheus": expected["service"], "rule": expected["policy"]})
    _manifests(result)
    return result


def from_arguments(args, config, environment):
    config, _ = environment.verify(config)
    values = {"configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "longhornNamespace": args.monitoring_longhorn_namespace}
    monitor, policy = _manifests(values)
    values["expected"] = longhorn_collection.capture(environment.client, monitor, policy, values["prometheusName"])
    return parameters(values)


def preview(value):
    values = parameters(value)
    return (("ops.monitoring.collection_preview", (values["namespace"], values["prometheusName"], values["longhornNamespace"])),
            ("ops.monitoring.collection_warning", ()))


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    monitor, policy = _manifests(values)
    observed = longhorn_collection.capture(environment.client, monitor, policy, values["prometheusName"])
    if observed != values["expected"]:
        raise OperationError("ops.change.stale")
    return {"configurationRevision": revision(config), "target": observed,
            "desiredSpecHash": fingerprint([monitor["spec"], policy["spec"]])}


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    monitor, policy = _manifests(values)
    return longhorn_collection.publish(environment.client, monitor, policy, values["prometheusName"],
                                       values["expected"], checkpoint)
