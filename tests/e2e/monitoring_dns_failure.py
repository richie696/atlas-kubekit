# SPDX-License-Identifier: MIT
"""Exercise CoreDNS and its managed rules in the disposable Operator cluster."""
from ipaddress import IPv4Address
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace
from urllib.request import urlopen

from scripts.operations import configuration
from scripts.operations.capabilities import monitoring_dns
from scripts.operations.changes import execution, plans
from scripts.operations.changes.coordinator import ClusterCoordinator
from tests.e2e.monitoring_kubernetes import command

NAME = "atlas-dns-e2e"
ZONE = "atlas-dns.test"
UPSTREAM_PORT = 1053
MISSING = "AtlasCoreDNSTargetOrTelemetryMissing"
ERROR = "AtlasCoreDNSResponseErrorRateHigh"
LATENCY = "AtlasCoreDNSP95LatencyHigh"
REPLICAS = "AtlasCoreDNSReplicasBelowMinimum"


def get_json(kubectl, *args):
    return json.loads(command([*kubectl, *args, "-o", "json"], timeout=25))


def prometheus_data(url, path):
    with urlopen(url + path, timeout=5) as response:
        payload = json.load(response)
    if payload["status"] != "success":
        raise RuntimeError("Prometheus request did not succeed")
    return payload["data"]


def wait_for(description, observe, traffic=None, timeout=100):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if traffic is not None:
            traffic()
        last = observe()
        if last[0]:
            return last[1]
        time.sleep(2)
    raise RuntimeError(description + " did not converge: " + repr(last))


def create_fixture(kubectl):
    fixture = Path(__file__).with_name("monitoring_dns_fixture.py").read_text(encoding="utf-8")
    labels = {"app": NAME}
    resources = [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": NAME},
         "data": {"dns.py": fixture}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": NAME},
         "spec": {"selector": labels, "ports": [
             {"name": "dns", "port": UPSTREAM_PORT, "protocol": "UDP"}]}},
        {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": NAME, "labels": labels},
         "spec": {"automountServiceAccountToken": False,
                  "containers": [{"name": "dns", "image": "python:3.13-slim",
                                  "imagePullPolicy": "Never",
                                  "command": ["python", "-u", "/fixture/dns.py", "serve"],
                                  "volumeMounts": [{"name": "fixture", "mountPath": "/fixture", "readOnly": True}]}],
                  "volumes": [{"name": "fixture", "configMap": {"name": NAME}}]}},
    ]
    for resource in resources:
        command([*kubectl, "create", "-f", "-"], data=resource, timeout=25)
    command([*kubectl, "wait", "--for=condition=Ready", "pod/" + NAME, "--timeout=60s"])
    return str(IPv4Address(get_json(kubectl, "get", "service", NAME)["spec"]["clusterIP"]))


def replace_corefile(kubectl, corefile):
    command([*kubectl, "-n", "kube-system", "patch", "configmap", "coredns", "--type=merge",
             "-p", json.dumps({"data": {"Corefile": corefile}})])
    deployment = ["-n", "kube-system", "rollout"]
    command([*kubectl, *deployment, "restart", "deployment/coredns"])
    command([*kubectl, *deployment, "status", "deployment/coredns", "--timeout=150s"], timeout=160)


