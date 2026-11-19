#!/usr/bin/env python3
"""Verify CoreDNS scrape and response-error rules with real promtool."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.monitoring.scrape_rules import core_dns_rules
from tests.e2e.monitoring_prometheus import cleanup_owned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    if parser.parse_args().confirm != "atlas-monitor-e2e":
        parser.error("Explicit confirmation atlas-monitor-e2e is required")
    suffix = uuid.uuid4().hex[:12]
    name, network = "atlas-dns-promtool-" + suffix, "atlas-dns-unused-" + suffix
    summary = "CoreDNS 指标目标不可用"
    description = "请检查 CoreDNS 指标采集。"
    error_summary = "CoreDNS 异常响应比例过高"
    error_description = "SERVFAIL/REFUSED 占 DNS 响应的 5% 以上。"
    latency_summary = "CoreDNS P95 查询延迟过高"
    latency_description = "P95 查询延迟超过 100 毫秒；统计窗口 5 秒内至少 100 次查询。"
    rule = core_dns_rules("monitoring", "stack", [{"job": "coredns", "instance": "dns-0:9153"}],
        1, 120, "warning", 5, 5, 100, summary, description, error_summary, error_description,
        latency_summary, latency_description, {"namespace": "kube-system", "name": "coredns"}, 2,
        "CoreDNS 副本不足", "CoreDNS 至少需要 2 个可用副本。")
    endpoint_alert = {"exp_labels": {"job": "coredns", "instance": "dns-0:9153",
        "risk_id": "R-NET-01", "severity": "warning"},
        "exp_annotations": {"summary": summary, "description": description}}
    ratio_alert = {"exp_labels": {"job": "coredns", "instance": "dns-0:9153",
        "risk_id": "R-NET-01", "severity": "warning"},
        "exp_annotations": {"summary": error_summary, "description": error_description}}
    latency_alert = {"exp_labels": {"job": "coredns", "instance": "dns-0:9153",
        "risk_id": "R-NET-01", "severity": "warning"},
        "exp_annotations": {"summary": latency_summary, "description": latency_description,
                            "observed_value": "0.300s"}}
    replica_alert = {"exp_labels": {"namespace": "kube-system", "deployment": "coredns",
        "minimum_replicas": "2", "risk_id": "R-NET-01", "severity": "warning"},
        "exp_annotations": {"summary": "CoreDNS 副本不足", "description": "CoreDNS 至少需要 2 个可用副本。"}}
    ratio_values = {2: "10.00%", 3: "10.00%", 8: "5.26%"}
    fixture = {"rule_files": ["rules.json"], "evaluation_interval": "1s", "tests": [
        {"name": "missing CoreDNS metrics target", "interval": "1s", "input_series": [],
         "alert_rule_test": [{"eval_time": "0s", "alertname": "AtlasCoreDNSTargetOrTelemetryMissing", "exp_alerts": []},
                             {"eval_time": "1s", "alertname": "AtlasCoreDNSTargetOrTelemetryMissing", "exp_alerts": [endpoint_alert]},
                             {"eval_time": "1s", "alertname": "AtlasCoreDNSReplicasBelowMinimum", "exp_alerts": [replica_alert]}]},
        {"name": "CoreDNS replica minimum handles duplicate kube-state-metrics scrapers", "interval": "1s", "input_series": [
            {"series": 'kube_deployment_status_replicas_available{namespace="kube-system",deployment="coredns",job="ksm-a"}',
             "values": "2 2 1 1 2 2"},
            {"series": 'kube_deployment_status_replicas_available{namespace="kube-system",deployment="coredns",job="ksm-b"}',
             "values": "2 2 2 2 2 2"}],
         "alert_rule_test": [
             {"eval_time": "0s", "alertname": "AtlasCoreDNSReplicasBelowMinimum", "exp_alerts": []},
             {"eval_time": "2s", "alertname": "AtlasCoreDNSReplicasBelowMinimum", "exp_alerts": []},
             {"eval_time": "3s", "alertname": "AtlasCoreDNSReplicasBelowMinimum", "exp_alerts": [replica_alert]},
             {"eval_time": "4s", "alertname": "AtlasCoreDNSReplicasBelowMinimum", "exp_alerts": []}]},
        {"name": "zero traffic does not alert", "interval": "1s", "input_series": [
            {"series": 'coredns_build_info{job="coredns",instance="dns-0:9153",version="1.12.0"}', "values": "1 1 1 1"},
            {"series": 'coredns_dns_responses_total{job="coredns",instance="dns-0:9153",rcode="NOERROR"}', "values": "0 0 0 0"},
            {"series": 'coredns_dns_request_duration_seconds_bucket{job="coredns",instance="dns-0:9153",le="0.1"}', "values": "0 0 0 0"},
            {"series": 'coredns_dns_request_duration_seconds_bucket{job="coredns",instance="dns-0:9153",le="0.5"}', "values": "0 0 0 0"},
            {"series": 'coredns_dns_request_duration_seconds_bucket{job="coredns",instance="dns-0:9153",le="+Inf"}', "values": "0 0 0 0"},
            {"series": 'coredns_dns_request_duration_seconds_count{job="coredns",instance="dns-0:9153"}', "values": "0 0 0 0"}],
         "alert_rule_test": [{"eval_time": str(t) + "s", "alertname": "AtlasCoreDNSResponseErrorRateHigh", "exp_alerts": []}
                             for t in (0, 1, 2, 3)] +
                            [{"eval_time": str(t) + "s", "alertname": "AtlasCoreDNSP95LatencyHigh", "exp_alerts": []}
                             for t in (0, 1, 2, 3)]},
        {"name": "low request volume suppresses unstable latency quantiles", "interval": "1s", "input_series": [
            {"series": 'coredns_build_info{job="coredns",instance="dns-0:9153",version="1.12.0"}',
             "values": "1 1 1 1 1 1 1"},
            {"series": 'coredns_dns_request_duration_seconds_bucket{job="coredns",instance="dns-0:9153",le="0.1"}',
             "values": "0 9 18 27 36 45 54"},
            {"series": 'coredns_dns_request_duration_seconds_bucket{job="coredns",instance="dns-0:9153",le="0.5"}',
             "values": "0 10 20 30 40 50 60"},
            {"series": 'coredns_dns_request_duration_seconds_bucket{job="coredns",instance="dns-0:9153",le="+Inf"}',
             "values": "0 10 20 30 40 50 60"},
            {"series": 'coredns_dns_request_duration_seconds_count{job="coredns",instance="dns-0:9153"}',
             "values": "0 10 20 30 40 50 60"}],
         "alert_rule_test": [{"eval_time": str(t) + "s", "alertname": "AtlasCoreDNSP95LatencyHigh", "exp_alerts": []}
                             for t in (2, 3, 4, 5, 6)]},
        {"name": "high SERVFAIL ratio fires and recovers", "interval": "1s", "input_series": [
            {"series": 'coredns_build_info{job="coredns",instance="dns-0:9153",version="1.12.0"}',
             "values": "1 1 1 1 1 1 1 1 1 1 1 1"},
            {"series": 'coredns_dns_responses_total{job="coredns",instance="dns-0:9153",rcode="NOERROR"}',
             "values": "0 90 180 270 360 450 540 630 720 810 900 990"},
            {"series": 'coredns_dns_responses_total{job="coredns",instance="dns-0:9153",rcode="SERVFAIL"}',
             "values": "0 10 20 30 40 50 60 60 60 60 60 60"}],
         "alert_rule_test": [{"eval_time": str(t) + "s", "alertname": "AtlasCoreDNSResponseErrorRateHigh",
             "exp_alerts": [dict(ratio_alert, exp_annotations=dict(
                 ratio_alert["exp_annotations"], observed_value=ratio_values[t]))] if firing else []}
             for t, firing in ((0, False), (1, False), (2, True), (3, True), (8, True), (10, False))]},
        {"name": "P95 latency fires and recovers after histogram window clears", "interval": "1s", "input_series": [
            {"series": 'coredns_build_info{job="coredns",instance="dns-0:9153",version="1.12.0"}',
             "values": "1 1 1 1 1 1 1 1 1 1 1 1 1"},
            {"series": 'coredns_dns_request_duration_seconds_bucket{job="coredns",instance="dns-0:9153",le="0.1"}',
             "values": "0 90 180 270 360 460 560 660 760 860 960 1060 1160"},
            {"series": 'coredns_dns_request_duration_seconds_bucket{job="coredns",instance="dns-0:9153",le="0.5"}',
             "values": "0 100 200 300 400 500 600 700 800 900 1000 1100 1200"},
            {"series": 'coredns_dns_request_duration_seconds_bucket{job="coredns",instance="dns-0:9153",le="+Inf"}',
             "values": "0 100 200 300 400 500 600 700 800 900 1000 1100 1200"},
            {"series": 'coredns_dns_request_duration_seconds_count{job="coredns",instance="dns-0:9153"}',
             "values": "0 100 200 300 400 500 600 700 800 900 1000 1100 1200"}],
         "alert_rule_test": [{"eval_time": "0s", "alertname": "AtlasCoreDNSP95LatencyHigh", "exp_alerts": []},
                             {"eval_time": "2s", "alertname": "AtlasCoreDNSP95LatencyHigh", "exp_alerts": [latency_alert]},
                             {"eval_time": "8s", "alertname": "AtlasCoreDNSP95LatencyHigh", "exp_alerts": []}]},
    ]}
    try:
        temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="atlas-dns-rules-", dir=temp_root) as directory:
            root = Path(directory).resolve()
            (root / "rules.json").write_text(json.dumps(rule["spec"]))
            (root / "test.json").write_text(json.dumps(fixture))
            result = subprocess.run(["docker", "run", "--rm", "--pull=never", "--name", name,
                "--label", "atlas-kubekit.e2e=" + suffix, "--network", "none", "-v", str(root) + ":/tmp/dns:ro",
                "--entrypoint=/bin/promtool", "prom/prometheus:v3.5.0", "test", "rules", "/tmp/dns/test.json"],
                capture_output=True, text=True, timeout=45)
            print(result.stdout, end="")
            if result.returncode:
                print(result.stderr, file=sys.stderr, end="")
                raise RuntimeError("CoreDNS promtool verification failed")
    finally:
        cleanup_owned(suffix, network, (name,))
        print("Owned temporary container cleaned up.")


if __name__ == "__main__":
    main()
