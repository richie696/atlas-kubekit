# SPDX-License-Identifier: MIT
"""Plan and publish registered LB health and capacity alerts."""
import copy

from scripts.lib.i18n import translate
from ..adapters.kubernetes import resource_name
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import lb_rules, publication
from .monitoring_rules import _expected


def _nodes(configuration):
    return [{"name": node["name"], "address": node["address"]}
            for node in configuration["nodes"] if node["name"].startswith("lb")]


def _vips(configuration):
    cluster = normalize(configuration)["cluster"]
    return list(dict.fromkeys(value for value in (cluster["apiVIP"], cluster["appVIP"]) if value))


def _manifest(values):
    try:
        return lb_rules.load_balancer_rules(
            values["namespace"], values["release"], values["nodes"], values["vips"],
            values["holdSeconds"], values["maxAgeSeconds"], values["severity"],
            values["capacityPercent"], values["connectionErrorThreshold"],
            values["driftThreshold"], values["summaries"], values["descriptions"],
        )
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    required = {"configuration", "namespace", "release", "prometheusName", "nodes", "holdSeconds",
                "maxAgeSeconds", "severity", "capacityPercent", "connectionErrorThreshold",
                "vips", "driftThreshold", "summaries", "descriptions", "expected"}
    if not isinstance(value, dict) or set(value) != required:
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result["configuration"] = normalize(value["configuration"])
    resource_name(value["namespace"])
    resource_name(value["prometheusName"])
    if result["nodes"] != _nodes(result["configuration"]):
        raise OperationError("ops.lifecycle.inventory")
    if result["vips"] != _vips(result["configuration"]):
        raise OperationError("ops.change.invalid_parameters")
    result["expected"] = _expected(value["expected"])
    _manifest(result)
    return result


def from_arguments(args, configuration, environment):
    config, _ = environment.verify(configuration)
    lang = args.lang
    values = {
        "configuration": config,
        "namespace": args.monitoring_namespace,
        "release": args.monitoring_release,
        "prometheusName": args.monitoring_prometheus_name,
        "nodes": _nodes(config),
        "holdSeconds": args.monitoring_hold_seconds,
        "maxAgeSeconds": args.monitoring_max_age,
        "severity": args.monitoring_severity,
        "capacityPercent": args.monitoring_lb_capacity_percent,
        "connectionErrorThreshold": args.monitoring_lb_connection_error_threshold,
        "vips": _vips(config),
        "driftThreshold": args.monitoring_lb_drift_threshold,
        "summaries": {kind: translate(lang, "ops.monitoring.lb_" + kind + "_summary")
                       for kind in ("target", "backend", "capacity", "connection_errors", "queue",
                                    "keepalived", "vip_missing", "vip_no_owner", "vip_multiple", "vip_drift")},
        "descriptions": {kind: translate(lang, "ops.monitoring.lb_" + kind + "_description")
                         for kind in ("target", "backend", "capacity", "connection_errors", "queue",
                                      "keepalived", "vip_missing", "vip_no_owner", "vip_multiple", "vip_drift")},
    }
    manifest = _manifest(values)
    values["expected"] = publication.capture(environment.client, manifest, values["prometheusName"])
    return parameters(values)


def preview(value):
    values = parameters(value)
    return (("ops.monitoring.lb_rules_preview", (
        len(values["nodes"]), values["namespace"], values["prometheusName"],
        values["holdSeconds"], values["maxAgeSeconds"], values["capacityPercent"],
        values["connectionErrorThreshold"], values["driftThreshold"])),
        ("ops.monitoring.lb_collection_nodes", (", ".join(
            row["name"] + "=" + row["address"] for row in values["nodes"]),)),
        ("ops.monitoring.lb_rules_warning", ()))


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    manifest = _manifest(values)
    observed = publication.capture(environment.client, manifest, values["prometheusName"])
    if observed != values["expected"]:
        raise OperationError("ops.change.stale")
    return {"configurationRevision": revision(config), "target": observed,
            "desiredSpecHash": fingerprint(manifest["spec"])}


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    checkpoint("monitoring-lb-rules")
    return publication.publish(environment.client, _manifest(values), values["prometheusName"], values["expected"])
