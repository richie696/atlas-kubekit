# SPDX-License-Identifier: MIT
"""Plan node-local network checks, target-scoped egress, and probe alerts."""
import copy

from scripts.lib.i18n import translate
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import network_probes
from .monitoring_rules import _expected


def _notification(language):
    return {
        "failedSummary": translate(language, "ops.monitoring.network_probe_failed_summary"),
        "failedDescription": translate(language, "ops.monitoring.network_probe_failed_description"),
        "scrapeSummary": translate(language, "ops.monitoring.network_probe_scrape_summary"),
    }


def _render(values, client):
    try:
        services = client.get("service").get("items", [])
        pods = client.get("pod").get("items", [])
        return network_probes.manifests(
            values["namespace"], values["release"], values["prometheusName"],
            values["exporterRelease"], values["targets"], services, pods,
            values["notification"],
        )
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.monitoring.network_target_error") from None


def parameters(value):
    required = {"configuration", "namespace", "release", "prometheusName", "exporterRelease",
                "targets", "notification", "expected"}
    if not isinstance(value, dict) or set(value) != required or not isinstance(value["expected"], dict):
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result["configuration"] = normalize(value["configuration"])
    try:
        network_probes.parse_targets(result["targets"])
    except (TypeError, ValueError):
        raise OperationError("ops.monitoring.network_target") from None
    if (set(result["expected"]) != {"prometheus", "service", "daemonset", "targets", "monitor", "policy", "rule"} or
            not isinstance(result["notification"], dict) or
            set(result["notification"]) != {"failedSummary", "failedDescription", "scrapeSummary"}):
        raise OperationError("ops.change.invalid_parameters")
    for key in ("prometheus", "service", "daemonset", "monitor", "policy", "rule"):
        _expected({"prometheus": result["expected"][key], "rule": result["expected"][key]})
    if not isinstance(result["expected"]["targets"], str) or len(result["expected"]["targets"]) != 64:
        raise OperationError("ops.change.invalid_parameters")
    return result


def from_arguments(args, config, environment):
    config, _ = environment.verify(config)
    target_specs = args.monitoring_network_target or []
    values = {"configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "exporterRelease": "blackbox-exporter", "targets": list(target_specs),
              "notification": _notification(args.monitoring_notification_language)}
    rendered = _render(values, environment.client)
    values["expected"] = network_probes.capture(
        environment.client, values["namespace"], values["release"], values["prometheusName"],
        values["exporterRelease"], rendered)
    return parameters(values)


def preview(value):
    values = parameters(value)
    names = ", ".join(item["name"] for item in network_probes.parse_targets(values["targets"]))
    return (("ops.monitoring.network_probe_preview", (values["namespace"], len(values["targets"]), names)),
            ("ops.monitoring.network_probe_warning", ()))


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    rendered = _render(values, environment.client)
    observed = network_probes.capture(
        environment.client, values["namespace"], values["release"], values["prometheusName"],
        values["exporterRelease"], rendered)
    if observed != values["expected"]:
        raise OperationError("ops.change.stale")
    return {"configurationRevision": revision(config), "target": observed,
            "desiredSpecHash": fingerprint([item["spec"] for item in rendered[:3]])}


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    rendered = _render(values, environment.client)
    observed = network_probes.capture(
        environment.client, values["namespace"], values["release"], values["prometheusName"],
        values["exporterRelease"], rendered)
    if observed != values["expected"]:
        raise OperationError("ops.change.stale")
    return network_probes.publish(environment.client, rendered, values["expected"], checkpoint)
