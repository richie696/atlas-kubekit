# SPDX-License-Identifier: MIT
"""Render network alerts for an explicitly registered node-exporter inventory."""
import json
import re

from .prometheus import _metric_selector
from .rules import _DNS_LABEL, _dns_name, _message

RESOURCE = "atlas-node-network"
MAX_INTERFACES = 128
# Prometheus Operator budgets 512 KiB per rule file. Leave room for its YAML serialization.
MAX_SPEC_BYTES = 448 * 1024
THRESHOLDS = {
    "bandwidthPercent": (1, 100),
    "dropBasisPoints": (1, 10000),
    "retransmitBasisPoints": (1, 10000),
    "listenDrops": (1, 1000000000),
    "minPackets": (1, 1000000000),
    "windowSeconds": (60, 3600),
}
MESSAGES = ("bandwidth", "drops", "retransmits", "listen_drops", "telemetry")


def _interfaces(interfaces):
    if not isinstance(interfaces, (list, tuple)) or not 1 <= len(interfaces) <= MAX_INTERFACES:
        raise ValueError("register between 1 and 128 interfaces")
    seen = set()
    for row in interfaces:
        if (not isinstance(row, dict) or set(row) != {"job", "instance", "device", "capacityMbps"} or
                not all(isinstance(row[key], str) and row[key] and len(row[key]) <= 128 and
                        "{{" not in row[key] and "}}" not in row[key] for key in ("job", "instance")) or
                not isinstance(row["device"], str) or
                not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,62}", row["device"])):
            raise ValueError("invalid network interface")
        capacity = row["capacityMbps"]
        if capacity is not None and (type(capacity) is not int or not 1 <= capacity <= 1000000):
            raise ValueError("invalid link capacity")
        identity = (row["job"], row["instance"], row["device"])
        if identity in seen:
            raise ValueError("duplicate network interface")
        seen.add(identity)
    return sorted(interfaces, key=lambda row: (row["job"], row["instance"], row["device"]))


def _sample(metric, labels, age, window=None):
    selector = _metric_selector(metric, labels)
    timestamp = f"time() - timestamp({selector})"
    freshness = f"(({timestamp} <= {age}) and ({timestamp} >= -10))"
    if window is None:
        measured = selector
    else:
        measured = f"rate({selector}[{window}s])"
    valid = f"(({measured} >= 0) and ({measured} < +Inf))"
    return f"({valid} and {freshness} and on() (count({selector}) == 1))"


def _missing(signals):
    # All required signals need one independent, fresh, valid series, including on idle links.
    present = " and on() ".join(signals)
    return f"vector(1) unless on() ({present})"


def network_rules(namespace, release, interfaces, thresholds, hold, age, severity, messages, ruleset="default"):
    if (not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace) or
            not isinstance(release, str) or len(release) > 53 or not _dns_name(release) or
            not isinstance(ruleset, str) or len(ruleset) > 44 or not _DNS_LABEL.fullmatch(ruleset) or
            type(hold) is not int or not 1 <= hold <= 86400 or
            type(age) is not int or not 5 <= age <= 86400 or
            severity not in ("critical", "warning", "info") or not isinstance(thresholds, dict) or
            set(thresholds) != set(THRESHOLDS) or any(
                type(thresholds[key]) is not int or not low <= thresholds[key] <= high
                for key, (low, high) in THRESHOLDS.items()) or
            not isinstance(messages, dict) or set(messages) != set(MESSAGES) or any(
                not isinstance(message, dict) or set(message) != {"summary", "description"} or
                not all(_message(value) for value in message.values()) for message in messages.values())):
        raise ValueError("invalid network rule configuration")

    rules = []
    window = thresholds["windowSeconds"]

    def add(name, expression, labels, message):
        annotations = messages[message].copy()
        if message != "telemetry":
            units = {"bandwidth": "%", "drops": " bp", "retransmits": " bp", "listen_drops": ""}
            detail = f"{labels['device']} / {labels['direction']}: " if "direction" in labels else ""
            annotations["observed_value"] = detail + '{{ printf "%.2f" $value }}' + units[message]
        if ruleset != "default":
            labels = {**labels, "network_ruleset": ruleset}
        rules.append({"alert": "AtlasNode" + name, "expr": expression, "for": f"{hold}s",
                      "labels": {**labels, "severity": severity, "risk_id": "R-NET-05"},
                      "annotations": annotations})

    nodes = set()
    for interface in _interfaces(interfaces):
        identity = {key: interface[key] for key in ("job", "instance", "device")}
        nodes.add((interface["job"], interface["instance"]))
        capacity = interface["capacityMbps"]
        speed = (str(capacity * 125000) if capacity is not None else
                 f"({_sample('node_network_speed_bytes', identity, age)} > 0)")
        required = [speed] if capacity is None else []
        for direction in ("receive", "transmit"):
            prefix = "node_network_" + direction
            octets = _sample(prefix + "_bytes_total", identity, age, window)
            dropped = _sample(prefix + "_drop_total", identity, age, window)
            packets = _sample(prefix + "_packets_total", identity, age, window)
            required.extend((octets, dropped, packets))
            direction_labels = {**identity, "direction": direction}
            add("NetworkBandwidthHigh", f"({octets} / {speed} * 100) >= {thresholds['bandwidthPercent']}",
                direction_labels, "bandwidth")
            add("NetworkPacketDropHigh",
                f"(({dropped} / {packets} * 10000) >= {thresholds['dropBasisPoints']}) "
                f"and ({packets} * {window} >= {thresholds['minPackets']})", direction_labels, "drops")
        add("NetworkTelemetryMissing", _missing(required), {**identity, "telemetry": "interface"}, "telemetry")

    for job, instance in sorted(nodes):
        labels = {"job": job, "instance": instance}
        retransmits = _sample("node_netstat_Tcp_RetransSegs", labels, age, window)
        segments = _sample("node_netstat_Tcp_OutSegs", labels, age, window)
        listen = _sample("node_netstat_TcpExt_ListenDrops", labels, age, window)
        add("TCPRetransmissionHigh",
            f"(({retransmits} / {segments} * 10000) >= {thresholds['retransmitBasisPoints']}) "
            f"and ({segments} * {window} >= {thresholds['minPackets']})", labels, "retransmits")
        add("TCPListenDropsHigh", f"({listen} * {window}) >= {thresholds['listenDrops']}", labels, "listen_drops")
        add("NetworkTelemetryMissing", _missing((retransmits, segments, listen)),
            {**labels, "telemetry": "tcp"}, "telemetry")

    name = RESOURCE if ruleset == "default" else f"{RESOURCE}-{ruleset}"
    group = "atlas.node.network" if ruleset == "default" else f"atlas.node.network.{ruleset}"
    spec = {"groups": [{"name": group, "rules": rules}]}
    if len(json.dumps(spec, indent=2).encode("utf-8")) > MAX_SPEC_BYTES:
        raise RuleSizeError("network rules exceed the per-file budget")
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": name, "namespace": namespace,
                         "labels": {"release": release, "atlas-kubekit.io/managed-by": "monitoring"}},
            "spec": spec}


class RuleSizeError(ValueError):
    """The generated rules need to be split across named rule sets."""
