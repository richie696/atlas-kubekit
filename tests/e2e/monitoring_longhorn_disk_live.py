#!/usr/bin/env python3
"""Verify live Longhorn disk telemetry and alert firing/recovery in the retained lab."""
import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import subprocess
import time
import urllib.parse
import urllib.request
import uuid


CONTEXT = "atlas-e2e-lab"
API_SERVER = "https://10.20.1.31:6443"
EXPECTED_NODES = {"cp1": "10.20.1.28", "w1": "10.20.1.29", "w2": "10.20.1.30"}
LONGHORN_NODES = {"w1", "w2"}
NAMESPACE = "monitoring"
SOURCE_RULE = "atlas-longhorn-disk-capacity"
PROMETHEUS = "kube-prometheus-stack-prometheus"
RELEASE = "kube-prometheus-stack"
USAGE_METRIC = "longhorn_disk_usage_bytes{node!=\"\",disk!=\"\"}"
CAPACITY_METRIC = "longhorn_disk_capacity_bytes{node!=\"\",disk!=\"\"}"
STATUS_METRIC = 'longhorn_disk_status{condition="ready",node!="",disk!=""}'
SOURCE_ALERT = "AtlasLonghornDiskUsageHigh"
TEST_ALERT = "AtlasLonghornDiskUsageE2E"


class E2EError(RuntimeError):
    pass


def kubectl(base, *args, check=True, input_text=None):
    result = subprocess.run(base + list(args), input=input_text, text=True,
                           capture_output=True, timeout=60)
    if check and result.returncode:
        raise E2EError("kubectl failed: " + result.stderr[-1600:])
    return result


def kubectl_json(base, *args):
    return json.loads(kubectl(base, *args, "-o", "json").stdout)


def ready_longhorn_nodes(items):
    """Return Longhorn nodes whose canonical Ready condition is true."""
    return {
        item.get("metadata", {}).get("name")
        for item in items
        if any(str(condition.get("type", "")).lower() == "ready"
               and condition.get("status") == "True"
               for condition in item.get("status", {}).get("conditions", []))
    }


def request_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def verify_prometheus_ready(base_url):
    with urllib.request.urlopen(base_url + "/-/ready", timeout=10) as response:
        if response.status != 200:
            raise E2EError("Prometheus is not ready")
        response.read()


def prometheus_query(base_url, expression):
    url = base_url + "/api/v1/query?" + urllib.parse.urlencode({"query": expression})
    payload = request_json(url)
    if payload.get("status") != "success":
        raise E2EError("Prometheus query was not successful")
    return payload.get("data", {}).get("result", [])


