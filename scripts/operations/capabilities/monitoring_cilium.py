# SPDX-License-Identifier: MIT
"""Plan and publish selected Cilium metrics and health rules."""
import copy

from scripts.lib.i18n import translate
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import cilium_metrics, localization


def _manifests(values):
    try:
        localized = values["localized"]
        return cilium_metrics.resources(
            values["namespace"], values["release"], values["prometheusName"], values["holdSeconds"],
            values["agentSummary"], values["agentDescription"],
            values["operatorSummary"], values["operatorDescription"],
            values["ipamHighPercent"], values["bpfPressurePercent"],
            localized["ipamMissingSummary"], localized["ipamMissingDescription"],
            localized["ipamSummary"], localized["ipamDescription"],
            localized["bpfSummary"], localized["bpfDescription"],
            localized["connectivitySummary"], localized["connectivityDescription"])
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    required = {"configuration", "namespace", "release", "prometheusName", "holdSeconds",
                "agentSummary", "agentDescription", "operatorSummary", "operatorDescription",
                "ipamHighPercent", "bpfPressurePercent", "localized", "expected"}
    if not isinstance(value, dict) or set(value) != required or not isinstance(value["expected"], dict):
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result["configuration"] = normalize(value["configuration"])
    if (set(value["expected"]) != {"prometheus", "agent", "operator", "rule"} or
            not isinstance(value["localized"], dict) or
            set(value["localized"]) != {"ipamMissingSummary", "ipamMissingDescription", "ipamSummary",
                                         "ipamDescription", "bpfSummary", "bpfDescription",
                                         "connectivitySummary", "connectivityDescription"}):
        raise OperationError("ops.change.invalid_parameters")
    _manifests(result)
    return result


def from_arguments(args, config, environment):
    config, _ = environment.verify(config)
    lang = localization.notification_language(args)
    values = {"configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "holdSeconds": args.monitoring_hold_seconds or 120,
              "ipamHighPercent": args.monitoring_cilium_ipam_percent,
              "bpfPressurePercent": args.monitoring_cilium_bpf_pressure_percent,
              "agentSummary": translate(lang, "ops.monitoring.cilium_agent_summary"),
              "agentDescription": translate(lang, "ops.monitoring.cilium_agent_description"),
              "operatorSummary": translate(lang, "ops.monitoring.cilium_operator_summary"),
              "operatorDescription": translate(lang, "ops.monitoring.cilium_operator_description"),
              "localized": {
                  "ipamMissingSummary": translate(lang, "ops.monitoring.cilium_ipam_missing_summary"),
                  "ipamMissingDescription": translate(lang, "ops.monitoring.cilium_ipam_missing_description"),
                  "ipamSummary": translate(lang, "ops.monitoring.cilium_ipam_summary"),
                  "ipamDescription": translate(lang, "ops.monitoring.cilium_ipam_description"),
                  "bpfSummary": translate(lang, "ops.monitoring.cilium_bpf_summary"),
                  "bpfDescription": translate(lang, "ops.monitoring.cilium_bpf_description"),
                  "connectivitySummary": translate(lang, "ops.monitoring.cilium_connectivity_summary"),
                  "connectivityDescription": translate(lang, "ops.monitoring.cilium_connectivity_description")}}
    wanted = _manifests(values)
    values["expected"] = cilium_metrics.capture(environment.client, values["namespace"],
                                                values["prometheusName"], wanted)
    return parameters(values)


def preview(value):
    values = parameters(value)
    return (("ops.monitoring.cilium_preview", (values["namespace"], values["prometheusName"],
                                                  values["holdSeconds"], values["ipamHighPercent"],
                                                  values["bpfPressurePercent"])),
            ("ops.monitoring.cilium_warning", ()),
            ("ops.monitoring.rule_warning", ()))


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    wanted = _manifests(values)
    observed = cilium_metrics.capture(environment.client, values["namespace"], values["prometheusName"], wanted)
    if observed != values["expected"]:
        raise OperationError("ops.change.stale")
    return {"configurationRevision": revision(config), "target": observed,
            "desiredSpecHash": fingerprint([item["spec"] for item in wanted.values()])}


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    wanted = _manifests(values)
    return cilium_metrics.publish(environment.client, values["namespace"], values["prometheusName"],
                                  wanted, values["expected"], checkpoint)
