#!/usr/bin/env python3
"""Isolated real-Prometheus smoke test; never targets a cluster or real channels."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.monitoring import coverage_report, metric_evidence
from scripts.operations.monitoring.prometheus import collect, collect_targets, parse_alert_rules, targets_endpoint

EXPORTER = '''from http.server import BaseHTTPRequestHandler, HTTPServer
import sys, time
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  stamp = ' ' + str(int(time.time()*1000)-20000) if len(sys.argv)>1 and sys.argv[1]=='stale' else ''
  body = ('# TYPE kube_node_status_condition gauge\\nkube_node_status_condition{node="cp",condition="Ready",status="true"} 1'+stamp+'\\nkube_node_status_condition{node="worker",condition="Ready",status="true"} 0'+stamp+'\\n').encode()
  self.send_response(200); self.send_header("Content-Type", "text/plain; version=0.0.4"); self.end_headers(); self.wfile.write(body)
 def log_message(self,*args): pass
HTTPServer(("0.0.0.0",8000),Handler).serve_forever()
'''
PROMETHEUS_CONFIG = '''global:
  scrape_interval: 2s
  scrape_timeout: 1s
scrape_configs:
  - job_name: atlas-e2e
    static_configs:
      - targets: ["exporter:8000"]
  - job_name: shared-a
    static_configs:
      - targets: ["exporter:8000"]
        labels: {job: atlas-shared-scrape, metrics_endpoint: primary}
    metric_relabel_configs: &rename_shared_metric
      - source_labels: [__name__]
        regex: kube_node_status_condition
        target_label: __name__
        replacement: atlas_e2e_shared_node_status
  - job_name: shared-b
    static_configs:
      - targets: ["exporter:8000"]
        labels: {job: atlas-shared-scrape, metrics_endpoint: secondary}
    metric_relabel_configs: *rename_shared_metric
'''


class RuleInventory:
    """Provide one declared rule for the report while verifying real sample queries."""
    def get(self, kind, namespace=None):
        return {"items": [{"metadata": {"namespace": "monitoring", "name": "nodes", "uid": "uid"},
                           "spec": {"groups": [{"name": "nodes", "rules": [
            {"alert": "AtlasNodeReady", "labels": {"risk_id": "R-NODE-01"}}
        ]}]}}]}


LOADED_RULES = {"state": "observed", "rules": parse_alert_rules({"status": "success", "data": {
    "groups": [{"name": "nodes", "file": "/rules/monitoring-nodes-uid.yaml", "rules": [
        {"name": "AtlasNodeReady", "health": "ok", "state": "inactive", "labels": {"risk_id": "R-NODE-01"}}
    ]}]}})}


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=45)
    if check and result.returncode:
        raise RuntimeError("docker " + args[0] + " failed: " + result.stderr.strip())
    return result


def wait_for(url, predicate, label):
    last = None
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        last = collect(url, ["cp", "worker"], 10)
        if predicate(last):
            print(label + ": " + json.dumps(last, ensure_ascii=False))
            return
        time.sleep(1)
    raise RuntimeError(label + " did not converge: " + json.dumps(last))


def wait_for_target(url, expected_state, label):
    last = None
    deadline = time.monotonic() + 35
    expected = [{"scrapePool": "atlas-e2e", "job": "atlas-e2e", "instance": "exporter:8000"}]
    while time.monotonic() < deadline:
        last = collect_targets(url, expected, 10)
        states = [item["state"] for item in last.get("expected", [])]
        if last.get("state") == "observed" and states == [expected_state]:
            risk_targets = coverage_report.parse_risk_targets([
                "R-NODE-01|atlas-e2e|atlas-e2e|exporter:8000"
            ])

            report = coverage_report.collect(RuleInventory(), {}, LOADED_RULES, last, risk_targets)
            risk = next(item for item in report["risks"] if item["riskId"] == "R-NODE-01")
            expected_data_state = "unknown" if expected_state == "healthy" else "failed"
            if (risk["targetHealthEvidence"]["state"] !=
                    ("healthy" if expected_state == "healthy" else "failed") or
                    risk["dataEvidence"]["state"] != expected_data_state or
                    (expected_state == "healthy" and
                     risk["dataEvidence"]["reason"] != "risk-metric-samples-not-queried")):
                raise RuntimeError("risk coverage overstated Prometheus target health: " +
                                   json.dumps(risk, ensure_ascii=False, sort_keys=True))
            print(label + ": " + json.dumps(last, ensure_ascii=False))
            print("Risk coverage target evidence: target=" + expected_state +
                  ", metric-data=" + expected_data_state + ".")
            return
        time.sleep(1)
    try:
        with urlopen(Request(targets_endpoint(url), headers={"Accept": "application/json"}), timeout=5) as response:
            payload = json.loads(response.read(1024 * 1024 + 1))
        targets = [{"labels": {key: row.get("labels", {}).get(key) for key in ("job", "instance")},
                    "health": row.get("health"), "lastScrape": row.get("lastScrape")}
                   for row in payload.get("data", {}).get("activeTargets", [])]
    except (OSError, ValueError, KeyError, TypeError):
        targets = []
    raise RuntimeError(label + " did not converge: " + json.dumps({"parsed": last, "targets": targets}))


def verify_risk_metric(url, expected_state):
    target = {"scrapePool": "atlas-e2e", "job": "atlas-e2e", "instance": "exporter:8000"}
    profile = coverage_report.parse_profile({"schemaVersion": 1, "risks": [],
        "riskTargets": [{"riskId": "R-NODE-01", **target}],
        "riskMetrics": [{"riskId": "R-NODE-01", **target,
                         "metric": "kube_node_status_condition",
                         "labels": {"node": "cp", "condition": "Ready", "status": "true"}}]})
    audit = collect_targets(url, [target], 10)
    checks = metric_evidence.collect(url, profile["riskMetrics"], audit, 10)
    assert checks["R-NODE-01"][0]["state"] == expected_state, checks
    expected_assessment = {"fresh": "verified", "stale": "failed",
                           "target-unverified": "unknown"}[expected_state]
    assert metric_evidence.assess(checks["R-NODE-01"], [target])[0] == expected_assessment
    report = coverage_report.collect(RuleInventory(), {}, LOADED_RULES, audit, profile["riskTargets"], checks)
    risk = next(row for row in report["risks"] if row["riskId"] == "R-NODE-01")
    assert risk["status"] == ("unverified" if expected_state == "fresh" else "uncovered")
    if expected_state == "fresh":
        assert risk["reasons"] == ["delivery-unverified"]
    assert risk["dataEvidence"]["state"] == ("failed" if expected_state == "target-unverified" else expected_assessment)
    print("Risk metric sample: " + expected_state + ".")
    if expected_state == "fresh":
        row = profile["riskMetrics"]["R-NODE-01"][0]
        outcome = metric_evidence.collect(url, {"R-NODE-01": [
            {**row, "labels": {"condition": "Ready", "status": "true"}},
            {**row, "labels": {**row["labels"], "node": "nonexistent-fixture-node"}},
        ]}, audit, 10)
        assert [item["state"] for item in outcome["R-NODE-01"]] == ["ambiguous", "missing"]
        print("Risk metric samples with two and zero matches identified.")


def verify_shared_source(url):
    target = {"scrapePool": "shared-a", "job": "atlas-shared-scrape", "instance": "exporter:8000"}
    peer = {**target, "scrapePool": "shared-b"}
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        audit = collect_targets(url, [target, peer], 10)
        if all(row["state"] == "healthy" for row in audit.get("expected", [])) and audit.get("expectedCount") == 2:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Shared scrape pools did not become healthy: " + json.dumps(audit))

    labels = {"node": "cp", "condition": "Ready", "status": "true"}
    row = {**target, "metric": "atlas_e2e_shared_node_status"}
    checks = metric_evidence.collect(url, {"R-NODE-01": [
        {**row, "labels": {**labels, "metrics_endpoint": "primary"}},
        {**row, "labels": labels},
        {**row, "labels": {**labels, "metrics_endpoint": "secondary"}},
    ]}, audit, 10)["R-NODE-01"]
    assert [(check["state"], check["attribution"]) for check in checks] == [
        ("fresh", "distinguishing-target-labels"),
        ("target-unverified", "ambiguous-job-instance"),
        ("target-unverified", "target-label-mismatch"),
    ], checks
    print("Shared Prometheus scrape pools: exact source verified; ambiguous and mismatched sources refused.")


def cleanup_owned(suffix, network, containers):
    failures = []
    resources = [("container", name) for name in containers] + [("network", network)]

    def missing(result, kind, name):
        error = result.stderr.lower()
        return ("no such" in error or
                (kind == "network" and "network " + name.lower() + " not found" in error))

    for kind, name in resources:
        template = ('{{ index .Config.Labels "atlas-kubekit.e2e" }}' if kind == "container"
                    else '{{ index .Labels "atlas-kubekit.e2e" }}')
        try:
            found = docker(kind, "inspect", "--format", template, name, check=False)
            if found.returncode:
                if not missing(found, kind, name):
                    failures.append(name + ": inspect failed")
                continue
            if found.stdout.strip() != suffix:
                failures.append(name + ": ownership mismatch")
                continue
            docker(kind, "rm", "-f", name, check=False) if kind == "container" else docker(kind, "rm", name, check=False)
            after = docker(kind, "inspect", "--format", template, name, check=False)
            if after.returncode == 0 or not missing(after, kind, name):
                failures.append(name + ": cleanup not verified")
        except (OSError, subprocess.TimeoutExpired):
            failures.append(name + ": cleanup not verified")
    if failures:
        raise RuntimeError("Could not clean up test resources: " + ", ".join(failures))


def observed(value):
    return value.get("state") == "observed" and {
        row["node"]: row["state"] for row in value["nodes"]
    } == {"cp": "ready", "worker": "not-ready"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True, help="Type atlas-monitor-e2e")
    args = parser.parse_args()
    if args.confirm != "atlas-monitor-e2e":
        parser.error("Explicit confirmation atlas-monitor-e2e is required")
    suffix = uuid.uuid4().hex[:12]
    network, exporter, prom = ("atlas-monitor-e2e-" + suffix, "atlas-exporter-" + suffix,
                               "atlas-prometheus-" + suffix)
    try:
        temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="atlas-monitor-", dir=temp_root) as directory:
            root = Path(directory).resolve()
            (root / "exporter.py").write_text(EXPORTER)
            (root / "prometheus.yml").write_text(PROMETHEUS_CONFIG)
            docker("network", "create", "--label", "atlas-kubekit.e2e=" + suffix, network)

            def start_exporter(stale=False):
                docker("run", "-d", "--pull=never", "--name", exporter,
                       "--label", "atlas-kubekit.e2e=" + suffix, "--network", network,
                       "--network-alias", "exporter", "-v", str(root / "exporter.py") + ":/tmp/exporter.py:ro",
                       "python:3.13-slim", "python", "/tmp/exporter.py", *( ["stale"] if stale else [] ))

            start_exporter(stale=True)
            docker("run", "-d", "--pull=never", "--name", prom, "--label", "atlas-kubekit.e2e=" + suffix,
                   "--network", network, "-p", "127.0.0.1::9090",
                   "-v", str(root / "prometheus.yml") + ":/etc/prometheus/prometheus.yml:ro",
                   "prom/prometheus:v3.5.0", "--config.file=/etc/prometheus/prometheus.yml")
            port = docker("port", prom, "9090/tcp").stdout.strip().rsplit(":", 1)[1]
            url = "http://127.0.0.1:" + port
            wait_for(url, lambda value: value.get("state") == "unverified" and bool(value.get("nodes"))
                     and all(row["state"] == "stale" for row in value["nodes"]), "old samples")
            wait_for_target(url, "healthy", "active scrape target")
            verify_risk_metric(url, "stale")
            docker("rm", "-f", exporter)
            start_exporter()
            wait_for(url, observed, "initial samples")
            verify_risk_metric(url, "fresh")
            docker("rm", "-f", exporter)
            wait_for_target(url, "down", "target scrape failure")
            verify_risk_metric(url, "target-unverified")
            wait_for(url, lambda value: value.get("state") == "unverified" and bool(value.get("nodes"))
                     and all(row["state"] == "missing" for row in value["nodes"]), "missing samples")
            start_exporter()
            wait_for_target(url, "healthy", "target scrape recovery")
            wait_for(url, observed, "recovered samples")
            verify_risk_metric(url, "fresh")
            verify_shared_source(url)
    finally:
        cleanup_owned(suffix, network, (prom, exporter))
        print("Owned temporary containers and network cleaned up.")


if __name__ == "__main__":
    main()