def verify_lab(kubeconfig, context, base_url, max_age):
    config = kubectl_json(["kubectl", "--kubeconfig", str(kubeconfig)], "config", "view", "--minify")
    active_context = config.get("contexts", [{}])[0].get("name")
    server = config.get("clusters", [{}])[0].get("cluster", {}).get("server")
    if active_context != context or context != CONTEXT or server != API_SERVER:
        raise E2EError("refusing a cluster other than the fixed isolated lab")

    client = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context]
    nodes = kubectl_json(client, "get", "nodes").get("items", [])
    observed = {}
    for node in nodes:
        metadata = node.get("metadata", {})
        internal_ip = next((item.get("address") for item in node.get("status", {}).get("addresses", [])
                            if item.get("type") == "InternalIP"), None)
        ready = any(item.get("type") == "Ready" and item.get("status") == "True"
                    for item in node.get("status", {}).get("conditions", []))
        if not ready:
            raise E2EError("lab node is not Ready: " + str(metadata.get("name")))
        observed[metadata.get("name")] = internal_ip
    if observed != EXPECTED_NODES:
        raise E2EError("refusing unexpected lab node inventory: " + repr(observed))

    lh_nodes = kubectl_json(client, "-n", "longhorn-system", "get", "nodes.longhorn.io").get("items", [])
    ready_lh_nodes = ready_longhorn_nodes(lh_nodes)
    if ready_lh_nodes != LONGHORN_NODES:
        raise E2EError("Longhorn node inventory differs from the fixed lab: " + repr(ready_lh_nodes))

    prom = kubectl_json(client, "-n", NAMESPACE, "get", "prometheus", PROMETHEUS)
    selector = prom.get("spec", {}).get("ruleSelector", {}).get("matchLabels", {})
    if selector.get("release") != RELEASE:
        raise E2EError("Prometheus rule selector differs from the expected release")
    custom_routes = kubectl_json(client, "get", "alertmanagerconfigs.monitoring.coreos.com", "--all-namespaces")
    if custom_routes.get("items"):
        raise E2EError("refusing to create test alerts while custom Alertmanager routes exist")

    source = kubectl_json(client, "-n", NAMESPACE, "get", "prometheusrule", SOURCE_RULE)
    labels = source.get("metadata", {}).get("labels", {})
    if labels.get("release") != RELEASE or labels.get("atlas-kubekit.io/managed-by") != "monitoring":
        raise E2EError("source Longhorn disk rules are not owned and selected as expected")
    rules = [rule for group in source.get("spec", {}).get("groups", [])
             for rule in group.get("rules", [])]
    source_alert = next((rule for rule in rules if rule.get("alert") == SOURCE_ALERT), None)
    if not source_alert:
        raise E2EError("managed Longhorn disk usage rule is missing")
    freshness_limits = {int(value) for value in re.findall(
        r"time\(\)\s*-\s*timestamp\([^)]*\)\s*<\s*(\d+)", source_alert.get("expr", ""))}
    if len(freshness_limits) != 1:
        raise E2EError("managed disk rule does not use one consistent freshness limit")
    effective_max_age = min(max_age, next(iter(freshness_limits)) - 5)
    if effective_max_age < 1:
        raise E2EError("disk metrics are too old for the managed rule freshness window")

    now = time.time()
    def collect(metric):
        values = prometheus_query(base_url, metric)
        result = {}
        for sample in values:
            identity = (sample.get("metric", {}).get("node"), sample.get("metric", {}).get("disk"))
            value = sample.get("value", [])
            if None in identity or len(value) != 2:
                continue
            timestamp, numeric = float(value[0]), float(value[1])
            if now - timestamp > effective_max_age or timestamp - now > 30 or not math.isfinite(numeric):
                raise E2EError("Longhorn disk metric is stale or invalid")
            if identity in result and result[identity] != numeric:
                raise E2EError("duplicate Longhorn manager samples disagree")
            result[identity] = numeric
        return result

    usage, capacity, status = collect(USAGE_METRIC), collect(CAPACITY_METRIC), collect(STATUS_METRIC)
    if not usage or set(usage) != set(capacity) or set(usage) != set(status):
        raise E2EError("Longhorn disk usage, capacity, and readiness samples are incomplete")
    if {node for node, _ in usage} != LONGHORN_NODES:
        raise E2EError("Longhorn disk metrics do not cover the registered storage nodes")
    if any(capacity[key] <= 0 or usage[key] < 0 or usage[key] >= capacity[key] or status[key] != 1
           for key in usage):
        raise E2EError("Longhorn disk capacity, utilization, or readiness is unhealthy")

    loaded_rules = request_json(base_url + "/api/v1/rules?type=alert").get("data", {}).get("groups", [])
    selected = {rule.get("name"): rule for group in loaded_rules for rule in group.get("rules", [])
                if rule.get("name") in {SOURCE_ALERT, "AtlasLonghornDiskNotReady",
                                         "AtlasLonghornDiskMetricsMissing", "AtlasLonghornDiskTelemetryPartial"}}
    required = {SOURCE_ALERT, "AtlasLonghornDiskNotReady", "AtlasLonghornDiskMetricsMissing",
                "AtlasLonghornDiskTelemetryPartial"}
    if set(selected) != required or any(rule.get("health") != "ok" or rule.get("state") != "inactive"
                                        for rule in selected.values()):
        raise E2EError("Longhorn disk rules are missing, unhealthy, or unexpectedly firing")
    verify_prometheus_ready(base_url)

    test_rule_name = "atlas-longhorn-disk-e2e-" + uuid.uuid4().hex[:10]
    existing = kubectl(client, "-n", NAMESPACE, "get", "prometheusrule", test_rule_name,
                       "--ignore-not-found", "-o", "name", check=False)
    if existing.returncode or existing.stdout.strip():
        raise E2EError("temporary PrometheusRule name is not safely available")
    return client, source_alert, usage, capacity, test_rule_name


def threshold_expression(source, threshold):
    expression = source.get("expr")
    if not isinstance(expression, str):
        raise E2EError("source alert expression is invalid")
    updated, count = re.subn(r"(>\s*)\d+(?:\.\d+)?(?=\)\s+and on \(node, disk\))",
                             lambda match: match.group(1) + format(threshold, ".6f"), expression, count=1)
    if count != 1:
        raise E2EError("source alert expression threshold format is unexpected")
    return updated


def test_manifest(source, threshold, run_id):
    rule = deepcopy(source)
    rule["alert"] = TEST_ALERT
    rule["expr"] = threshold_expression(rule, threshold)
    rule.pop("for", None)
    rule.pop("keep_firing_for", None)
    labels = dict(rule.get("labels", {}))
    labels.update({"severity": "info", "atlas_e2e_run": run_id})
    rule["labels"] = labels
    rule["annotations"] = {"summary": "隔离环境 Longhorn 磁盘指标规则触发与恢复验证",
                            "description": "临时阈值由实时磁盘样本派生；不代表生产容量阈值。"}
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": "", "namespace": NAMESPACE,
                         "labels": {"release": RELEASE, "atlas-kubekit.io/e2e": "longhorn-disk",
                                    "atlas-kubekit.io/e2e-run": run_id}},
            "spec": {"groups": [{"name": "atlas.longhorn.disk.e2e." + run_id, "rules": [rule]}]}}


