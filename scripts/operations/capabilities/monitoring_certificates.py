# SPDX-License-Identifier: MIT
"""Plan and publish cert-manager expiry/readiness rules."""
import copy

from scripts.lib.i18n import translate
from ..adapters.kubernetes import resource_name
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import certificate_rules, localization, publication
from .monitoring_rules import _expected


_FIELDS = {"configuration", "namespace", "release", "prometheusName", "certificates", "expiryDays",
           "holdSeconds", "maxAgeSeconds", "severity", "expirySummary", "expiryDescription",
           "readinessSummary", "readinessDescription", "telemetrySummary", "telemetryDescription", "expected"}


def _manifest(values):
    try:
        return certificate_rules.certificate_rules(
            values["namespace"], values["release"], values["certificates"], values["expiryDays"],
            values["holdSeconds"], values["maxAgeSeconds"], values["severity"], values["expirySummary"],
            values["expiryDescription"], values["readinessSummary"], values["readinessDescription"],
            values["telemetrySummary"], values["telemetryDescription"])
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    if not isinstance(value, dict) or set(value) != _FIELDS:
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
    certificates = []
    for target in args.monitoring_certificate or []:
        if not isinstance(target, str) or target.count("/") != 1:
            raise OperationError("ops.change.invalid_parameters")
        namespace, name = target.split("/", 1)
        certificates.append({"namespace": namespace, "name": name})
    language = localization.notification_language(args)
    values = {"configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "certificates": certificates, "expiryDays": args.monitoring_certificate_expiry_days,
              "holdSeconds": args.monitoring_hold_seconds, "maxAgeSeconds": args.monitoring_max_age,
              "severity": args.monitoring_severity,
              "expirySummary": translate(language, "ops.monitoring.certificate_expiry_summary"),
              "expiryDescription": translate(language, "ops.monitoring.certificate_expiry_description",
                                              args.monitoring_certificate_expiry_days),
              "readinessSummary": translate(language, "ops.monitoring.certificate_readiness_summary"),
              "readinessDescription": translate(language, "ops.monitoring.certificate_readiness_description"),
              "telemetrySummary": translate(language, "ops.monitoring.certificate_telemetry_summary"),
              "telemetryDescription": translate(language, "ops.monitoring.certificate_telemetry_description")}
    manifest = _manifest(values)
    values["expected"] = publication.capture(environment.client, manifest, values["prometheusName"])
    return parameters(values)


def preview(value):
    values = parameters(value)
    targets = ", ".join(item["namespace"] + "/" + item["name"] for item in values["certificates"])
    return (("ops.monitoring.certificate_preview", (len(values["certificates"]), values["expiryDays"],
                                                      values["namespace"], values["prometheusName"])),
            ("ops.monitoring.certificates_preview", (targets,)),
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
    checkpoint("monitoring-certificate-rules")
    return publication.publish(environment.client, _manifest(values), values["prometheusName"], values["expected"])
