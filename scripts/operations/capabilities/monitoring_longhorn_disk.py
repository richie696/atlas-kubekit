# SPDX-License-Identifier: MIT
"""Publish explicit Longhorn disk occupancy and readiness alerts."""
import copy
from scripts.lib.i18n import translate
from ..adapters.kubernetes import resource_name
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import localization, longhorn_disk_rules, publication
from .monitoring_rules import _expected


def _manifest(values):
    try:
        return longhorn_disk_rules.disk_capacity_rules(
            values["namespace"], values["release"], values["usedPercent"], values["holdSeconds"],
            values["maxAgeSeconds"], values["severity"], values["summaries"], values["descriptions"])
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    required = {"configuration", "namespace", "release", "prometheusName", "usedPercent", "holdSeconds",
                "maxAgeSeconds", "severity", "summaries", "descriptions", "expected"}
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
    lang = localization.notification_language(args)
    values = {"configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "usedPercent": args.monitoring_longhorn_disk_used_percent,
              "holdSeconds": args.monitoring_hold_seconds, "maxAgeSeconds": args.monitoring_max_age,
              "severity": args.monitoring_severity,
              "summaries": {key: translate(lang, "ops.monitoring.longhorn_disk_" + key + "_summary")
                            for key in ("capacity", "not_ready", "missing", "partial")},
              "descriptions": {key: translate(lang, "ops.monitoring.longhorn_disk_" + key + "_description")
                               for key in ("capacity", "not_ready", "missing", "partial")}}
    manifest = _manifest(values)
    values["expected"] = publication.capture(environment.client, manifest, values["prometheusName"])
    return parameters(values)


def preview(value):
    values = parameters(value)
    return (("ops.monitoring.longhorn_disk_preview", (values["usedPercent"], values["namespace"],
                values["prometheusName"], values["holdSeconds"], values["maxAgeSeconds"])),
            ("ops.monitoring.longhorn_disk_warning", ()), ("ops.monitoring.rule_warning", ()))


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
    checkpoint("monitoring-longhorn-disk-rules")
    return publication.publish(environment.client, _manifest(values), values["prometheusName"], values["expected"])