def wait_for(description, predicate, timeout, interval=3):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, E2EError) as error:
            last = error
        time.sleep(interval)
    raise E2EError("timed out waiting for " + description + (": " + str(last) if last else ""))


def alerts(base_url):
    expression = 'ALERTS{alertname="' + TEST_ALERT + '",alertstate="firing"}'
    return prometheus_query(base_url, expression)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--max-age", type=int, default=180)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.prometheus_url)
    if (args.confirm != "atlas-longhorn-disk-live-e2e-28-30" or args.context != CONTEXT
            or not args.kubeconfig.is_absolute() or not args.kubeconfig.is_file()
            or args.kubeconfig.stat().st_mode & 0o077
            or parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1", "localhost")
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not 30 <= args.max_age <= 3600 or not 60 <= args.timeout <= 600):
        parser.error("private kubeconfig, fixed lab confirmation, loopback Prometheus URL, and bounded timings are required")
    base_url = args.prometheus_url.rstrip("/")
    client, source, usage, capacity, rule_name = verify_lab(args.kubeconfig, args.context,
                                                            base_url, args.max_age)
    run_id = uuid.uuid4().hex[:10]
    pairs = sorted(usage)
    observed_percent = {key: usage[key] / capacity[key] * 100 for key in pairs}
    trigger_threshold = min(observed_percent.values()) - max(0.01, min(observed_percent.values()) * 0.05)
    recovery_threshold = max(observed_percent.values()) + 1
    if recovery_threshold >= 100:
        raise E2EError("observed disk use leaves insufficient test threshold headroom")
    manifest = test_manifest(source, trigger_threshold, run_id)
    manifest["metadata"]["name"] = rule_name
    created = False
    try:
        applied = deepcopy(manifest)
        created = True
        kubectl(client, "apply", "-f", "-", input_text=json.dumps(applied))
        expected_pairs = set(pairs)
        def fired_pairs():
            current = alerts(base_url)
            observed = {(item.get("metric", {}).get("node"), item.get("metric", {}).get("disk"))
                        for item in current}
            return current if observed == expected_pairs and all(run_id == item.get("metric", {}).get("atlas_e2e_run")
                                                                for item in current) else None
        fired = wait_for("one firing alert for every live Longhorn node/disk", fired_pairs, args.timeout)
        if any(not item.get("metric", {}).get("node") or not item.get("metric", {}).get("disk") for item in fired):
            raise E2EError("disk alert lacks node/disk labels")
        print("PASS: live Longhorn usage/capacity/ready metrics produced per-disk firing alerts.")
        for item in fired:
            metric = item["metric"]
            print("firing:", metric["node"], metric["disk"],
                  "observed_percent=" + format(observed_percent[(metric["node"], metric["disk"])], ".2f"))

        recovered = deepcopy(manifest)
        recovered["spec"]["groups"][0]["rules"][0]["expr"] = threshold_expression(source, recovery_threshold)
        kubectl(client, "apply", "-f", "-", input_text=json.dumps(recovered))
        wait_for("all temporary disk alerts to resolve", lambda: not alerts(base_url), args.timeout)
        print("PASS: live rule alerts resolved after restoring a non-firing threshold.")
    finally:
        if created:
            current = kubectl(client, "-n", NAMESPACE, "get", "prometheusrule", rule_name,
                              "-o", "json", check=False)
            if current.returncode == 0:
                obj = json.loads(current.stdout)
                labels = obj.get("metadata", {}).get("labels", {})
                if labels.get("atlas-kubekit.io/e2e-run") != run_id:
                    raise E2EError("refusing to delete a temporary-rule name not owned by this run")
                kubectl(client, "-n", NAMESPACE, "delete", "prometheusrule", rule_name,
                        "--ignore-not-found", "--wait=true")
                wait_for("temporary disk alerts to disappear after rule cleanup",
                         lambda: not alerts(base_url), min(args.timeout, 180))
            elif current.returncode != 1:
                raise E2EError("could not verify temporary disk rule ownership before cleanup")
    remaining = kubectl(client, "-n", NAMESPACE, "get", "prometheusrule", rule_name,
                        "--ignore-not-found", "-o", "name", check=False)
    if remaining.returncode or remaining.stdout.strip():
        raise E2EError("temporary Longhorn disk rule cleanup was not verified")
    print("Longhorn disk collection and alert firing/recovery live E2E: PASS (temporary rule removed)")


if __name__ == "__main__":
    try:
        main()
    except (E2EError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise SystemExit("Longhorn disk live E2E failed: " + str(error))
