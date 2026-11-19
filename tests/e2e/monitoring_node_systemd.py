#!/usr/bin/env python3
"""Evaluate node kubelet/containerd systemd alerts with isolated real promtool."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.addons.monitoring_values import safe_monitoring_values
from scripts.addons.sources import yaml_module
from tests.e2e.monitoring_prometheus import cleanup_owned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    if parser.parse_args().confirm != "atlas-monitor-e2e":
        parser.error("Explicit confirmation atlas-monitor-e2e is required")
    suffix = uuid.uuid4().hex[:12]
    name, network = "atlas-systemd-promtool-" + suffix, "atlas-systemd-unused-" + suffix
    values = yaml_module().safe_load(safe_monitoring_values("zh_CN"))
    rules = values["additionalPrometheusRulesMap"]["atlas-node-systemd"]["groups"][0]["rules"]
    rule_file = {"groups": [{"name": "atlas.node.systemd", "rules": rules}]}
    labels = {"nodename": "test-worker", "risk_id": "R-NODE-06", "severity": "critical"}
    mtime_series = 'node_textfile_mtime_seconds{instance="test-worker:9100",file="/host-textfile/atlas_systemd_units.prom"}'
    tests = [
        {
            "name": "kubelet inactive, sustained alert and recovery",
            "interval": "1m",
            "input_series": [
                {"series": 'node_uname_info{instance="test-worker:9100",nodename="test-worker"}',
                 "values": "1 1 1 1 1"},
                {"series": 'atlas_systemd_unit_active{instance="test-worker:9100",unit="kubelet"}',
                 "values": "0 0 0 1 1"},
                {"series": 'atlas_systemd_unit_probe_success{instance="test-worker:9100",unit="kubelet"}',
                 "values": "1 1 1 1 1"},
                {"series": mtime_series, "values": "0 60 120 180 240"},
            ],
            "alert_rule_test": [
                {"eval_time": "0m", "alertname": "AtlasKubeletServiceUnavailable", "exp_alerts": []},
                {"eval_time": "1m", "alertname": "AtlasKubeletServiceUnavailable", "exp_alerts": []},
                {"eval_time": "2m", "alertname": "AtlasKubeletServiceUnavailable", "exp_alerts": [
                    {"exp_labels": {**labels, "instance": "test-worker:9100", "nodename": "test-worker",
                                    "unit": "kubelet"},
                     "exp_annotations": {
                         "summary": "kubelet 服务未运行或无法读取其状态",
                         "description": "请检查 kubelet 服务及节点日志，并确认主机 systemd 指标定时器正常、node-exporter 正在采集 Atlas textfile 指标。",
                     }}]},
                {"eval_time": "3m", "alertname": "AtlasKubeletServiceUnavailable", "exp_alerts": []},
            ],
        },
        {
            "name": "containerd systemd telemetry missing and recovery",
            "interval": "1m",
            "input_series": [
                {"series": 'node_uname_info{instance="test-worker:9100",nodename="test-worker"}',
                 "values": "1 1 1 1 1"},
                {"series": 'atlas_systemd_unit_probe_success{instance="test-worker:9100",unit="containerd"}',
                 "values": "_ _ _ 1 1"},
                {"series": 'atlas_systemd_unit_active{instance="test-worker:9100",unit="containerd"}',
                 "values": "_ _ _ 1 1"},
                {"series": mtime_series, "values": "_ _ _ 180 240"},
            ],
            "alert_rule_test": [
                {"eval_time": "0m", "alertname": "AtlasContainerdSystemdTelemetryMissing", "exp_alerts": []},
                {"eval_time": "1m", "alertname": "AtlasContainerdSystemdTelemetryMissing", "exp_alerts": []},
                {"eval_time": "2m", "alertname": "AtlasContainerdSystemdTelemetryMissing", "exp_alerts": [
                    {"exp_labels": {**labels, "instance": "test-worker:9100", "nodename": "test-worker"},
                     "exp_annotations": {
                         "summary": "containerd systemd 指标缺失或已过期",
                         "description": "请检查 atlas-node-systemd-metrics.timer、最近一次服务执行结果、主机 textfile 文件及 node-exporter 的 Prometheus 采集目标。",
                     }}]},
                {"eval_time": "3m", "alertname": "AtlasContainerdSystemdTelemetryMissing", "exp_alerts": []},
            ],
        },
    ]
    missing = tests[1]["alert_rule_test"][2]
    tests.append({
        "name": "exporter keeps scraping but the systemd textfile stops updating",
        "interval": "1m",
        "input_series": [
            {**entry, "values": "1 1 1 1 1 1"}
            for entry in tests[1]["input_series"][:3]
        ] + [
            {"series": mtime_series, "values": "0 0 0 0 0 300"},
            {"series": mtime_series.replace("atlas_systemd_units.prom", "other.prom"),
             "values": "0 60 120 180 240 300"},
        ],
        "alert_rule_test": [
            {"eval_time": "1m", "alertname": missing["alertname"], "exp_alerts": []},
            {"eval_time": "3m", "alertname": missing["alertname"], "exp_alerts": []},
            {**missing, "eval_time": "4m"},
            {"eval_time": "5m", "alertname": missing["alertname"], "exp_alerts": []},
        ],
    })
    fixture = {"rule_files": ["rules.json"], "evaluation_interval": "1m", "tests": tests}
    try:
        temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="atlas-systemd-rules-", dir=temp_root) as directory:
            root = Path(directory).resolve()
            (root / "rules.json").write_text(json.dumps(rule_file, ensure_ascii=False))
            (root / "test.json").write_text(json.dumps(fixture, ensure_ascii=False))
            result = subprocess.run([
                "docker", "run", "--rm", "--pull=never", "--name", name,
                "--label", "atlas-kubekit.e2e=" + suffix, "--network", "none",
                "-v", str(root) + ":/tmp/atlas-systemd:ro", "--entrypoint=/bin/promtool",
                "prom/prometheus:v3.14.0", "test", "rules", "/tmp/atlas-systemd/test.json",
            ], capture_output=True, text=True, timeout=45)
            print(result.stdout, end="")
            if result.returncode:
                print(result.stderr, file=sys.stderr, end="")
                raise RuntimeError("node systemd Prometheus rule verification failed")
    finally:
        cleanup_owned(suffix, network, (name,))
        print("Owned temporary container cleaned up.")


if __name__ == "__main__":
    main()
