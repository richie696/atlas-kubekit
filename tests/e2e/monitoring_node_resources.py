#!/usr/bin/env python3
"""Exercise configurable node CPU, memory, filesystem and inode alerts with promtool."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.monitoring.rules import node_readiness_rule
from tests.e2e.monitoring_prometheus import cleanup_owned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    if parser.parse_args().confirm != "atlas-monitor-e2e":
        parser.error("Explicit confirmation atlas-monitor-e2e is required")

    suffix = uuid.uuid4().hex[:12]
    container = "atlas-node-resource-promtool-" + suffix
    network = "atlas-unused-" + suffix
    messages = {
        "cpu": {"summary": "CPU high", "description": "Check CPU contention.",
                "missing_summary": "CPU missing", "missing_description": "Check CPU telemetry."},
        "memory": {"summary": "Memory high", "description": "Check memory pressure.",
                   "missing_summary": "Memory missing", "missing_description": "Check memory telemetry."},
        "filesystem": {"summary": "Filesystem high", "description": "Check filesystem capacity.",
                       "missing_summary": "Filesystem missing", "missing_description": "Check filesystem telemetry."},
        "inode": {"summary": "Inodes high", "description": "Check inode use.",
                  "missing_summary": "Inodes missing", "missing_description": "Check inode telemetry."},
    }
    # Test samples use a 1m scrape cadence; allow them to remain fresh through
    # two missed scrapes while still exercising the configurable age boundary.
    rendered = node_readiness_rule("monitoring", "stack", ["cp1"], 1, 120, "warning",
                                   "Node readiness", "Check readiness telemetry.",
                                   80, 80, 80, 80, messages)

    def expected(alert, risk, summary, description, extra=None, observed=None):
        return {"exp_labels": {"node": "cp1", "risk_id": risk, "severity": "warning",
                               "instance": "cp1:9100", "nodename": "cp1", **(extra or {})},
                "exp_annotations": {"summary": summary, "description": description,
                                    **({"observed_value": observed} if observed is not None else {})}}

    def timeline(alert, firing, labels):
        exp = expected(alert, labels[0], labels[1], labels[2], labels[3] if len(labels) > 3 else None,
                       labels[4] if len(labels) > 4 else None)
        return [{"eval_time": str(t) + "s", "alertname": alert,
                 "exp_alerts": [exp] if active else []} for t, active in firing]

    cpu_name = 'node_cpu_seconds_total{instance="cp1:9100",cpu="0",mode="idle"}'
    memory_available = 'node_memory_MemAvailable_bytes{instance="cp1:9100"}'
    memory_total = 'node_memory_MemTotal_bytes{instance="cp1:9100"}'
    fs = 'node_filesystem_avail_bytes{device="/dev/sda1",fstype="ext4",instance="cp1:9100",mountpoint="/"}'
    fs_size = 'node_filesystem_size_bytes{device="/dev/sda1",fstype="ext4",instance="cp1:9100",mountpoint="/"}'
    inode_free = 'node_filesystem_files_free{device="/dev/sda1",fstype="ext4",instance="cp1:9100",mountpoint="/"}'
    inode_total = 'node_filesystem_files{device="/dev/sda1",fstype="ext4",instance="cp1:9100",mountpoint="/"}'
    identity = 'node_uname_info{instance="cp1:9100",nodename="cp1"}'

    tests = [
        {"name": "CPU threshold fires and recovers", "interval": "1m",
         "input_series": [{"series": identity, "values": "1 1 1 1 1 1"},
                          {"series": cpu_name, "values": "0 0.1 0.2 60.2 120.2 180.2"}],
         "alert_rule_test": timeline("AtlasNodeCPUUsageHigh", [(0, False), (60, False), (120, True),
                                                                (180, False)],
                                      ("R-NODE-02", "CPU high", "Check CPU contention.", None,
                                       "99.93333333333332%"))},
        {"name": "memory threshold fires and recovers", "interval": "1s",
         "input_series": [{"series": identity, "values": "1 1 1 1"},
                          {"series": memory_available, "values": "10 10 30 30"},
                          {"series": memory_total, "values": "100 100 100 100"}],
         "alert_rule_test": timeline("AtlasNodeMemoryUsageHigh", [(0, False), (1, True), (2, False)],
                                      ("R-NODE-03", "Memory high", "Check memory pressure.", None, "90%"))},
        {"name": "root filesystem threshold fires and recovers", "interval": "1s",
         "input_series": [{"series": identity, "values": "1 1 1 1"},
                          {"series": fs, "values": "10 10 30 30"},
                          {"series": fs_size, "values": "100 100 100 100"}],
         "alert_rule_test": timeline("AtlasNodeFilesystemUsageHigh", [(0, False), (1, True), (2, False)],
                                      ("R-NODE-04", "Filesystem high", "Check filesystem capacity.", None, "90%"))},
        {"name": "root inode threshold fires and recovers", "interval": "1s",
         "input_series": [{"series": identity, "values": "1 1 1 1"},
                          {"series": inode_free, "values": "10 10 30 30"},
                          {"series": inode_total, "values": "100 100 100 100"}],
         "alert_rule_test": timeline("AtlasNodeInodeUsageHigh", [(0, False), (1, True), (2, False)],
                                      ("R-NODE-04", "Inodes high", "Check inode use.", None, "90%"))},
        {"name": "missing CPU series fires and clears when restored", "interval": "1s",
         "input_series": [{"series": identity, "values": "1 1 1 1"},
                          {"series": cpu_name, "values": "_ _ 0 0.1"}],
         "alert_rule_test": timeline("AtlasNodeCPUTelemetryMissing", [(0, False), (1, True), (2, False)],
                                      ("R-NODE-02", "CPU missing", "Check CPU telemetry.",
                                             {"instance": "cp1:9100", "nodename": "cp1"}))},
        {"name": "stale CPU series fires before instant-vector lookback expires", "interval": "1m",
         "input_series": [{"series": identity, "values": "1 1 1 1 1 1"},
                          {"series": cpu_name, "values": "0 0.1 _ _ _ 0.2"}],
         "alert_rule_test": timeline("AtlasNodeCPUTelemetryMissing", [(0, False), (120, False),
                                                                         (180, False), (240, True),
                                                                         (300, False)],
                                      ("R-NODE-02", "CPU missing", "Check CPU telemetry.",
                                       {"instance": "cp1:9100", "nodename": "cp1"}))},
        {"name": "missing memory denominator fires", "interval": "1s",
         "input_series": [{"series": identity, "values": "1 1 1"},
                          {"series": memory_available, "values": "10 10 10"}],
         "alert_rule_test": timeline("AtlasNodeMemoryTelemetryMissing", [(0, False), (1, True)],
                                      ("R-NODE-03", "Memory missing", "Check memory telemetry.",
                                             {"instance": "cp1:9100", "nodename": "cp1"}))},
    ]
    fixture = {"rule_files": ["rules.json"], "evaluation_interval": "1s", "tests": tests}
    try:
        temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="atlas-node-resource-", dir=temp_root) as directory:
            root = Path(directory).resolve()
            (root / "rules.json").write_text(json.dumps(rendered["spec"]))
            (root / "test.json").write_text(json.dumps(fixture))
            result = subprocess.run(["docker", "run", "--rm", "--pull=never", "--name", container,
                "--label", "atlas-kubekit.e2e=" + suffix, "--network", "none", "-v", str(root) + ":/tmp/nr:ro",
                "--entrypoint=/bin/promtool", "prom/prometheus:v3.14.0", "test", "rules", "/tmp/nr/test.json"],
                capture_output=True, text=True, timeout=45)
            print(result.stdout, end="")
            if result.returncode:
                print(result.stderr, file=sys.stderr, end="")
                raise RuntimeError("node resource promtool verification failed")
    finally:
        cleanup_owned(suffix, network, (container,))
        print("Owned temporary container cleaned up.")


if __name__ == "__main__":
    main()
