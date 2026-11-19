#!/usr/bin/env python3
"""Verify live API server, scheduler, controller-manager and etcd monitoring."""
import argparse
from datetime import datetime, timezone
import json
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen


EXPECTED_TARGETS = {
    "apiserver": "10.20.1.28:6443",
    "kube-etcd": "10.20.1.28:2381",
    "kube-scheduler": "10.20.1.28:10259",
    "kube-controller-manager": "10.20.1.28:10257",
}
EXPECTED_ALERTS = {
    "KubeAPIDown", "KubeAPIInstanceUnreachable", "KubeAPIErrorBudgetBurn",
    "KubeAPITerminatedRequests", "KubeSchedulerDown", "KubeSchedulerInstanceUnreachable",
    "KubeControllerManagerDown", "KubeControllerManagerInstanceUnreachable",
    "etcdMembersDown", "etcdInsufficientMembers", "etcdNoLeader",
    "etcdHighNumberOfLeaderChanges", "etcdHighNumberOfFailedGRPCRequests",
    "etcdGRPCRequestsSlow", "etcdMemberCommunicationSlow", "etcdHighNumberOfFailedProposals",
    "etcdHighFsyncDurations", "etcdHighCommitDurations", "etcdDatabaseQuotaLowSpace",
    "etcdExcessiveDatabaseGrowth", "etcdDatabaseHighFragmentationRatio",
}
METRIC_QUERIES = {
    "apiserver": 'count(apiserver_request_total{job="apiserver"})',
    "kube-etcd": 'max(etcd_server_has_leader{job="kube-etcd"})',
    "kube-scheduler": 'count({job="kube-scheduler",__name__!="up"})',
    "kube-controller-manager": 'count({job="kube-controller-manager",__name__!="up"})',
}


def get_json(base_url, path, timeout):
    with urlopen(base_url + path, timeout=timeout) as response:
        return json.load(response)


def query(base_url, expression, timeout):
    payload = get_json(base_url, "/api/v1/query?" + urlencode({"query": expression}), timeout)
    if payload.get("status") != "success":
        raise RuntimeError("Prometheus query failed")
    return payload.get("data", {}).get("result", [])


def parse_timestamp(value):
    # Prometheus may return RFC3339 timestamps with nanosecond fractions;
    # datetime.fromisoformat accepts at most microsecond precision.
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", value)
    return datetime.fromisoformat(normalized.replace("Z", "+00:00")).timestamp()


def verify(base_url, timeout, max_age):
    payload = get_json(base_url, "/api/v1/targets?state=active", timeout)
    if payload.get("status") != "success":
        raise RuntimeError("Prometheus target API failed")
    targets = payload.get("data", {}).get("activeTargets", [])
    now = datetime.now(timezone.utc).timestamp()
    for job, instance in EXPECTED_TARGETS.items():
        matching = [target for target in targets if target.get("labels", {}).get("job") == job]
        if len(matching) != 1:
            raise RuntimeError(job + " scrape target count is " + str(len(matching)) + ", expected 1")
        target = matching[0]
        scrape_time = target.get("lastScrape")
        if (target.get("health") != "up" or target.get("lastError") or
                target.get("labels", {}).get("instance") != instance or not scrape_time or
                scrape_time.startswith("0001-")):
            raise RuntimeError(job + " target is unhealthy or mismatched")
        parsed = parse_timestamp(scrape_time)
        if now - parsed > max_age or parsed - now > 30:
            raise RuntimeError(job + " target sample is stale")

    samples = {}
    for job, expression in METRIC_QUERIES.items():
        values = query(base_url, expression, timeout)
        if len(values) != 1:
            raise RuntimeError(job + " real component metrics are absent or ambiguous")
        item = values[0].get("value", [])
        if len(item) != 2 or str(item[1]) in ("NaN", "+Inf", "-Inf"):
            raise RuntimeError(job + " returned an invalid metric sample")
        metric_value = float(item[1])
        if job == "kube-etcd" and metric_value != 1:
            raise RuntimeError("etcd leader metric does not report a leader")
        if job != "kube-etcd" and metric_value <= 0:
            raise RuntimeError(job + " has no observed component metric series")
        if now - float(item[0]) > max_age or float(item[0]) - now > 30:
            raise RuntimeError(job + " component metric sample is stale")
        samples[job] = str(item[1])

    rules = get_json(base_url, "/api/v1/rules", timeout)
    if rules.get("status") != "success":
        raise RuntimeError("Prometheus rules API failed")
    selected = [rule for group in rules.get("data", {}).get("groups", [])
                for rule in group.get("rules", []) if rule.get("name") in EXPECTED_ALERTS]
    present = {rule.get("name") for rule in selected}
    missing = EXPECTED_ALERTS - present
    if missing:
        raise RuntimeError("default control-plane rules are missing: " + ", ".join(sorted(missing)))
    unhealthy = [rule.get("name") for rule in selected
                 if rule.get("health") != "ok" or rule.get("state") != "inactive"]
    if unhealthy:
        raise RuntimeError("control-plane rules are unhealthy or unexpectedly firing: " + repr(unhealthy))
    print("Control-plane live monitoring E2E: PASS")
    for job in EXPECTED_TARGETS:
        print(job + ": target up; real metric sample=" + samples[job])
    print("Prometheus default control-plane and etcd alert rules: loaded, healthy, inactive")
    print("No control-plane or etcd fault was injected.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--max-age", type=int, default=180)
    args = parser.parse_args()
    parsed = urlsplit(args.prometheus_url)
    if (args.confirm != "atlas-control-plane-live-e2e-28-30" or parsed.scheme not in ("http", "https") or
            not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or
            (parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1", "localhost")) or
            not 1 <= args.timeout <= 60 or not 30 <= args.max_age <= 86400):
        parser.error("refusing unsafe URL, parameters, or missing explicit lab confirmation")
    verify(args.prometheus_url.rstrip("/"), args.timeout, args.max_age)


if __name__ == "__main__":
    main()