def run(kubectl, url, environment, args):
    monitors = get_json(kubectl, "get", "servicemonitor", "-l", "release=stack")["items"]
    names = [item["metadata"]["name"] for item in monitors if item["metadata"]["name"].endswith("-coredns")]
    if len(names) != 1:
        raise RuntimeError("expected one chart-installed CoreDNS ServiceMonitor")
    monitor = names[0]
    pool = "serviceMonitor/monitoring/" + monitor + "/0"
    corefile = get_json(kubectl, "-n", "kube-system", "get", "configmap", "coredns")["data"]["Corefile"]
    if len(re.findall(r"(?m)^\s*forward\s+\.\s+", corefile)) != 1 or ZONE in corefile:
        raise RuntimeError("unexpected default CoreDNS Corefile")
    upstream = create_fixture(kubectl)
    service = get_json(kubectl, "-n", "kube-system", "get", "service", "kube-dns")
    resolver = service["spec"]["clusterIP"]
    updated = re.sub(r"(?m)^([ \t]+)(forward[ \t]+\.[ \t]+)",
                     rf"\g<1>forward {ZONE} {upstream}:{UPSTREAM_PORT}\n\g<1>\g<2>",
                     corefile, count=1)
    if updated == corefile:
        raise RuntimeError("failed to insert the isolated DNS upstream into the Corefile")

    def fixture(mode, *arguments):
        return command([*kubectl, "exec", NAME, "--", "python", "/fixture/dns.py",
                        mode, *arguments], timeout=20)

    def query_fixtures(expected):
        result = json.loads(fixture("probe", ZONE))
        if result["resolver"] != resolver or set(result["rcodes"]) != {expected} or result["queries"] != 32:
            raise RuntimeError("pod DNS results did not match the injected upstream: " + repr(result))
        if expected == 0 and not result["valid_answers"]:
            raise RuntimeError("DNS success did not contain the expected A record")
        return result

    def targets():
        return [item for item in prometheus_data(url, "/api/v1/targets?state=active")["activeTargets"]
                if item["scrapePool"] == pool]

    def ready_targets():
        active = targets()
        resources = ["-n", "kube-system"]
        replicas = get_json(kubectl, *resources, "get", "deployment", "coredns")["spec"]["replicas"]
        pods = get_json(kubectl, *resources, "get", "pods", "-l", "k8s-app=kube-dns")["items"]
        ready = {pod["metadata"]["name"] for pod in pods if not pod["metadata"].get("deletionTimestamp")
                 and any(item["type"] == "Ready" and item["status"] == "True"
                         for item in pod.get("status", {}).get("conditions", []))}
        available = [item["labels"] for item in active
                     if item["health"] == "up" and item["labels"].get("namespace") == "kube-system"]
        actual = {item.get("pod") for item in available}
        return len(available) == len(active) == replicas > 0 and actual == ready, available

    def evaluate(expected, traffic=0):
        def observe():
            rows = [row for group in prometheus_data(url, "/api/v1/rules")["groups"]
                    if group["name"] == "atlas.dns.health" for row in group["rules"]]
            states = {name: sorted(row["state"] for row in rows if row["name"] == name)
                      for name in expected}
            ok = True
            for name, state in expected.items():
                count = 1 if name == REPLICAS else len(registered)
                matches = [row for row in rows if row["name"] == name]
                ok &= len(matches) == count and all(
                    row["state"] == state and row["health"] == "ok" and
                    row["labels"]["risk_id"] == "R-NET-01" and
                    "CoreDNS" in row["annotations"]["summary"] and
                    bool(re.search(r"[\u4e00-\u9fff]", row["annotations"]["description"])) for row in matches)
                if state == "firing" and name in (ERROR, LATENCY):
                    # During the injection almost all resolver queries are test SERVFAILs.
                    # Require the reported ratio to reach that range, independently of QPS.
                    suffix, threshold = ("%", 90) if name == ERROR else ("s", 0.1)
                    for row in matches:
                        firing = [alert for alert in row.get("alerts", []) if alert["state"] == "firing"]
                        ok &= len(firing) == 1 and all(
                            alert["annotations"]["observed_value"].endswith(suffix) and
                            float(alert["annotations"]["observed_value"][:-1]) > threshold for alert in firing)
            return ok, states

        return wait_for("CoreDNS rule states", observe, lambda: query_fixtures(traffic))

    selector_changed = False
    try:
        replace_corefile(kubectl, updated)
        registered = wait_for("chart-selected CoreDNS metrics targets", ready_targets)
        services = {row["service"] for row in registered}
        if len(services) != 1 or "kube-dns" in services:
            raise RuntimeError("expected a dedicated CoreDNS metrics Service")
        metrics_service = services.pop()
        selector_key = "atlas-e2e-metrics-disabled"
        metrics_spec = get_json(kubectl, "-n", "kube-system", "get", "service", metrics_service)["spec"]
        if not metrics_spec.get("selector") or selector_key in metrics_spec["selector"]:
            raise RuntimeError("unexpected CoreDNS metrics Service selector")

        def select_metrics(enabled):
            patch = {"spec": {"selector": {selector_key: None if enabled else "true"}}}
            command([*kubectl, "-n", "kube-system", "patch", "service", metrics_service,
                     "--type=merge", "-p", json.dumps(patch)])

        dns_args = SimpleNamespace(**vars(args))
        dns_args.monitoring_target = [row["job"] + "=" + row["instance"] for row in registered]
        dns_args.monitoring_max_age = 20
        dns_args.monitoring_dns_window_seconds = 30
        dns_args.monitoring_dns_min_requests = 20
        values = monitoring_dns.from_arguments(dns_args, configuration.template(), environment)
        observation = monitoring_dns.observe(values, environment)
        client = environment.client
        plan = plans.make("monitoring-dns-rules", client.cluster_identity(), observation, values)
        execution.execute(plan.record(), plan.id, ClusterCoordinator(client),
                          lambda params: monitoring_dns.observe(params, environment),
                          lambda params, checkpoint: monitoring_dns.apply(params, environment, checkpoint, plan.id),
                          monitoring_dns.parameters)
        healthy = {MISSING: "inactive", ERROR: "inactive", LATENCY: "inactive", REPLICAS: "inactive"}
        evaluate(healthy)
        print("CoreDNS E2E: the cluster resolver returned the test A record and Prometheus scraped all registered replicas.", flush=True)

        fixture("mode", "servfail")
        evaluate({**healthy, ERROR: "firing"}, traffic=2)
        print("CoreDNS E2E: real SERVFAIL responses fired the error-rate alert while scrapes and replicas stayed healthy.", flush=True)

        fixture("mode", "slow")
        result = query_fixtures(0)
        if result["minimum_seconds"] < 0.25:
            raise RuntimeError("controlled upstream response was not delayed")
        evaluate({**healthy, LATENCY: "firing"})
        print("CoreDNS E2E: slow successful replies fired the P95 rule and the response-error alert recovered.", flush=True)

        fixture("mode", "normal")
        evaluate(healthy)
        select_metrics(False)
        selector_changed = True
        evaluate({**healthy, MISSING: "firing"})
        if targets():
            raise RuntimeError("the metrics Service still contributed active scrape targets")
        select_metrics(True)
        selector_changed = False
        wait_for("restored chart CoreDNS metrics targets", ready_targets)
        evaluate(healthy)
        print("CoreDNS E2E: DNS stayed available through scrape loss, and all four rule types recovered after restoration.", flush=True)
    finally:
        if selector_changed:
            select_metrics(True)
        replace_corefile(kubectl, corefile)
        actual = get_json(kubectl, "-n", "kube-system", "get", "configmap", "coredns")["data"]["Corefile"]
        if actual != corefile:
            raise RuntimeError("original CoreDNS configuration was not restored")
