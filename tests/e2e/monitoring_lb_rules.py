#!/usr/bin/env python3
"""Evaluate HAProxy and Keepalived/VIP alert rules with promtool."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.monitoring.lb_rules import load_balancer_rules
from tests.e2e.monitoring_prometheus import cleanup_owned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    if parser.parse_args().confirm != "atlas-monitor-e2e":
        parser.error("Explicit confirmation atlas-monitor-e2e is required")
    suffix = uuid.uuid4().hex[:12]
    name, network = "atlas-lb-rules-promtool-" + suffix, "atlas-lb-rules-unused-" + suffix
    summaries = {key: key for key in ("target", "backend", "capacity", "connection_errors", "queue", "keepalived", "vip_missing", "vip_no_owner", "vip_multiple", "vip_drift")}
    descriptions = {key: "Check LB service, route, and backend." for key in summaries}
    rule = load_balancer_rules("monitoring", "stack", [{"name": "lb1", "address": "10.0.0.11"},
                                                        {"name": "lb2", "address": "10.0.0.12"}], ["10.0.0.9"],
                               2, 30, "warning", 80, 5, 4, summaries, descriptions)
    errors = {"exp_labels": {"node": "lb1", "lb": "lb1", "proxy": "app", "risk_id": "R-LB-02",
                              "severity": "warning"},
              "exp_annotations": {"summary": "connection_errors", "description": descriptions["connection_errors"],
                                  "observed_value": "10"}}
    queued = {"exp_labels": {"node": "lb1", "lb": "lb1", "proxy": "app", "risk_id": "R-LB-03",
                              "severity": "warning"},
              "exp_annotations": {"summary": "queue", "description": descriptions["queue"],
                                  "observed_value": "1"}}
    no_owner = {"exp_labels": {"vip": "10.0.0.9", "risk_id": "R-LB-04", "severity": "warning"},
                "exp_annotations": {"summary": "vip_no_owner", "description": descriptions["vip_no_owner"]}}
    multiple = {"exp_labels": {"vip": "10.0.0.9", "risk_id": "R-LB-04", "severity": "warning"},
                "exp_annotations": {"summary": "vip_multiple", "description": descriptions["vip_multiple"]}}
    drift = {"exp_labels": {"vip": "10.0.0.9", "risk_id": "R-LB-04", "severity": "warning"},
             "exp_annotations": {"summary": "vip_drift", "description": descriptions["vip_drift"],
                                 "observed_value": "10"}}
    keepalived = {"exp_labels": {"node": "lb2", "risk_id": "R-LB-04", "severity": "warning"},
                  "exp_annotations": {"summary": "keepalived", "description": descriptions["keepalived"]}}
    owner_lb1 = 'atlas_keepalived_vip_owner{job="atlas-lb-metrics",metrics_endpoint="vip",lb="lb1",vip="10.0.0.9"}'
    owner_lb2 = 'atlas_keepalived_vip_owner{job="atlas-lb-metrics",metrics_endpoint="vip",lb="lb2",vip="10.0.0.9"}'
    fixture = {"rule_files": ["rules.json"], "evaluation_interval": "1s", "tests": [
        {"name": "backend connection errors cross threshold and recover", "interval": "60s",
         "input_series": [{"series": 'haproxy_backend_connection_errors_total{job="atlas-lb-metrics",lb="lb1",proxy="app"}',
                           "values": "0 2 4 6 8 10 12 12 12 12 12"}],
         "alert_rule_test": [
             {"eval_time": "0s", "alertname": "AtlasLBBackendConnectionErrors", "exp_alerts": []},
             {"eval_time": "5m", "alertname": "AtlasLBBackendConnectionErrors", "exp_alerts": [errors]},
             {"eval_time": "10m", "alertname": "AtlasLBBackendConnectionErrors", "exp_alerts": []}]},
        {"name": "queued backend sessions trigger only while sustained", "interval": "1s",
         "input_series": [{"series": 'haproxy_backend_current_queue{job="atlas-lb-metrics",lb="lb1",proxy="app"}',
                           "values": "0 0 1 1 1 0 0"}],
         "alert_rule_test": [
             {"eval_time": "0s", "alertname": "AtlasLBBackendQueueNonZero", "exp_alerts": []},
             {"eval_time": "4s", "alertname": "AtlasLBBackendQueueNonZero", "exp_alerts": [queued]},
             {"eval_time": "6s", "alertname": "AtlasLBBackendQueueNonZero", "exp_alerts": []}]},
        {"name": "no owner and duplicate VIP owners are detected", "interval": "1s",
         "input_series": [{"series": owner_lb1, "values": "0 0 0 0 0"},
                          {"series": owner_lb2, "values": "0 0 0 0 0"}],
         "alert_rule_test": [
             {"eval_time": "0s", "alertname": "AtlasLBVipNoOwner", "exp_alerts": []},
             {"eval_time": "3s", "alertname": "AtlasLBVipNoOwner", "exp_alerts": [no_owner]}]},
        {"name": "multiple VIP owners are detected", "interval": "1s",
         "input_series": [{"series": owner_lb1, "values": "1 1 1 1 1"},
                          {"series": owner_lb2, "values": "1 1 1 1 1"}],
         "alert_rule_test": [
             {"eval_time": "0s", "alertname": "AtlasLBVipMultipleOwners", "exp_alerts": []},
             {"eval_time": "3s", "alertname": "AtlasLBVipMultipleOwners", "exp_alerts": [multiple]}]},
        {"name": "Keepalived alert requires an inactive service and recovers", "interval": "1s",
         "input_series": [{"series": 'atlas_keepalived_active{job="atlas-lb-metrics",lb="lb1",metrics_endpoint="vip"}',
                           "values": "1 1 1 1 1 1"},
                          {"series": 'atlas_keepalived_active{job="atlas-lb-metrics",lb="lb2",metrics_endpoint="vip"}',
                           "values": "1 0 0 0 0 1 1"}],
         "alert_rule_test": [
             {"eval_time": "0s", "alertname": "AtlasLBKeepalivedInactive", "exp_alerts": []},
             {"eval_time": "4s", "alertname": "AtlasLBKeepalivedInactive", "exp_alerts": [keepalived]},
             {"eval_time": "6s", "alertname": "AtlasLBKeepalivedInactive", "exp_alerts": []}]},
        {"name": "one normal failover does not trip the VIP drift threshold", "interval": "1m",
         "input_series": [{"series": owner_lb1, "values": "1 0 0 0 0 0"},
                          {"series": owner_lb2, "values": "0 1 1 1 1 1"}],
         "alert_rule_test": [
             {"eval_time": "5m", "alertname": "AtlasLBVipOwnershipDrift", "exp_alerts": []}]},
        {"name": "repeated ownership changes exceed the configurable threshold", "interval": "1m",
         "input_series": [{"series": owner_lb1, "values": "1 0 1 0 1 0 1"},
                          {"series": owner_lb2, "values": "0 1 0 1 0 1 0"}],
         "alert_rule_test": [
             {"eval_time": "0s", "alertname": "AtlasLBVipOwnershipDrift", "exp_alerts": []},
             {"eval_time": "5m", "alertname": "AtlasLBVipOwnershipDrift", "exp_alerts": [drift]}]}]}
    try:
        temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="atlas-lb-rules-", dir=temp_root) as directory:
            root = Path(directory).resolve()
            (root / "rules.json").write_text(json.dumps(rule["spec"]))
            (root / "test.json").write_text(json.dumps(fixture))
            result = subprocess.run(["docker", "run", "--rm", "--pull=never", "--name", name,
                "--label", "atlas-kubekit.e2e=" + suffix, "--network", "none", "-v", str(root) + ":/tmp/lb:ro",
                "--entrypoint=/bin/promtool", "prom/prometheus:v3.14.0", "test", "rules", "/tmp/lb/test.json"],
                capture_output=True, text=True, timeout=45)
            print(result.stdout, end="")
            if result.returncode:
                print(result.stderr, file=sys.stderr, end="")
                raise RuntimeError("HAProxy rule promtool verification failed")
    finally:
        cleanup_owned(suffix, network, (name,))
        print("Owned HAProxy rule test container cleaned up.")


if __name__ == "__main__":
    main()
