# SPDX-License-Identifier: MIT
"""Plan and publish explicitly registered node network rules."""
import copy
import re

from scripts.lib.i18n import translate
from ..adapters.kubernetes import resource_name
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import localization, node_network_rules, publication
from .monitoring_rules import _expected
from .monitoring_scrapes import _parse_target

NAME = "monitoring-node-network-rules"
OPTIONS = (
    ("bandwidthPercent", "network_bandwidth_percent", 80),
    ("dropBasisPoints", "network_drop_basis_points", 100),
    ("retransmitBasisPoints", "network_retransmit_basis_points", 300),
    ("listenDrops", "network_listen_drops", 20),
    ("minPackets", "network_min_packets", 1000),
    ("windowSeconds", "network_window_seconds", 300),
)
DEFAULT_HOLD = 120
FIELDS = {"configuration", "namespace", "release", "prometheusName", "interfaces", "thresholds", "ruleset",
          "holdSeconds", "maxAgeSeconds", "severity", "messages", "expected"}


def _interface(raw):
    target = _parse_target(raw)
    labels = target.pop("labels", {})
    if "device" not in labels or labels.keys() - {"device", "capacity_mbps"}:
        raise OperationError("ops.change.invalid_parameters")
    capacity = labels.get("capacity_mbps")
    if capacity is not None and not re.fullmatch(r"[0-9]{1,7}", capacity):
        raise OperationError("ops.change.invalid_parameters")
    return {**target, "device": labels["device"], "capacityMbps": int(capacity) if capacity is not None else None}


def _manifest(values):
    try:
        return node_network_rules.network_rules(
            values["namespace"], values["release"], values["interfaces"], values["thresholds"],
            values["holdSeconds"], values["maxAgeSeconds"], values["severity"], values["messages"], values["ruleset"])
    except node_network_rules.RuleSizeError:
        raise OperationError("ops.monitoring.node_network_too_large") from None
    except (ValueError, TypeError, KeyError):
        raise OperationError("ops.change.invalid_parameters") from None


def parameters(value):
    if not isinstance(value, dict) or value.keys() not in (FIELDS, FIELDS - {"ruleset"}):
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result.setdefault("ruleset", "default")
    result["configuration"] = normalize(value["configuration"])
    resource_name(value["prometheusName"])
    result["expected"] = _expected(value["expected"])
    _manifest(result)
    return result


def from_arguments(args, configuration, environment):
    config, _ = environment.verify(configuration)
    lang = localization.notification_language(args)
    thresholds = {key: getattr(args, "monitoring_" + option) for key, option, _ in OPTIONS}
    messages = {name: {field: translate(lang, f"ops.monitoring.network_{name}_{field}")
                       for field in ("summary", "description")} for name in node_network_rules.MESSAGES}
    values = {"configuration": config, "namespace": args.monitoring_namespace,
              "release": args.monitoring_release, "prometheusName": args.monitoring_prometheus_name,
              "interfaces": [_interface(item) for item in (args.monitoring_target or ())],
              "ruleset": args.monitoring_network_ruleset,
              "thresholds": thresholds, "severity": args.monitoring_severity,
              "holdSeconds": DEFAULT_HOLD if args.monitoring_hold_seconds is None else args.monitoring_hold_seconds,
              "maxAgeSeconds": args.monitoring_max_age, "messages": messages}
    values["expected"] = publication.capture(environment.client, _manifest(values), values["prometheusName"])
    return parameters(values)


def preview(value):
    values = parameters(value)
    thresholds = values["thresholds"]
    return (
        ("ops.monitoring.node_network_preview", (values["ruleset"], values["namespace"], values["prometheusName"],
                                                len(values["interfaces"]), values["holdSeconds"],
                                                values["maxAgeSeconds"])),
        ("ops.monitoring.node_network_thresholds", tuple(thresholds[key] for key, _, _ in OPTIONS)),
        *(("ops.monitoring.node_network_interface", (row["job"], row["instance"], row["device"],
                                                    row["capacityMbps"] or "-"))
          for row in values["interfaces"]),
        ("ops.monitoring.rule_warning", ()),
    )


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
    checkpoint(NAME)
    return publication.publish(environment.client, _manifest(values), values["prometheusName"], values["expected"])
