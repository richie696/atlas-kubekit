#!/usr/bin/env python3
"""Verify live Cilium metric collection and health rules on a retained lab."""
import argparse
from datetime import datetime, timezone
import json
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen


EXPECTED_AGENT_NODES = {"cp1", "w1", "w2"}
AGENT_POOL = "podMonitor/monitoring/atlas-cilium-agent/0"
OPERATOR_POOL = "podMonitor/monitoring/atlas-cilium-operator/0"
EXPECTED_RULES = {"AtlasCiliumAgentOrTelemetryMissing", "AtlasCiliumOperatorTelemetryUnavailable"}
EXPECTED_RULES |= {
    "AtlasCiliumIPAMTelemetryMissing", "AtlasCiliumNodeIPAMCapacityLow",
    "AtlasCiliumBPFMapPressureHigh", "AtlasCiliumNodeConnectivityDegraded",
}


def fetch_json(base_url, path, timeout):
    with urlopen(base_url + path, timeout=timeout) as response:
        return json.load(response)


def query(base_url, expression, timeout):
    path = "/api/v1/query?" + urlencode({"query": expression})
    payload = fetch_json(base_url, path, timeout)
    if payload.get("status") != "success":
        raise RuntimeError("Prometheus query failed")
    return payload.get("data", {}).get("result", [])


def samples_by_node(base_url, job, metric, timeout, max_age):
    samples = query(base_url, metric, timeout)
    observed = {}
    now = datetime.now(timezone.utc).timestamp()
    for sample in samples:
        labels = sample.get("metric", {})
        node = labels.get("node")
        value = sample.get("value", [])
        if not node or len(value) != 2:
            continue
        timestamp, raw = float(value[0]), str(value[1])
        if now - timestamp > max_age or timestamp - now > 30:
            raise RuntimeError(job + " has stale or future samples")
        observed[node] = raw
    return observed


