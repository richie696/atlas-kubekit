#!/usr/bin/env python3
"""Exercise Cilium IPAM and eBPF alert firing and recovery in the retained lab."""
import argparse
import copy
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen


CONTEXT = "atlas-e2e-lab"
NODES = {"cp1": "10.20.1.28", "w1": "10.20.1.29", "w2": "10.20.1.30"}
NAMESPACE = "monitoring"
PROMETHEUS = "kube-prometheus-stack-prometheus"
SOURCE_RULE = "atlas-cilium-health"
TEST_RULE = "atlas-cilium-threshold-e2e"
TEST_ALERTS = {"AtlasCiliumIPAMCapacityE2E", "AtlasCiliumBPFPressureE2E"}
RELEASE = "kube-prometheus-stack"


def command(args, *, input_text=None, check=True, timeout=60):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("kubectl operation failed: " + result.stderr[-1200:])
    return result


def resource(client, *args):
    return json.loads(command(client + list(args) + ["-o", "json"]).stdout)


def fetch_json(base_url, path, timeout):
    with urlopen(base_url + path, timeout=timeout) as response:
        return json.load(response)


def query(base_url, expression, timeout):
    value = fetch_json(base_url, "/api/v1/query?" + urlencode({"query": expression}), timeout)
    if value.get("status") != "success":
        raise RuntimeError("Prometheus query failed")
    return value.get("data", {}).get("result", [])


def verify_lab(client, kubeconfig, context, base_url, timeout):
    if context != CONTEXT or not kubeconfig.is_absolute() or not kubeconfig.is_file():
        raise RuntimeError("refusing non-lab context or missing absolute kubeconfig")
    if kubeconfig.stat().st_mode & 0o077:
        raise RuntimeError("kubeconfig permissions must be 0600 or stricter")
    selected = resource(client, "config", "view", "--minify")
    current_context = selected.get("contexts", [{}])[0].get("name")
    server = selected.get("clusters", [{}])[0].get("cluster", {}).get("server")
    if current_context != CONTEXT or server != "https://10.20.1.31:6443":
        raise RuntimeError("active kubeconfig identity differs from the fixed test lab")
    nodes = resource(client, "get", "nodes").get("items", [])
    observed = {}
    for node in nodes:
        name = node.get("metadata", {}).get("name")
        address = next((item.get("address") for item in node.get("status", {}).get("addresses", [])
                        if item.get("type") == "InternalIP"), None)
        observed[name] = address
        if not any(item.get("type") == "Ready" and item.get("status") == "True"
                   for item in node.get("status", {}).get("conditions", [])):
            raise RuntimeError("lab node is not Ready: " + str(name))
    if observed != NODES:
        raise RuntimeError("refusing unexpected lab node inventory: " + repr(observed))
    prom = resource(client, "-n", NAMESPACE, "get", "prometheus", PROMETHEUS)
    spec = prom.get("spec", {})
    if (spec.get("ruleSelector", {}).get("matchLabels", {}).get("release") != RELEASE or
            spec.get("ruleNamespaceSelector") != {}):
        raise RuntimeError("Prometheus selector differs from the retained test lab")
    configs = resource(client, "get", "alertmanagerconfigs.monitoring.coreos.com", "--all-namespaces")
    if configs.get("items"):
        raise RuntimeError("refusing threshold tests while AlertmanagerConfig routes are present")
    source = resource(client, "-n", NAMESPACE, "get", "prometheusrule", SOURCE_RULE)
    labels = source.get("metadata", {}).get("labels", {})
    if labels.get("release") != RELEASE or labels.get("atlas-kubekit.io/managed-by") != "monitoring":
        raise RuntimeError("managed Cilium rules are not selected as expected")
    existing_test = command(client + ["get", "prometheusrule", TEST_RULE, "-n", NAMESPACE,
                                      "--ignore-not-found", "-o", "json"], check=False)
    if existing_test.returncode:
        raise RuntimeError("cannot verify that the temporary E2E rule name is unused")
    if existing_test.stdout.strip():
        raise RuntimeError("temporary Cilium E2E rule name is already in use")
    for expression in (
        '(sum by (node, family) (cilium_ip_addresses{job="cilium-agent"}) '
        '/ sum by (node, family) (cilium_ipam_capacity{job="cilium-agent"}) * 100)',
        'max by (node, map_name) (cilium_bpf_map_pressure{job="cilium-agent"}) * 100',
    ):
        samples = query(base_url, expression, timeout)
        if not samples or any(float(item.get("value", [None, "nan"])[1]) <= 0 for item in samples):
            raise RuntimeError("positive Cilium telemetry is unavailable for threshold E2E")
    with urlopen(base_url + "/-/ready", timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError("Prometheus is not ready")
    return source


def expression_at(rule, threshold):
    expression = rule.get("expr")
    if not isinstance(expression, str) or not re.search(r">=\s*\d+(?:\.\d+)?\s*$", expression):
        raise RuntimeError("source Cilium threshold rule has an unexpected expression")
    return re.sub(r">=\s*\d+(?:\.\d+)?\s*$", ">= " + str(threshold), expression)


def test_manifest(source, ipam_threshold, bpf_threshold):
    rules = {rule.get("alert"): rule
             for group in source.get("spec", {}).get("groups", [])
             for rule in group.get("rules", [])}
    if not {"AtlasCiliumNodeIPAMCapacityLow", "AtlasCiliumBPFMapPressureHigh"} <= set(rules):
        raise RuntimeError("managed Cilium threshold rules are missing")
    selected = []
    for source_name, test_name, threshold in (
        ("AtlasCiliumNodeIPAMCapacityLow", "AtlasCiliumIPAMCapacityE2E", ipam_threshold),
        ("AtlasCiliumBPFMapPressureHigh", "AtlasCiliumBPFPressureE2E", bpf_threshold),
    ):
        rule = copy.deepcopy(rules[source_name])
        rule["alert"] = test_name
        rule["expr"] = expression_at(rule, threshold)
        rule.pop("for", None)
        rule.setdefault("labels", {})["severity"] = "info"
        rule["annotations"]["summary"] = "隔离环境 Cilium 告警触发与恢复验证"
        rule["annotations"]["description"] = "临时阈值 E2E；不代表生产阈值。"
        selected.append(rule)
    labels = copy.deepcopy(source.get("metadata", {}).get("labels", {}))
    labels.update({"release": RELEASE, "atlas-kubekit.io/managed-by": "e2e"})
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": TEST_RULE, "namespace": NAMESPACE, "labels": labels},
            "spec": {"groups": [{"name": "atlas.cilium.e2e", "rules": selected}]}}


