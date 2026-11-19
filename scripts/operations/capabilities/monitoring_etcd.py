# SPDX-License-Identifier: MIT
"""Controlled publication of explicit etcd member leader-signal rules."""
from scripts.lib.i18n import translate
from ..configuration import revision
from ..models import OperationError, fingerprint
from ..monitoring import localization, publication, scrape_rules
from .monitoring_scrapes import parameters


def _manifest(values):
    try:
        return scrape_rules.etcd_leader_rules(values["namespace"], values["release"], values["targets"],
            values["holdSeconds"], values["maxAgeSeconds"], values["severity"], values["summary"], values["description"])
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def from_arguments(args, config, environment):
    config, _ = environment.verify(config)
    targets = []
    for item in args.monitoring_target or []:
        if not isinstance(item, str) or "=" not in item:
            raise OperationError("ops.change.invalid_parameters")
        job, instance = item.split("=", 1)
        targets.append({"job": job, "instance": instance})
    values = {"configuration": config, "namespace": args.monitoring_namespace, "release": args.monitoring_release,
              "prometheusName": args.monitoring_prometheus_name, "targets": targets,
              "holdSeconds": args.monitoring_hold_seconds, "maxAgeSeconds": args.monitoring_max_age,
              "severity": args.monitoring_severity,
              "summary": translate(localization.notification_language(args), "ops.monitoring.etcd_summary"),
              "description": translate(localization.notification_language(args), "ops.monitoring.etcd_description")}
    manifest = _manifest(values)
    values["expected"] = publication.capture(environment.client, manifest, values["prometheusName"])
    return parameters(values)


def preview(value):
    values = parameters(value)
    return (("ops.monitoring.etcd_preview", (len(values["targets"]), values["namespace"],
                values["prometheusName"], values["holdSeconds"], values["maxAgeSeconds"])),
            ("ops.monitoring.scrape_targets_preview", (", ".join(t["job"] + "=" + t["instance"]
                for t in values["targets"]),)), ("ops.monitoring.etcd_warning", ()), ("ops.monitoring.rule_warning", ()))


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    manifest = _manifest(values)
    observed = publication.capture(environment.client, manifest, values["prometheusName"])
    if observed != values["expected"]:
        raise OperationError("ops.change.stale")
    return {"configurationRevision": revision(config), "target": observed, "desiredSpecHash": fingerprint(manifest["spec"])}


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    checkpoint("monitoring-etcd-rules")
    return publication.publish(environment.client, _manifest(values), values["prometheusName"], values["expected"])
