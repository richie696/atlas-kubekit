#!/usr/bin/env python3
"""Check the managed logging alerts and recovery with real promtool."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.addons.logging_monitoring import logging_rules


def checks():
    spec = logging_rules(["loki", "alloy"], "zh_CN")["spec"]
    rules = {row["alert"]: row for row in spec["groups"][0]["rules"]}

    def test_alert(name, timestamp, labels=None, observed_value=None):
        rule = rules[name]
        expected = []
        if labels is not None:
            annotations = dict(rule["annotations"])
            if observed_value is not None:
                annotations["observed_value"] = observed_value
            expected.append({"exp_labels": dict(rule["labels"], **labels), "exp_annotations": annotations})
        return {"eval_time": timestamp, "alertname": name, "exp_alerts": expected}

    loki = "AtlasLokiMetricsUnavailable"
    alloy = "AtlasAlloyMetricsUnavailable"
    rejected = "AtlasLokiLogIngestionDiscarded"
    dropped = "AtlasAlloyLogDeliveryDropped"
    test = {"rule_files": ["rules.json"], "evaluation_interval": "30s", "tests": [
        {"name": "missing monitoring targets", "interval": "30s", "input_series": [],
         "alert_rule_test": [
             test_alert(loki, "1m30s"),
             test_alert(loki, "2m", {"job": "atlas-logging-loki"}),
             test_alert(alloy, "2m", {"job": "atlas-logging-alloy"}),
         ]},
        {"name": "scrape failure and recovery", "interval": "30s", "input_series": [
            {"series": 'up{job="atlas-logging-loki",pod="loki-0"}', "values": "1 1 0 0 0 0 0 1"},
         ], "alert_rule_test": [
             test_alert(loki, "2m30s"),
             test_alert(loki, "3m", {"job": "atlas-logging-loki", "pod": "loki-0"}),
             test_alert(loki, "3m30s"),
         ]},
        {"name": "Loki rejects logs and Alloy drops entries once, then recover", "interval": "30s",
         "input_series": [
             {"series": 'loki_discarded_samples_total{job="atlas-logging-loki",namespace="logging",pod="loki-0",reason="rate_limited"}',
              "values": "0 0 3 3 3 3 3 3 3 3 3 3 3"},
             {"series": 'loki_write_dropped_entries_total{job="atlas-logging-alloy",namespace="logging",pod="alloy-1"}',
              "values": "0 0 4 4 4 4 4 4 4 4 4 4 4"},
             {"series": 'loki_write_dropped_entries_total{job="unowned",namespace="logging",pod="ignored"}',
              "values": "0 1 2 3 4 5 6 7 8 9 10 11 12"},
         ], "alert_rule_test": [
             test_alert(rejected, "1m30s"), test_alert(dropped, "1m30s"),
             test_alert(rejected, "2m", {"namespace": "logging", "pod": "loki-0", "reason": "rate_limited"}, "3"),
             test_alert(dropped, "2m", {"namespace": "logging", "pod": "alloy-1"}, "4"),
             test_alert(rejected, "6m"), test_alert(dropped, "6m"),
         ]},
    ]}
    return spec, test


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True, choices=("atlas-monitor-e2e",))
    parser.parse_args()
    spec, scenarios = checks()
    with tempfile.TemporaryDirectory(prefix="atlas-logging-rules-") as directory:
        root = Path(directory).resolve()
        (root / "rules.json").write_text(json.dumps(spec), encoding="utf-8")
        (root / "tests.json").write_text(json.dumps(scenarios), encoding="utf-8")
        subprocess.run(["docker", "run", "--rm", "--pull=never", "--network", "none", "-v", str(root) + ":/tests:ro",
                        "--entrypoint=/bin/promtool", "prom/prometheus:v3.5.0", "test", "rules", "/tests/tests.json"],
                       check=True, timeout=45)


if __name__ == "__main__":
    main()
