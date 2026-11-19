#!/usr/bin/env python3
"""Evaluate Longhorn disk capacity/readiness alerts with Prometheus promtool."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.monitoring.longhorn_disk_rules import disk_capacity_rules
from tests.e2e.monitoring_prometheus import cleanup_owned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    if parser.parse_args().confirm != "atlas-monitor-e2e":
        parser.error("Explicit confirmation atlas-monitor-e2e is required")
    suffix = uuid.uuid4().hex[:12]
    name, network = "atlas-longhorn-disk-promtool-" + suffix, "atlas-longhorn-disk-unused-" + suffix
    summaries = {"capacity": "磁盘使用率过高", "not_ready": "磁盘未就绪", "missing": "磁盘指标缺失",
                 "partial": "磁盘指标不完整"}
    descriptions = {"capacity": "检查副本重建空间。", "not_ready": "检查磁盘和节点。", "missing": "检查指标采集。",
                    "partial": "核对磁盘容量、用量和就绪指标。"}
    rule = disk_capacity_rules("monitoring", "stack", 70, 1, 5, "warning", summaries, descriptions)
    high = {"exp_labels": {"node": "worker-a", "disk": "disk-a", "risk_id": "R-STOR-04", "severity": "warning"},
            "exp_annotations": {"summary": summaries["capacity"], "description": descriptions["capacity"],
                                "observed_value": "80%"}}
    not_ready = {"exp_labels": {"node": "worker-b", "disk": "disk-b", "risk_id": "R-STOR-04", "severity": "warning"},
                 "exp_annotations": {"summary": summaries["not_ready"], "description": descriptions["not_ready"]}}
    missing = {"exp_labels": {"risk_id": "R-STOR-04", "severity": "warning"},
               "exp_annotations": {"summary": summaries["missing"], "description": descriptions["missing"]}}
    partial = {"exp_labels": {"node": "worker-c", "disk": "disk-c", "risk_id": "R-STOR-04", "severity": "warning"},
               "exp_annotations": {"summary": summaries["partial"], "description": descriptions["partial"]}}
    fixture = {"rule_files": ["rules.json"], "evaluation_interval": "1s", "tests": [
        {"name": "replicated manager metrics are deduplicated; high usage fires then recovers", "interval": "1s",
         "input_series": [
             {"series": 'longhorn_disk_usage_bytes{disk="disk-a",node="worker-a",source="manager-a"}', "values": "80 80 80 50 50"},
             {"series": 'longhorn_disk_usage_bytes{disk="disk-a",node="worker-a",source="manager-b"}', "values": "80 80 80 50 50"},
             {"series": 'longhorn_disk_capacity_bytes{disk="disk-a",node="worker-a",source="manager-a"}', "values": "100 100 100 100 100"},
             {"series": 'longhorn_disk_capacity_bytes{disk="disk-a",node="worker-a",source="manager-b"}', "values": "100 100 100 100 100"},
             {"series": 'longhorn_disk_status{condition="ready",disk="disk-a",node="worker-a",source="manager-a"}', "values": "1 1 1 1 1"}],
         "alert_rule_test": [
             {"eval_time": "0s", "alertname": "AtlasLonghornDiskUsageHigh", "exp_alerts": []},
             {"eval_time": "1s", "alertname": "AtlasLonghornDiskUsageHigh", "exp_alerts": [high]},
             {"eval_time": "2s", "alertname": "AtlasLonghornDiskUsageHigh", "exp_alerts": [high]},
             {"eval_time": "3s", "alertname": "AtlasLonghornDiskUsageHigh", "exp_alerts": []}]},
        {"name": "not-ready disk fires and recovers", "interval": "1s",
         "input_series": [
             {"series": 'longhorn_disk_status{condition="ready",disk="disk-b",node="worker-b"}', "values": "1 0 0 1"}],
         "alert_rule_test": [
             {"eval_time": "0s", "alertname": "AtlasLonghornDiskNotReady", "exp_alerts": []},
             {"eval_time": "2s", "alertname": "AtlasLonghornDiskNotReady", "exp_alerts": [not_ready]},
             {"eval_time": "3s", "alertname": "AtlasLonghornDiskNotReady", "exp_alerts": []}]},
        {"name": "complete disk telemetry absence alerts", "interval": "1s", "input_series": [],
         "alert_rule_test": [
             {"eval_time": "0s", "alertname": "AtlasLonghornDiskMetricsMissing", "exp_alerts": []},
             {"eval_time": "1s", "alertname": "AtlasLonghornDiskMetricsMissing", "exp_alerts": [missing]}]},
        {"name": "one disk missing capacity while other disks remain fully observable", "interval": "1s",
         "input_series": [
             {"series": 'longhorn_disk_usage_bytes{disk="disk-a",node="worker-a"}', "values": "20 20"},
             {"series": 'longhorn_disk_capacity_bytes{disk="disk-a",node="worker-a"}', "values": "100 100"},
             {"series": 'longhorn_disk_status{condition="ready",disk="disk-a",node="worker-a"}', "values": "1 1"},
             {"series": 'longhorn_disk_usage_bytes{disk="disk-c",node="worker-c"}', "values": "20 20"},
             {"series": 'longhorn_disk_status{condition="ready",disk="disk-c",node="worker-c"}', "values": "1 1"}],
         "alert_rule_test": [
             {"eval_time": "0s", "alertname": "AtlasLonghornDiskTelemetryPartial", "exp_alerts": []},
             {"eval_time": "1s", "alertname": "AtlasLonghornDiskTelemetryPartial", "exp_alerts": [partial]}]},
        {"name": "old disk metrics become missing instead of remaining healthy", "interval": "1s",
         "input_series": [
             {"series": 'longhorn_disk_usage_bytes{disk="disk-a",node="worker-a"}', "values": "20"},
             {"series": 'longhorn_disk_capacity_bytes{disk="disk-a",node="worker-a"}', "values": "100"},
             {"series": 'longhorn_disk_status{condition="ready",disk="disk-a",node="worker-a"}', "values": "1"}],
         "alert_rule_test": [
             {"eval_time": "0s", "alertname": "AtlasLonghornDiskMetricsMissing", "exp_alerts": []},
             {"eval_time": "7s", "alertname": "AtlasLonghornDiskMetricsMissing", "exp_alerts": [missing]}]},
    ]}
    try:
        temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="atlas-longhorn-disk-", dir=temp_root) as directory:
            root = Path(directory).resolve()
            (root / "rules.json").write_text(json.dumps(rule["spec"]))
            (root / "test.json").write_text(json.dumps(fixture))
            result = subprocess.run(["docker", "run", "--rm", "--pull=never", "--name", name,
                "--label", "atlas-kubekit.e2e=" + suffix, "--network", "none", "-v", str(root) + ":/tmp/lh:ro",
                "--entrypoint=/bin/promtool", "prom/prometheus:v3.5.0", "test", "rules", "/tmp/lh/test.json"],
                capture_output=True, text=True, timeout=45)
            print(result.stdout, end="")
            if result.returncode:
                print(result.stderr, file=sys.stderr, end="")
                raise RuntimeError("Longhorn disk promtool verification failed")
    finally:
        cleanup_owned(suffix, network, (name,))
        print("Owned temporary container cleaned up.")


if __name__ == "__main__":
    main()
