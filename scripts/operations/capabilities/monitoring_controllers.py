# SPDX-License-Identifier: MIT
"""Shared controlled publication for explicitly registered controller types."""
import copy
from scripts.lib.i18n import translate
from ..adapters.kubernetes import resource_name
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import localization, publication, workload_rules
from .monitoring_rules import _expected


def _manifest(kind, values):
    try:
        return workload_rules.controller_rules(kind, values["namespace"], values["release"], values["workloads"],
            values["holdSeconds"], values["maxAgeSeconds"], values["severity"], values["summary"], values["description"])
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(kind, value):
    fields = {"kind", "configuration", "namespace", "release", "prometheusName", "workloads", "holdSeconds",
              "maxAgeSeconds", "severity", "summary", "description", "expected"}
    if not isinstance(value, dict) or set(value) != fields or value.get("kind") != kind:
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result["configuration"] = normalize(value["configuration"])
    if not isinstance(value["prometheusName"], str):
        raise OperationError("ops.change.invalid_parameters")
    resource_name(value["prometheusName"])
    result["expected"] = _expected(value["expected"])
    _manifest(kind, result)
    return result


def from_arguments(kind, args, config, environment):
    config, _ = environment.verify(config)
    workloads = []
    for item in args.monitoring_controller or []:
        if not isinstance(item, str) or item.count("/") != 1:
            raise OperationError("ops.change.invalid_parameters")
        namespace, name = item.split("/")
        workloads.append({"namespace": namespace, kind: name})
    values = {"kind": kind, "configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "workloads": workloads, "holdSeconds": args.monitoring_hold_seconds,
              "maxAgeSeconds": args.monitoring_max_age, "severity": args.monitoring_severity,
              "summary": translate(localization.notification_language(args), "ops.monitoring." + kind + "_summary"),
              "description": translate(localization.notification_language(args), "ops.monitoring.controller_description")}
    manifest = _manifest(kind, values)
    values["expected"] = publication.capture(environment.client, manifest, values["prometheusName"])
    return parameters(kind, values)


def preview(kind, value):
    values = parameters(kind, value)
    return (("ops.monitoring.controller_preview", (kind, len(values["workloads"]), values["namespace"],
             values["prometheusName"], values["holdSeconds"], values["maxAgeSeconds"])),
            ("ops.monitoring.workloads_preview", (", ".join(w["namespace"] + "/" + w[kind] for w in values["workloads"]),)),
            ("ops.monitoring.rule_warning", ()))


def observe(kind, value, environment):
    values = parameters(kind, value)
    config, _ = environment.verify(values["configuration"])
    manifest = _manifest(kind, values)
    observed = publication.capture(environment.client, manifest, values["prometheusName"])
    if observed != values["expected"]:
        raise OperationError("ops.change.stale")
    return {"configurationRevision": revision(config), "target": observed, "desiredSpecHash": fingerprint(manifest["spec"])}


def apply(kind, value, environment, checkpoint, run_id):
    values = parameters(kind, value)
    environment.verify(values["configuration"])
    checkpoint("monitoring-" + kind + "-rules")
    return publication.publish(environment.client, _manifest(kind, values), values["prometheusName"], values["expected"])
