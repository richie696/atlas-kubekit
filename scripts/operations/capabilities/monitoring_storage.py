# SPDX-License-Identifier: MIT
"""Plan and publish owned PVC filesystem capacity rules."""
import copy
from scripts.lib.i18n import translate
from ..adapters.kubernetes import resource_name
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import localization, publication, storage_rules
from .monitoring_rules import _expected


def _manifest(values):
    try:
        return storage_rules.pvc_capacity_rules(values["namespace"], values["release"], values["claims"],
            values["freePercent"], values["holdSeconds"], values["maxAgeSeconds"], values["severity"],
            values["summary"], values["description"])
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    required = {"configuration", "namespace", "release", "prometheusName", "claims", "freePercent", "holdSeconds",
                "maxAgeSeconds", "severity", "summary", "description", "expected"}
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
    claims = []
    for item in args.monitoring_pvc or []:
        if not isinstance(item, str) or item.count("/") != 1:
            raise OperationError("ops.change.invalid_parameters")
        namespace, claim = item.split("/")
        claims.append({"namespace": namespace, "claim": claim})
    values = {"configuration": config, "namespace": args.monitoring_namespace, "release": args.monitoring_release,
              "prometheusName": args.monitoring_prometheus_name, "claims": claims,
              "freePercent": args.monitoring_free_percent, "holdSeconds": args.monitoring_hold_seconds,
              "maxAgeSeconds": args.monitoring_max_age, "severity": args.monitoring_severity,
              "summary": translate(localization.notification_language(args), "ops.monitoring.storage_summary"),
              "description": translate(localization.notification_language(args), "ops.monitoring.storage_description")}
    manifest = _manifest(values)
    values["expected"] = publication.capture(environment.client, manifest, values["prometheusName"])
    return parameters(values)


def preview(values):
    values = parameters(values)
    return (("ops.monitoring.storage_preview", (len(values["claims"]), values["namespace"],
                values["prometheusName"], values["freePercent"], values["holdSeconds"], values["maxAgeSeconds"])),
            ("ops.monitoring.pvcs_preview", (", ".join(p["namespace"] + "/" + p["claim"] for p in values["claims"]),)),
            ("ops.monitoring.storage_warning", ()), ("ops.monitoring.rule_warning", ()))


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
    checkpoint("monitoring-storage-rules")
    return publication.publish(environment.client, _manifest(values), values["prometheusName"], values["expected"])
