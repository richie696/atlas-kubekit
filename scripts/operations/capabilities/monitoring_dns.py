# SPDX-License-Identifier: MIT
"""Plan and publish explicitly targeted CoreDNS alert rules."""
import copy
from scripts.lib.i18n import translate
from ..configuration import revision
from ..models import OperationError, fingerprint
from ..monitoring import localization, publication, scrape_rules
from .monitoring_scrapes import parameters as scrape_parameters


_BASE_FIELDS = {"configuration", "namespace", "release", "prometheusName", "targets", "holdSeconds",
                "maxAgeSeconds", "severity", "summary", "description", "expected"}
_FIELDS = _BASE_FIELDS | {"errorPercent", "windowSeconds", "latencyMs", "errorSummary", "errorDescription",
                          "latencyMinRequests", "latencySummary", "latencyDescription", "deployment", "minReplicas",
                          "replicaSummary", "replicaDescription"}


def _manifest(values):
    try:
        return scrape_rules.core_dns_rules(values["namespace"], values["release"], values["targets"],
            values["holdSeconds"], values["maxAgeSeconds"], values["severity"], values["errorPercent"],
            values["windowSeconds"], values["latencyMs"], values["summary"], values["description"],
            values["errorSummary"], values["errorDescription"], values["latencySummary"],
            values["latencyDescription"], values["deployment"], values["minReplicas"],
            values["replicaSummary"], values["replicaDescription"], values["latencyMinRequests"])
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise OperationError("ops.change.invalid_parameters")
    base = scrape_parameters({key: value[key] for key in _BASE_FIELDS})
    result = {**base, "errorPercent": value["errorPercent"], "windowSeconds": value["windowSeconds"],
              "latencyMs": value["latencyMs"], "errorSummary": value["errorSummary"],
              "latencyMinRequests": value["latencyMinRequests"],
              "errorDescription": value["errorDescription"], "latencySummary": value["latencySummary"],
              "latencyDescription": value["latencyDescription"], "deployment": value["deployment"],
              "minReplicas": value["minReplicas"], "replicaSummary": value["replicaSummary"],
              "replicaDescription": value["replicaDescription"]}
    _manifest(result)
    return result


def from_arguments(args, config, environment):
    verified, _ = environment.verify(config)
    deployment = args.monitoring_dns_deployment
    if not isinstance(deployment, str) or deployment.count("/") != 1:
        raise OperationError("ops.change.invalid_parameters")
    deployment_namespace, deployment_name = deployment.split("/", 1)
    targets = []
    for item in args.monitoring_target or []:
        if not isinstance(item, str) or "=" not in item:
            raise OperationError("ops.change.invalid_parameters")
        job, instance = item.split("=", 1)
        targets.append({"job": job, "instance": instance})
    values = {"configuration": verified, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "targets": targets, "holdSeconds": args.monitoring_hold_seconds,
              "maxAgeSeconds": args.monitoring_max_age, "severity": args.monitoring_severity,
              "summary": translate(localization.notification_language(args), "ops.monitoring.dns_summary"),
              "description": translate(localization.notification_language(args), "ops.monitoring.dns_description"),
              "errorPercent": args.monitoring_dns_error_percent,
              "windowSeconds": args.monitoring_dns_window_seconds,
              "latencyMs": args.monitoring_dns_latency_ms,
              "latencyMinRequests": args.monitoring_dns_min_requests,
              "errorSummary": translate(localization.notification_language(args), "ops.monitoring.dns_error_summary"),
              "errorDescription": translate(localization.notification_language(args), "ops.monitoring.dns_error_description",
                                             args.monitoring_dns_error_percent),
              "latencySummary": translate(localization.notification_language(args), "ops.monitoring.dns_latency_summary"),
              "latencyDescription": translate(localization.notification_language(args),
                                               "ops.monitoring.dns_latency_description", args.monitoring_dns_latency_ms,
                                               args.monitoring_dns_window_seconds, args.monitoring_dns_min_requests),
              "deployment": {"namespace": deployment_namespace, "name": deployment_name},
              "minReplicas": args.monitoring_dns_min_replicas,
              "replicaSummary": translate(localization.notification_language(args),
                                          "ops.monitoring.dns_replica_summary"),
              "replicaDescription": translate(localization.notification_language(args),
                                               "ops.monitoring.dns_replica_description",
                                               args.monitoring_dns_min_replicas)}
    desired = _manifest(values)
    values["expected"] = publication.capture(environment.client, desired, values["prometheusName"])
    return parameters(values)


def preview(value):
    values = parameters(value)
    targets = ", ".join(t["job"] + "=" + t["instance"] for t in values["targets"])
    return (("ops.monitoring.dns_preview", (len(values["targets"]), values["namespace"],
                values["prometheusName"], values["errorPercent"], values["latencyMs"],
                values["latencyMinRequests"], values["windowSeconds"], values["holdSeconds"], values["maxAgeSeconds"],
                values["deployment"]["namespace"] + "/" + values["deployment"]["name"],
                values["minReplicas"])),
            ("ops.monitoring.scrape_targets_preview", (targets,)),
            ("ops.monitoring.dns_warning", ()), ("ops.monitoring.rule_warning", ()))


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
    checkpoint("monitoring-dns-rules")
    return publication.publish(environment.client, _manifest(values), values["prometheusName"], values["expected"])