def verify(base_url, timeout, max_age):
    ready = fetch_json(base_url, "/api/v1/targets?state=active", timeout)
    if ready.get("status") != "success":
        raise RuntimeError("Prometheus target API did not return success")
    targets = ready.get("data", {}).get("activeTargets", [])
    for pool, expected_nodes in ((AGENT_POOL, EXPECTED_AGENT_NODES), (OPERATOR_POOL, None)):
        selected = [target for target in targets if target.get("scrapePool") == pool]
        if not selected:
            raise RuntimeError("missing active target pool: " + pool)
        if any(target.get("health") != "up" or target.get("lastError") for target in selected):
            raise RuntimeError("target is not healthy: " + pool)
        last_scrapes = [target.get("lastScrape") for target in selected]
        if any(not value or value.startswith("0001-") for value in last_scrapes):
            raise RuntimeError("target has not completed a scrape: " + pool)
        if expected_nodes is not None:
            observed_nodes = {target.get("labels", {}).get("node") for target in selected}
            if observed_nodes != expected_nodes:
                raise RuntimeError("Cilium Agent node coverage differs: " + repr(observed_nodes))

    agent_up = samples_by_node(
        base_url, "Cilium Agent", 'up{job="cilium-agent"}', timeout, max_age
    )
    if set(agent_up) != EXPECTED_AGENT_NODES or any(value != "1" for value in agent_up.values()):
        raise RuntimeError("Cilium Agent up samples are incomplete or unhealthy: " + repr(agent_up))
    agent_metrics = samples_by_node(
        base_url,
        "Cilium Agent metrics",
        'count by (node) ({job="cilium-agent",__name__=~".+"})',
        timeout,
        max_age,
    )
    if set(agent_metrics) != EXPECTED_AGENT_NODES or any(float(value) <= 1 for value in agent_metrics.values()):
        raise RuntimeError("Cilium Agent telemetry samples are missing: " + repr(agent_metrics))

    ipam_capacity = samples_by_node(
        base_url, "Cilium IPAM capacity", 'sum by (node) (cilium_ipam_capacity{job="cilium-agent"})',
        timeout, max_age)
    ipam_usage = samples_by_node(
        base_url, "Cilium IPAM utilization",
        '(sum by (node, family) (cilium_ip_addresses{job="cilium-agent"}) '
        '/ sum by (node, family) (cilium_ipam_capacity{job="cilium-agent"}) * 100)',
        timeout, max_age)
    if (set(ipam_capacity) != EXPECTED_AGENT_NODES or set(ipam_usage) != EXPECTED_AGENT_NODES or
            any(float(value) <= 0 for value in ipam_capacity.values()) or
            any(not 0 <= float(value) <= 100 for value in ipam_usage.values())):
        raise RuntimeError("Cilium IPAM capacity or utilization samples are invalid")
    bpf_pressure = samples_by_node(
        base_url, "Cilium eBPF map pressure",
        'max by (node) (cilium_bpf_map_pressure{job="cilium-agent"} * 100)',
        timeout, max_age)
    if set(bpf_pressure) != EXPECTED_AGENT_NODES or any(float(value) < 0 for value in bpf_pressure.values()):
        raise RuntimeError("Cilium eBPF map pressure samples are incomplete or invalid")
    unreachable = samples_by_node(
        base_url, "Cilium unreachable-node count",
        'max by (node) (cilium_node_health_connectivity_status'
        '{job="cilium-agent",type="node",status="unreachable"})', timeout, max_age)
    if set(unreachable) != EXPECTED_AGENT_NODES or any(float(value) != 0 for value in unreachable.values()):
        raise RuntimeError("Cilium node connectivity is degraded or telemetry is missing: " + repr(unreachable))
    operator_up = query(base_url, 'up{job="operator"}', timeout)
    if not operator_up or any(str(item.get("value", [None, "0"])[1]) != "1" for item in operator_up):
        raise RuntimeError("Cilium Operator target is not healthy")
    operator_metrics = query(base_url, 'count({job="operator",__name__=~".+"})', timeout)
    if not operator_metrics or float(operator_metrics[0].get("value", [None, "0"])[1]) <= 1:
        raise RuntimeError("Cilium Operator telemetry samples are missing")

    rules = fetch_json(base_url, "/api/v1/rules", timeout)
    if rules.get("status") != "success":
        raise RuntimeError("Prometheus rules API did not return success")
    observed_rules = {
        rule.get("name"): rule
        for group in rules.get("data", {}).get("groups", [])
        for rule in group.get("rules", [])
        if rule.get("name") in EXPECTED_RULES
    }
    if set(observed_rules) != EXPECTED_RULES:
        raise RuntimeError("Cilium health rules were not loaded by Prometheus")
    if any(rule.get("health") != "ok" or rule.get("state") != "inactive" for rule in observed_rules.values()):
        raise RuntimeError("Cilium rules are unhealthy or unexpectedly firing")

    print("Cilium live monitoring E2E: PASS")
    print("Agent targets: " + ", ".join(sorted(agent_up)))
    print("Agent metric series: " + ", ".join(f"{node}={agent_metrics[node]}" for node in sorted(agent_metrics)))
    print("IPAM capacity: " + ", ".join(f"{node}={ipam_capacity[node]}" for node in sorted(ipam_capacity)))
    print("IPAM utilization percent: " + ", ".join(f"{node}={float(ipam_usage[node]):.1f}" for node in sorted(ipam_usage)))
    print("Maximum observed eBPF map pressure percent: " +
          ", ".join(f"{node}={float(bpf_pressure[node]):.2f}" for node in sorted(bpf_pressure)))
    print("Unreachable-node counts: " + ", ".join(f"{node}={unreachable[node]}" for node in sorted(unreachable)))
    print("Operator targets: " + str(len(operator_up)) + "; Operator metric series: " + operator_metrics[0]["value"][1])
    print("Health rules: loaded, healthy, and inactive (no failure was injected)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", required=True, help="Prometheus base URL; loopback HTTP or verified HTTPS")
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--max-age", type=int, default=180)
    args = parser.parse_args()
    parsed = urlsplit(args.prometheus_url)
    if (args.confirm != "atlas-cilium-live-e2e-28-30" or parsed.scheme not in ("http", "https") or
            not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or
            (parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1", "localhost")) or
            not 1 <= args.timeout <= 60 or not 30 <= args.max_age <= 86400):
        parser.error("refusing unsafe URL, parameters, or missing explicit lab confirmation")
    verify(args.prometheus_url.rstrip("/"), args.timeout, args.max_age)


if __name__ == "__main__":
    main()
