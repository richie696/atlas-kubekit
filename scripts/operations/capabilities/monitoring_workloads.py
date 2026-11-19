# SPDX-License-Identifier: MIT
"""Plan and publish owned Deployment availability rules."""
import copy
from scripts.lib.i18n import translate
from ..adapters.kubernetes import resource_name
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import localization, publication, workload_rules
from .monitoring_rules import _expected


def _manifest(values):
    try:
        return workload_rules.deployment_rules(values["namespace"], values["release"], values["workloads"],
                                               values["holdSeconds"], values["maxAgeSeconds"], values["severity"],
                                               values["summary"], values["description"],
                                               values["rolloutSummary"], values["rolloutDescription"])
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    required = {"configuration", "namespace", "release", "prometheusName", "workloads", "holdSeconds",
                "maxAgeSeconds", "severity", "summary", "description", "rolloutSummary",
                "rolloutDescription", "expected"}
    if not isinstance(value, dict) or set(value) != required:
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result["configuration"] = normalize(value["configuration"])
    if not isinstance(value["prometheusName"], str):
        raise OperationError("ops.change.invalid_parameters")
    resource_name(value["prometheusName"])
    result["expected"] = _expected(value["expected"])
    _manifest(result)
    return result


def from_arguments(args, config, environment):
    config, _ = environment.verify(config)
    workloads = []
    for item in args.monitoring_deployment or []:
        if not isinstance(item, str) or item.count("/") != 1:
            raise OperationError("ops.change.invalid_parameters")
        namespace, deployment = item.split("/")
        workloads.append({"namespace": namespace, "deployment": deployment})
    values = {"configuration": config, "namespace": args.monitoring_namespace, "release": args.monitoring_release,
              "prometheusName": args.monitoring_prometheus_name, "workloads": workloads,
              "holdSeconds": args.monitoring_hold_seconds, "maxAgeSeconds": args.monitoring_max_age,
              "severity": args.monitoring_severity,
              "summary": translate(localization.notification_language(args), "ops.monitoring.workload_summary"),
              "description": translate(localization.notification_language(args), "ops.monitoring.workload_description"),
              "rolloutSummary": translate(localization.notification_language(args),
                                          "ops.monitoring.workload_rollout_summary"),
              "rolloutDescription": translate(localization.notification_language(args),
                                              "ops.monitoring.workload_rollout_description")}
    manifest = _manifest(values)
    values["expected"] = publication.capture(environment.client, manifest, values["prometheusName"])
    return parameters(values)


def preview(values):
    values = parameters(values)
    return (("ops.monitoring.workload_preview", (len(values["workloads"]), values["namespace"],
                values["prometheusName"], values["holdSeconds"], values["maxAgeSeconds"])),
            ("ops.monitoring.workloads_preview", (", ".join(w["namespace"] + "/" + w["deployment"]
                                                              for w in values["workloads"]),)),
            ("ops.monitoring.rule_warning", ()))


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
    checkpoint("monitoring-workload-rules")
    return publication.publish(environment.client, _manifest(values), values["prometheusName"], values["expected"])
