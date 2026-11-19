# SPDX-License-Identifier: MIT
"""Publish explicitly selected scrape-target rules through the change protocol."""
import copy
from scripts.lib.i18n import translate
from ..adapters.kubernetes import resource_name
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import localization, publication, scrape_rules
from .monitoring_rules import _expected


def _manifest(values):
    try:
        return scrape_rules.target_rules(values["namespace"], values["release"], values["targets"],
                                         values["holdSeconds"], values["maxAgeSeconds"],
                                         values["severity"], values["summary"], values["description"])
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def _parse_target(item):
    if not isinstance(item, str) or not item or "=" not in item:
        raise OperationError("ops.change.invalid_parameters")
    parts = item.split("|")
    job, instance = parts[0].split("=", 1)
    labels = {}
    for part in parts[1:]:
        if "=" not in part:
            raise OperationError("ops.change.invalid_parameters")
        name, value = part.split("=", 1)
        if name in labels:
            raise OperationError("ops.change.invalid_parameters")
        labels[name] = value
    result = {"job": job, "instance": instance}
    if labels:
        result["labels"] = labels
    return result


def parameters(value):
    required = {"configuration", "namespace", "release", "prometheusName", "targets", "holdSeconds",
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


def from_arguments(args, configuration, environment):
    config, _ = environment.verify(configuration)
    targets = [_parse_target(item) for item in (args.monitoring_target or [])]
    values = {"configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "targets": targets, "holdSeconds": args.monitoring_hold_seconds,
              "maxAgeSeconds": args.monitoring_max_age, "severity": args.monitoring_severity,
              "summary": translate(localization.notification_language(args), "ops.monitoring.scrape_summary"),
              "description": translate(localization.notification_language(args), "ops.monitoring.scrape_description")}
    manifest = _manifest(values)
    values["expected"] = publication.capture(environment.client, manifest, values["prometheusName"])
    return parameters(values)


def preview(values):
    values = parameters(values)
    return (("ops.monitoring.scrape_preview", (len(values["targets"]), values["namespace"],
                                                values["prometheusName"], values["holdSeconds"],
                                                values["maxAgeSeconds"])),
            ("ops.monitoring.scrape_targets_preview", (", ".join(
                t["job"] + "=" + t["instance"] + "".join(
                    "|" + name + "=" + value for name, value in sorted(t.get("labels", {}).items()))
                for t in values["targets"]),)),
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
    checkpoint("monitoring-scrape-rules")
    return publication.publish(environment.client, _manifest(values), values["prometheusName"], values["expected"])
