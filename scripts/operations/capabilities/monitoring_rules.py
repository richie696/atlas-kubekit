# SPDX-License-Identifier: MIT
"""Plan an explicitly owned node-readiness rule through the shared change protocol."""
import copy
import re

from scripts.lib.i18n import translate
from ..adapters.kubernetes import resource_name
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import localization, publication, rules


def _manifest(values):
    try:
        return rules.node_readiness_rule(values["namespace"], values["release"], values["nodes"],
                                         values["holdSeconds"], values["maxAgeSeconds"],
                                         values["severity"], values["summary"], values["description"],
                                         values["cpuPercent"], values["memoryPercent"],
                                         values["filesystemPercent"], values["inodePercent"],
                                         values["resourceMessages"])
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def _expected(value):
    if not isinstance(value, dict) or set(value) != {"prometheus", "rule"}:
        raise OperationError("ops.change.invalid_parameters")
    for identity, fields in ((value["prometheus"], {"uid", "specHash"}),
                             (value["rule"], {"uid", "specHash"})):
        if identity is None and fields == {"uid", "specHash"}:
            continue
        if (not isinstance(identity, dict) or set(identity) != fields or
                not all(isinstance(v, str) and 0 < len(v) <= 128 for v in identity.values()) or
                ("specHash" in fields and not re.fullmatch(r"[a-f0-9]{64}", identity["specHash"]))):
            raise OperationError("ops.change.invalid_parameters")
    return copy.deepcopy(value)


def parameters(value):
    required = {"configuration", "namespace", "release", "prometheusName", "nodes", "holdSeconds",
                "maxAgeSeconds", "severity", "summary", "description", "cpuPercent", "memoryPercent",
                "filesystemPercent", "inodePercent", "resourceMessages", "expected"}
    if not isinstance(value, dict) or set(value) != required:
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result["configuration"] = normalize(value["configuration"])
    if not isinstance(value["prometheusName"], str):
        raise OperationError("ops.change.invalid_parameters")
    resource_name(value["prometheusName"])
    result["expected"] = _expected(value["expected"])
    _manifest(result)
    expected_nodes = sorted(node["name"] for node in result["configuration"]["nodes"]
                            if not node["name"].startswith("lb"))
    if sorted(result["nodes"]) != expected_nodes:
        raise OperationError("ops.lifecycle.inventory")
    return result


def from_arguments(args, configuration, environment):
    config, _ = environment.verify(configuration)
    lang = localization.notification_language(args)
    resource_messages = {}
    for key in ("cpu", "memory", "filesystem", "inode"):
        resource_messages[key] = {
            "summary": translate(lang, "ops.monitoring.node_resource." + key + ".summary"),
            "description": translate(lang, "ops.monitoring.node_resource." + key + ".description"),
            "missing_summary": translate(lang, "ops.monitoring.node_resource." + key + ".missing_summary"),
            "missing_description": translate(lang, "ops.monitoring.node_resource." + key + ".missing_description"),
        }
    values = {"configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "nodes": sorted(node["name"] for node in config["nodes"] if not node["name"].startswith("lb")),
              "holdSeconds": args.monitoring_hold_seconds, "maxAgeSeconds": args.monitoring_max_age,
              "severity": args.monitoring_severity,
              "summary": translate(lang, "ops.monitoring.rule_summary"),
              "description": translate(lang, "ops.monitoring.rule_description"),
              "cpuPercent": args.monitoring_node_cpu_percent,
              "memoryPercent": args.monitoring_node_memory_percent,
              "filesystemPercent": args.monitoring_node_filesystem_percent,
              "inodePercent": args.monitoring_node_inode_percent,
              "resourceMessages": resource_messages}
    manifest = _manifest(values)
    values["expected"] = publication.capture(environment.client, manifest, values["prometheusName"])
    return parameters(values)


def preview(values):
    values = parameters(values)
    return (("ops.monitoring.rule_preview", (len(values["nodes"]), values["namespace"],
                                               values["prometheusName"], values["holdSeconds"],
                                               values["maxAgeSeconds"], values["cpuPercent"],
                                               values["memoryPercent"], values["filesystemPercent"],
                                               values["inodePercent"])),
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
    checkpoint("monitoring-rules")
    return publication.publish(environment.client, _manifest(values), values["prometheusName"],
                               values["expected"])
