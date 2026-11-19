# SPDX-License-Identifier: MIT
"""Plan and publish authenticated notification-relay metrics collection."""
import copy

from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import relay_metrics as metrics
from scripts.lib.i18n import translate


def _manifests(values):
    try:
        return metrics.resources(values["namespace"], values["release"], values["prometheusName"],
            values["authSecret"], values["authKey"], values["summary"], values["description"])
    except (KeyError, TypeError, ValueError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    fields = {"configuration", "namespace", "release", "prometheusName", "authSecret", "authKey",
              "summary", "description", "expected"}
    if not isinstance(value, dict) or set(value) != fields or not isinstance(value["expected"], dict):
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result["configuration"] = normalize(result["configuration"])
    expected = result["expected"]
    if set(expected) != {"prometheus", "monitor", "rule"}:
        raise OperationError("ops.change.invalid_parameters")
    prometheus = expected["prometheus"]
    if (not isinstance(prometheus, dict) or set(prometheus) != {"uid", "resourceVersion"} or
            not all(isinstance(item, str) and item for item in prometheus.values())):
        raise OperationError("ops.change.invalid_parameters")
    for key in ("monitor", "rule"):
        identity = expected[key]
        if identity is not None and (
                not isinstance(identity, dict) or set(identity) != {"uid", "resourceVersion", "specHash"} or
                not all(isinstance(item, str) and item for item in identity.values())):
            raise OperationError("ops.change.invalid_parameters")
    _manifests(result)
    return result


def from_arguments(args, config, environment):
    config, _ = environment.verify(config)
    values = {"configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "authSecret": args.monitoring_relay_auth_secret, "authKey": args.monitoring_relay_auth_key,
              "summary": translate(args.lang, "ops.monitoring.relay_metrics_summary"),
              "description": translate(args.lang, "ops.monitoring.relay_metrics_description")}
    wanted = _manifests(values)
    values["expected"] = metrics.capture(environment.client, values["namespace"], values["prometheusName"],
        wanted["podMonitor"], wanted["rule"])
    return parameters(values)


def preview(value):
    values = parameters(value)
    return (("ops.monitoring.relay_metrics_preview", (values["namespace"], values["prometheusName"],
             values["authSecret"], values["authKey"])),
            ("ops.monitoring.relay_metrics_warning", ()))


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    wanted = _manifests(values)
    current = metrics.capture(environment.client, values["namespace"], values["prometheusName"],
                              wanted["podMonitor"], wanted["rule"])
    if current != values["expected"]:
        raise OperationError("ops.change.stale")
    return {"configurationRevision": revision(config), "target": current,
            "desiredSpecHash": fingerprint([wanted["podMonitor"]["spec"], wanted["rule"]["spec"]])}


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    wanted = _manifests(values)
    checkpoint("monitoring-relay-metrics")
    return metrics.publish(environment.client, values["namespace"], values["prometheusName"],
                           wanted["podMonitor"], wanted["rule"], values["expected"], checkpoint)
