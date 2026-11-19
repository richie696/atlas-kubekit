#!/usr/bin/env python3
"""Evaluate Atlas node network rules with real Prometheus promtool."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.operations.monitoring.node_network_rules import MESSAGES, network_rules

DEVICE = {"job": "node-exporter", "instance": "cp1:9100", "device": "eth0"}
NODE = {key: DEVICE[key] for key in ("job", "instance")}
THRESHOLDS = {"bandwidthPercent": 80, "dropBasisPoints": 100, "retransmitBasisPoints": 300,
              "listenDrops": 20, "minPackets": 1000, "windowSeconds": 60}
EXPECTED = {
    ("AtlasNodeNetworkBandwidthHigh", "receive"): (DEVICE, 160),
    ("AtlasNodeNetworkPacketDropHigh", "receive"): (DEVICE, 500),
    ("AtlasNodeTCPRetransmissionHigh", ""): (NODE, 500),
    ("AtlasNodeTCPListenDropsHigh", ""): (NODE, 60),
}
OBSERVED_TEXT = {
    "AtlasNodeNetworkBandwidthHigh": "eth0 / receive: 160.00%",
    "AtlasNodeNetworkPacketDropHigh": "eth0 / receive: 500.00 bp",
    "AtlasNodeTCPRetransmissionHigh": "500.00 bp",
    "AtlasNodeTCPListenDropsHigh": "60.00",
}


def label_text(labels):
    return "{" + ",".join(f"{key}={json.dumps(value)}" for key, value in sorted(labels.items())) + "}"


def series():
    increments = {
        "node_network_receive_bytes_total": 30000000, "node_network_transmit_bytes_total": 1500,
        "node_network_receive_packets_total": 1500, "node_network_transmit_packets_total": 15,
        "node_network_receive_drop_total": 75, "node_network_transmit_drop_total": 15,
        "node_netstat_Tcp_RetransSegs": 75, "node_netstat_Tcp_OutSegs": 1500,
        "node_netstat_TcpExt_ListenDrops": 15,
    }
    counters = [
        {"series": metric + label_text(DEVICE if metric.startswith("node_network_") else NODE),
         "values": f"0+{delta}x5 {5 * delta}+0x8"}
        for metric, delta in increments.items()
    ]
    return [*counters, {"series": "node_network_speed_bytes" + label_text(DEVICE), "values": "1250000+0x14"}]


def fixture(manifest):
    rules = manifest["spec"]["groups"][0]["rules"]
    cases = []
    checks = []
    alerts = []
    for rule in rules:
        direction = rule["labels"].get("direction", "")
        expected = EXPECTED.get((rule["alert"], direction))
        checks.append({
            "expr": rule["expr"], "eval_time": "60s",
            "exp_samples": [{"labels": label_text(expected[0]), "value": expected[1]}] if expected else [],
        })
        checks.append({"expr": rule["expr"], "eval_time": "150s", "exp_samples": []})
        if expected:
            annotations = {**rule["annotations"], "observed_value": OBSERVED_TEXT[rule["alert"]]}
            for time, firing in (("20s", False), ("60s", True), ("150s", False)):
                alerts.append({"alertname": rule["alert"], "eval_time": time, "exp_alerts": [
                    {"exp_labels": rule["labels"], "exp_annotations": annotations}
                ] if firing else []})
    cases.append({"name": "high network activity, tiny TX sample, and recovery", "interval": "15s",
                  "input_series": series(), "promql_expr_test": checks, "alert_rule_test": alerts})

    bandwidth = next(rule for rule in rules if rule["alert"] == "AtlasNodeNetworkBandwidthHigh"
                     and rule["labels"]["direction"] == "receive")
    telemetry = next(rule for rule in rules if rule["labels"].get("telemetry") == "interface")
    reset = series()
    reset[0]["values"] = "1000000000 1000000020 1000000040 0 20 40 60"
    cases.append({"name": "counter reset at low utilization", "interval": "15s", "input_series": reset,
                  "promql_expr_test": [
                      {"expr": rule["expr"], "eval_time": "60s", "exp_samples": []}
                      for rule in (bandwidth, telemetry)
                  ]})
    for name, extra in (("stale input", None), ("duplicate source", {**DEVICE, "endpoint": "duplicate"}),
                        ("zero capacity", DEVICE)):
        inputs = series()
        speed = inputs[-1]
        if extra is None:
            inputs[0]["values"] = "0 30000000"
        elif name == "duplicate source":
            inputs.append({**speed, "series": "node_network_speed_bytes" + label_text(extra)})
        else:
            speed["values"] = "0+0x14"
        cases.append({"name": name, "interval": "15s", "input_series": inputs, "promql_expr_test": [
            {"expr": bandwidth["expr"], "eval_time": "60s", "exp_samples": []},
            {"expr": telemetry["expr"], "eval_time": "60s", "exp_samples": [{"labels": "{}", "value": 1}]},
        ]})
    return {"rule_files": ["rules.json"], "evaluation_interval": "1s", "fuzzy_compare": True, "tests": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    if parser.parse_args().confirm != "atlas-monitor-e2e":
        parser.error("Explicit confirmation atlas-monitor-e2e is required")
    messages = {name: {"summary": name, "description": "Inspect the recorded source."} for name in MESSAGES}
    manifest = network_rules("monitoring", "stack", [{**DEVICE, "capacityMbps": None}],
                             THRESHOLDS, 20, 30, "warning", messages)
    temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="atlas-node-network-", dir=temp_root) as directory:
        root = Path(directory)
        (root / "rules.json").write_text(json.dumps(manifest["spec"]), encoding="utf-8")
        (root / "test.json").write_text(json.dumps(fixture(manifest)), encoding="utf-8")
        subprocess.run(["docker", "run", "--rm", "--pull=never", "--network", "none", "-v",
                        f"{root}:/work:ro", "-w", "/work", "--entrypoint", "/bin/promtool",
                        "prom/prometheus:v3.14.0", "test", "rules", "test.json"], check=True, timeout=90)


if __name__ == "__main__":
    main()