def matching_alerts(base_url, timeout):
    alerts = fetch_json(base_url, "/api/v1/alerts", timeout).get("data", {}).get("alerts", [])
    return {item.get("labels", {}).get("alertname"): item for item in alerts
            if item.get("labels", {}).get("alertname") in TEST_ALERTS}


def wait_for(base_url, timeout, predicate, description, seconds=150):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(5)
    raise RuntimeError("timed out waiting for " + description)


def delete_test_rule(client):
    current = command(client + ["get", "prometheusrule", TEST_RULE, "-n", NAMESPACE,
                                "--ignore-not-found", "-o", "json"], check=False)
    if current.returncode:
        raise RuntimeError("cannot inspect temporary Cilium rule before cleanup")
    if not current.stdout.strip():
        return
    value = json.loads(current.stdout)
    if value.get("metadata", {}).get("labels", {}).get("atlas-kubekit.io/managed-by") != "e2e":
        raise RuntimeError("refusing to delete a Cilium rule not owned by this E2E")
    command(client + ["delete", "prometheusrule", TEST_RULE, "-n", NAMESPACE,
                      "--ignore-not-found=true", "--wait=true", "--timeout=60s"], timeout=70)
    remaining = command(client + ["get", "prometheusrule", TEST_RULE, "-n", NAMESPACE,
                                 "--ignore-not-found", "-o", "name"], check=False)
    if remaining.returncode or remaining.stdout.strip():
        raise RuntimeError("temporary Cilium test rule cleanup could not be verified")


def run(base_url, timeout, client, source):
    # Thresholds are intentionally below the positive live samples; they only exist in this temporary rule.
    firing_manifest = test_manifest(source, 1, 0.01)
    recovered_manifest = test_manifest(source, 100, 100)
    created = False
    try:
        created = True
        command(client + ["apply", "-f", "-"], input_text=json.dumps(firing_manifest))
        wait_for(base_url, timeout,
                 lambda: set(matching_alerts(base_url, timeout)) == TEST_ALERTS and
                 all(item.get("state") == "firing" for item in matching_alerts(base_url, timeout).values()),
                 "both temporary Cilium threshold alerts to fire")
        fired = matching_alerts(base_url, timeout)
        ipam_labels = fired["AtlasCiliumIPAMCapacityE2E"].get("labels", {})
        bpf_labels = fired["AtlasCiliumBPFPressureE2E"].get("labels", {})
        if not ipam_labels.get("node") or ipam_labels.get("risk_id") != "R-NET-04":
            raise RuntimeError("IPAM threshold alert lacks expected node and risk labels")
        if not bpf_labels.get("node") or not bpf_labels.get("map_name") or bpf_labels.get("risk_id") != "R-NET-02":
            raise RuntimeError("eBPF threshold alert lacks expected node/map and risk labels")
        print("PASS: temporary IPAM and eBPF pressure rules fired with node-level labels.")
        command(client + ["apply", "-f", "-"], input_text=json.dumps(recovered_manifest))
        wait_for(base_url, timeout,
                 lambda: not matching_alerts(base_url, timeout),
                 "temporary threshold alerts to resolve after restoring non-firing thresholds")
        print("PASS: both threshold alerts resolved after restoring thresholds above observed values.")
    finally:
        if created:
            delete_test_rule(client)
            wait_for(base_url, timeout, lambda: not matching_alerts(base_url, timeout),
                     "temporary Cilium alerts to disappear after cleanup", seconds=90)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:19090")
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args()
    parsed = urlsplit(args.prometheus_url)
    if (args.confirm != "atlas-cilium-failure-e2e-28-30" or parsed.scheme not in ("http", "https") or
            not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or
            (parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1", "localhost")) or
            not 1 <= args.timeout <= 60):
        parser.error("refusing unsafe URL, parameters, or missing fixed-lab confirmation")
    client = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context]
    source = verify_lab(client, args.kubeconfig, args.context, args.prometheus_url.rstrip("/"), args.timeout)
    run(args.prometheus_url.rstrip("/"), args.timeout, client, source)
    print("Cilium IPAM/eBPF threshold failure and recovery E2E: PASS (temporary rule removed)")


if __name__ == "__main__":
    main()
