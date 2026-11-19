# SPDX-License-Identifier: MIT
"""Publish owned, cluster-wide workload health alerts through the change plan."""
import copy
from scripts.lib.i18n import translate
from ..adapters.kubernetes import resource_name
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import localization, publication, workload_health_rules
from .monitoring_rules import _expected


_SUMMARY_KEYS = ("pending", "container", "restarts", "oom", "job", "job_timeout", "cronjob_overdue", "hpa",
                 "hpa_metrics_unavailable", "keda_scaler_errors", "keda_object_errors", "keda_empty_response",
                 "keda_latency", "keda_target")


def _manifest(values):
    try:
        language = values["notificationLanguage"]
        summaries = {key: translate(language, "ops.monitoring.workload_health_" + key)
                     for key in _SUMMARY_KEYS}
        description = translate(language, "ops.monitoring.workload_health_description")
        return workload_health_rules.workload_health_rules(
            values["namespace"], values["release"], values["holdSeconds"], values["maxAgeSeconds"],
            values["restartThreshold"], values["windowSeconds"], values["jobTimeoutSeconds"],
            values["cronjobOverdueSeconds"], values["kedaLatencySeconds"], values["kedaNamespace"],
            values["severity"], summaries, description)
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    required = {"configuration", "namespace", "release", "prometheusName", "holdSeconds", "maxAgeSeconds",
                "restartThreshold", "windowSeconds", "jobTimeoutSeconds", "cronjobOverdueSeconds",
                "kedaLatencySeconds", "kedaNamespace", "severity", "notificationLanguage", "expected"}
    if not isinstance(value, dict) or set(value) != required:
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result["configuration"] = normalize(value["configuration"])
    if not isinstance(value["prometheusName"], str):
        raise OperationError("ops.change.invalid_parameters")
    resource_name(value["prometheusName"])
    if value["notificationLanguage"] not in ("en", "zh_CN", "zh_TW"):
        raise OperationError("ops.change.invalid_parameters")
    if not isinstance(value["kedaNamespace"], str):
        raise OperationError("ops.change.invalid_parameters")
    result["expected"] = _expected(value["expected"])
    _manifest(result)
    return result


def from_arguments(args, config, environment):
    config, _ = environment.verify(config)
    values = {"configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "holdSeconds": args.monitoring_hold_seconds, "maxAgeSeconds": args.monitoring_max_age,
              "restartThreshold": args.monitoring_restart_threshold,
              "windowSeconds": args.monitoring_restart_window_seconds,
              "jobTimeoutSeconds": args.monitoring_job_timeout_seconds,
              "cronjobOverdueSeconds": args.monitoring_cronjob_overdue_seconds,
              "kedaLatencySeconds": args.monitoring_keda_latency_seconds,
              "kedaNamespace": getattr(args, "monitoring_keda_namespace", ""),
              "severity": args.monitoring_severity,
              "notificationLanguage": localization.notification_language(args)}
    manifest = _manifest(values)
    values["expected"] = publication.capture(environment.client, manifest, values["prometheusName"])
    return parameters(values)


def preview(value):
    values = parameters(value)
    return (("ops.monitoring.workload_health_preview", (len(_SUMMARY_KEYS), values["namespace"],
             values["prometheusName"], values["holdSeconds"], values["maxAgeSeconds"],
             values["restartThreshold"], values["windowSeconds"], values["jobTimeoutSeconds"],
             values["cronjobOverdueSeconds"], values["kedaLatencySeconds"])),
            ("ops.monitoring.workload_health_warning", ()))


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
    checkpoint("monitoring-workload-health-rules")
    return publication.publish(environment.client, _manifest(values), values["prometheusName"],
                               values["expected"])
