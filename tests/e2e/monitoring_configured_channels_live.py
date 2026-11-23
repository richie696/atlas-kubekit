#!/usr/bin/env python3
"""Verify firing and recovery through the configured SMTP and WeCom relay routes."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid


EXPECTED_NODES = {
    "cp1": "10.20.1.34", "cp2": "10.20.1.35", "cp3": "10.20.1.36",
    "w1": "10.20.1.37", "w2": "10.20.1.38", "w3": "10.20.1.39",
}
NAMESPACE = "monitoring"
RELEASE = "kube-prometheus-stack"
OWNER = "atlas-kubekit.io/managed-by"


def kubectl(kubeconfig, *args, input_text=None, check=True, timeout=45):
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), "--request-timeout=20s", *args],
        input=input_text, text=True, capture_output=True, timeout=timeout,
    )
    if check and result.returncode:
        # Do not emit stderr: kubectl diagnostics can contain object values.
        raise RuntimeError("kubectl operation failed: " + " ".join(args[:4]))
    return result


def resource(kubeconfig, *args):
    try:
        return json.loads(kubectl(kubeconfig, *args, "-o", "json").stdout)
    except (ValueError, TypeError):
        raise RuntimeError("Kubernetes returned invalid JSON") from None


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def forward(kubeconfig, resource_name, *ports):
    process = subprocess.Popen(
        ["kubectl", "--kubeconfig", str(kubeconfig), "-n", NAMESPACE,
         "port-forward", "--address=127.0.0.1", resource_name,
         *(f"{local}:{remote}" for local, remote in ports)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    return process


def get_json(url, timeout=5, headers=None):
    with urlopen(Request(url, headers=headers or {}), timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_ok(url, timeout=5):
    with urlopen(url, timeout=timeout) as response:
        return response.status == 200


def wait_for(predicate, timeout, message, processes=()):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            failed = next(process for process in processes if process.poll() is not None)
            try:
                detail = failed.communicate(timeout=1)[0].strip().splitlines()[-1:]
            except (OSError, subprocess.TimeoutExpired):
                detail = []
            suffix = " (" + detail[0][:240] + ")" if detail else ""
            raise RuntimeError("kubectl port-forward exited during E2E" + suffix)
        try:
            value = predicate()
            if value:
                return value
        except (OSError, URLError, ValueError, KeyError, TypeError, IndexError):
            pass
        time.sleep(5)
    raise RuntimeError(message)


def api_query(base, query):
    data = get_json(base + "/api/v1/query?" + urlencode({"query": query}))
    if data.get("status") != "success":
        raise RuntimeError("Prometheus query failed")
    return data.get("data", {}).get("result", [])


def accepted_count(base):
    samples = api_query(base, 'atlas_relay_deliveries_total{outcome="accepted"}')
    return sum(int(float(sample["value"][1])) for sample in samples)


def alert_is_firing(base, name):
    data = get_json(base + "/api/v1/alerts").get("data", {}).get("alerts", [])
    return any(item.get("labels", {}).get("alertname") == name and item.get("state") == "firing"
               for item in data)


def rule_manifest(name, firing):
    return {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
        "metadata": {"name": name, "namespace": NAMESPACE,
                     "labels": {"release": RELEASE, OWNER: "monitoring",
                                "atlas-kubekit.io/e2e": "configured-channels"}},
        "spec": {"groups": [{"name": "atlas.notification.delivery.e2e", "interval": "15s",
                              "rules": [{"alert": name, "expr": "vector(1) > 0" if firing else "vector(0) > 0",
                                         "for": "0s",
                                         "labels": {"cluster": "atlas-monitoring-e2e", "namespace": NAMESPACE,
                                                    "severity": "warning", "node": "notification-test"},
                                         "annotations": {
                                             "summary": "监控告警渠道 E2E 测试",
                                             "description": "验证告警触发与恢复经邮件和企业微信通知渠道送达。",
                                             "observed_value": "仅用于隔离测试集群验证"}}]}]},
    }


def validate_environment(kubeconfig):
    if not kubeconfig.is_absolute() or not kubeconfig.is_file() or kubeconfig.stat().st_mode & 0o077:
        raise RuntimeError("provide an absolute kubeconfig with permissions 0600 or stricter")
    nodes = resource(kubeconfig, "get", "nodes").get("items", [])
    observed = {}
    for node in nodes:
        meta = node.get("metadata", {})
        ip = next((address.get("address") for address in node.get("status", {}).get("addresses", [])
                   if address.get("type") == "InternalIP"), None)
        ready = any(condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in node.get("status", {}).get("conditions", []))
        if not ready:
            raise RuntimeError("a monitoring cluster node is not Ready")
        observed[meta.get("name")] = ip
    if observed != EXPECTED_NODES:
        raise RuntimeError("refusing an unexpected cluster identity")

    marker = resource(kubeconfig, "-n", NAMESPACE, "get", "configmap", "atlas-monitoring-topology")
    if marker.get("data", {}).get("mode") != "center":
        raise RuntimeError("the target is not marked as an independent monitoring center")
    routes = resource(kubeconfig, "-n", NAMESPACE, "get", "configmap", "atlas-notification-relay")
    try:
        configured = json.loads(routes["data"]["routes.json"])["routes"]
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("the managed notification route configuration is invalid") from None
    kinds = {item.get("kind") for item in configured if item.get("receiver") in ("atlas-email", "atlas-wecom")}
    if kinds != {"email", "wecom"}:
        raise RuntimeError("both the SMTP and WeCom routes must be present before testing")

    alertmanager = resource(kubeconfig, "-n", NAMESPACE, "get", "alertmanager",
                            RELEASE + "-alertmanager")
    manager_spec = alertmanager.get("spec", {})
    selector = manager_spec.get("alertmanagerConfigSelector", {}).get("matchLabels", {})
    if selector.get(OWNER) != "monitoring":
        raise RuntimeError("Alertmanager does not select script-managed notification routes")
    pod = resource(kubeconfig, "-n", NAMESPACE, "get", "pod",
                   "alertmanager-" + RELEASE + "-alertmanager-0")
    if pod.get("status", {}).get("phase") != "Running" or len(pod.get("status", {}).get("containerStatuses", [])) != 3:
        raise RuntimeError("Alertmanager and notification relay are not all running")

    name = "atlas-relay-" + uuid.uuid4().hex[:12]
    existing = kubectl(kubeconfig, "-n", NAMESPACE, "get", "prometheusrule", name,
                       "--ignore-not-found", "-o", "name", check=False)
    if existing.returncode or existing.stdout.strip():
        raise RuntimeError("temporary alert name collision")
    return name, pod["metadata"]["name"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != "atlas-monitoring-channels-e2e-34-39":
        parser.error("explicit confirmation atlas-monitoring-channels-e2e-34-39 is required")

    name, alertmanager_pod = validate_environment(args.kubeconfig)
    prom_port, metrics_port = free_port(), free_port()
    prom_forward = forward(args.kubeconfig, "svc/" + RELEASE + "-prometheus", (prom_port, 9090))
    relay_forward = forward(args.kubeconfig, "pod/" + alertmanager_pod,
                            (metrics_port, 18081))
    rule_created = False
    try:
        prom = f"http://127.0.0.1:{prom_port}"
        metrics_url = f"http://127.0.0.1:{metrics_port}/metrics"
        wait_for(lambda: http_ok(prom + "/-/ready"), 60,
                 "Prometheus did not become ready", (prom_forward, relay_forward))
        token_result = resource(args.kubeconfig, "-n", NAMESPACE, "get", "secret",
                                "atlas-relay-auth-c466d7b5")
        try:
            token = __import__("base64").b64decode(token_result["data"]["token"], validate=True).decode("ascii")
        except (KeyError, ValueError, UnicodeError):
            raise RuntimeError("relay metrics authentication is not configured") from None
        headers = {"Authorization": "Bearer " + token}
        wait_for(lambda: "atlas_relay_deliveries_total" in
                 urlopen(Request(metrics_url, headers=headers), timeout=5).read().decode("ascii"),
                 60, "notification relay metrics endpoint is unavailable", (prom_forward, relay_forward))
        wait_for(lambda: bool(api_query(prom, 'up{namespace="monitoring",pod="' +
                 alertmanager_pod + '"} == 1')), 180,
                 "Prometheus is not scraping the authenticated relay metrics endpoint",
                 (prom_forward, relay_forward))

        wait_for(lambda: bool(api_query(prom, 'atlas_relay_deliveries_total{outcome="accepted"}')), 90,
                 "Prometheus has no accepted relay counter", (prom_forward, relay_forward))
        baseline = accepted_count(prom)
        alert_name = "AtlasNotifyE2E" + uuid.uuid4().hex[:10]
        created = rule_manifest(name, True)
        created["spec"]["groups"][0]["rules"][0]["alert"] = alert_name
        kubectl(args.kubeconfig, "-n", NAMESPACE, "create", "-f", "-", input_text=json.dumps(created))
        rule_created = True
        wait_for(lambda: alert_is_firing(prom, alert_name), 180,
                 "the temporary Prometheus alert did not enter firing state", (prom_forward, relay_forward))
        wait_for(lambda: accepted_count(prom) >= baseline + 2, 300,
                 "firing notification was not accepted by both external routes",
                 (prom_forward, relay_forward))

        accepted_firing = accepted_count(prom)
        recovered = rule_manifest(name, False)
        recovered["spec"]["groups"][0]["rules"][0]["alert"] = alert_name
        kubectl(args.kubeconfig, "-n", NAMESPACE, "apply", "-f", "-", input_text=json.dumps(recovered))
        wait_for(lambda: not alert_is_firing(prom, alert_name), 180,
                 "the temporary Prometheus alert did not leave firing state", (prom_forward, relay_forward))
        wait_for(lambda: accepted_count(prom) >= accepted_firing + 2, 420,
                 "recovery notification was not accepted by both external routes",
                 (prom_forward, relay_forward))
        print("MONITORING_CHANNELS_E2E_PASS nodes=6 routes=smtp,wecom firing=accepted recovery=accepted html=email metric_scrape=up")
    finally:
        if rule_created:
            kubectl(args.kubeconfig, "-n", NAMESPACE, "delete", "prometheusrule", name,
                    "--ignore-not-found", "--wait=true", "--timeout=60s", check=False, timeout=70)
        for process in (prom_forward, relay_forward):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
