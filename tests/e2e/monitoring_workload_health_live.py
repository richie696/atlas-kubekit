#!/usr/bin/env python3
"""Trigger and clear the managed pending-Pod alert in the retained 10.20.1.28-30 lab."""
import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path
from urllib.request import urlopen


EXPECTED_NODES = {"cp1": "10.20.1.28", "w1": "10.20.1.29", "w2": "10.20.1.30"}
CONTEXT = "atlas-e2e-lab"
MONITORING = "monitoring"
PROMETHEUS = "kube-prometheus-stack-prometheus"
RULE = "atlas-workload-health"
RELEASE = "kube-prometheus-stack"


def command(args, *, timeout=60, check=True, input_text=None):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("command failed: " + " ".join(args) + "\n" + result.stderr[-2000:])
    return result


def resource(client, *args):
    return json.loads(command(client + list(args) + ["-o", "json"]).stdout)


def verify_lab(client, kubeconfig, context, prometheus_url):
    if context != CONTEXT or not kubeconfig.is_absolute() or not kubeconfig.is_file():
        raise RuntimeError("refusing non-lab context or missing absolute kubeconfig")
    if kubeconfig.stat().st_mode & 0o077:
        raise RuntimeError("kubeconfig permissions must be 0600 or stricter")
    selected = resource(client, "config", "view", "--minify")
    if selected.get("contexts", [{}])[0].get("name") != CONTEXT:
        raise RuntimeError("active kubeconfig context does not match the retained lab")
    nodes = resource(client, "get", "nodes").get("items", [])
    observed = {}
    for node in nodes:
        internal = next((entry.get("address") for entry in node.get("status", {}).get("addresses", [])
                         if entry.get("type") == "InternalIP"), None)
        observed[node.get("metadata", {}).get("name")] = internal
        if not any(condition.get("type") == "Ready" and condition.get("status") == "True"
                   for condition in node.get("status", {}).get("conditions", [])):
            raise RuntimeError("lab node is not Ready: " + str(node.get("metadata", {}).get("name")))
    if observed != EXPECTED_NODES:
        raise RuntimeError("refusing unexpected cluster node identity: " + repr(observed))
    prometheus = resource(client, "-n", MONITORING, "get", "prometheus", PROMETHEUS)
    if (prometheus.get("spec", {}).get("ruleSelector", {}).get("matchLabels", {}).get("release") != RELEASE or
            prometheus.get("spec", {}).get("ruleNamespaceSelector") != {}):
        raise RuntimeError("Prometheus selector differs from the retained lab contract")
    rule = resource(client, "-n", MONITORING, "get", "prometheusrule", RULE)
    labels = rule.get("metadata", {}).get("labels", {})
    rule_names = {item.get("alert") for group in rule.get("spec", {}).get("groups", [])
                  for item in group.get("rules", [])}
    if (labels.get("release") != RELEASE or labels.get("atlas-kubekit.io/managed-by") != "monitoring" or
            "AtlasPodPendingTooLong" not in rule_names):
        raise RuntimeError("managed workload health rule is not selected as expected")
    configs = resource(client, "get", "alertmanagerconfigs.monitoring.coreos.com", "--all-namespaces")
    if configs.get("items"):
        raise RuntimeError("refusing to create a real alert while AlertmanagerConfig routes are present")
    parsed = command(client + ["config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"])
    if not parsed.stdout.startswith("https://"):
        raise RuntimeError("kubeconfig API endpoint is not HTTPS")
    with urlopen(prometheus_url.rstrip("/") + "/-/ready", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("Prometheus is not ready")


def matching_alerts(prometheus_url, namespace, pod):
    with urlopen(prometheus_url.rstrip("/") + "/api/v1/alerts", timeout=5) as response:
        alerts = json.load(response)["data"]["alerts"]
    return [item for item in alerts if item.get("labels", {}).get("alertname") == "AtlasPodPendingTooLong"
            and item.get("labels", {}).get("namespace") == namespace
            and item.get("labels", {}).get("pod") == pod]


def query_prometheus(prometheus_url, expression):
    from urllib.parse import urlencode

    with urlopen(prometheus_url.rstrip("/") + "/api/v1/query?" + urlencode({"query": expression}),
                 timeout=5) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError("Prometheus query failed")
    return payload.get("data", {}).get("result", [])


def verify_job_cronjob_metrics(prometheus_url, namespace):
    expressions = {
        "active Job state": 'kube_job_status_active{namespace="' + namespace +
        '",job_name="long-running"} == 1',
        "active Job start time": 'kube_job_status_start_time{namespace="' + namespace +
        '",job_name="long-running"}',
        "CronJob next scheduled time": 'kube_cronjob_next_schedule_time{namespace="' + namespace +
        '",cronjob="schedule-probe"}',
        "CronJob suspension state": 'kube_cronjob_spec_suspend{namespace="' + namespace +
        '",cronjob="schedule-probe"} == 0',
    }
    deadline = time.monotonic() + 180
    pending = set(expressions)
    while pending and time.monotonic() < deadline:
        for description in tuple(pending):
            if query_prometheus(prometheus_url, expressions[description]):
                pending.remove(description)
        if pending:
            time.sleep(5)
    if pending:
        raise RuntimeError("kube-state-metrics did not expose: " + ", ".join(sorted(pending)))
    alerts = []
    with urlopen(prometheus_url.rstrip("/") + "/api/v1/alerts", timeout=5) as response:
        alerts = json.load(response).get("data", {}).get("alerts", [])
    unexpected = [item.get("labels", {}).get("alertname") for item in alerts
                  if item.get("labels", {}).get("namespace") == namespace and
                  item.get("labels", {}).get("alertname") in
                  ("AtlasJobRunningTooLong", "AtlasCronJobScheduleOverdue")]
    if unexpected:
        raise RuntimeError("short-lived Job/CronJob test unexpectedly fired timeout alerts: " + repr(unexpected))
    print("PASS: real kube-state-metrics exposes active Job timing and active CronJob schedule state.")


def delete_namespace(client, namespace):
    command(client + ["delete", "namespace", namespace, "--wait=true", "--timeout=90s"],
            timeout=100, check=False)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = command(client + ["get", "namespace", namespace, "--ignore-not-found", "-o", "name"],
                         check=False)
        if result.returncode:
            return "namespace cleanup could not be verified"
        if not result.stdout.strip():
            return None
        time.sleep(2)
    return "temporary namespace remains: " + namespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:19090")
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != "atlas-monitor-e2e":
        parser.error("explicit confirmation atlas-monitor-e2e is required")

    client = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context]
    verify_lab(client, args.kubeconfig, args.context, args.prometheus_url)
    suffix = uuid.uuid4().hex[:8]
    namespace, pod = "atlas-workload-e2e-" + suffix, "pending-probe"
    created = False
    active_error = False
    try:
        command(client + ["create", "namespace", namespace])
        created = True
        sleeper = {"restartPolicy": "Never", "automountServiceAccountToken": False,
                   "containers": [{"name": "sleeper", "image": "registry.k8s.io/pause:3.10",
                                   "imagePullPolicy": "IfNotPresent"}]}
        command(client + ["apply", "-f", "-"], input_text=json.dumps({
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": "long-running", "namespace": namespace,
                         "labels": {"app.kubernetes.io/managed-by": "atlas-kubekit-e2e"}},
            "spec": {"backoffLimit": 0, "template": {"spec": sleeper}},
        }))
        command(client + ["apply", "-f", "-"], input_text=json.dumps({
            "apiVersion": "batch/v1", "kind": "CronJob",
            "metadata": {"name": "schedule-probe", "namespace": namespace,
                         "labels": {"app.kubernetes.io/managed-by": "atlas-kubekit-e2e"}},
            "spec": {"schedule": "*/5 * * * *", "suspend": False,
                     "jobTemplate": {"spec": {"backoffLimit": 0, "template": {"spec": sleeper}}}},
        }))
        verify_job_cronjob_metrics(args.prometheus_url, namespace)
        manifest = {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod, "namespace": namespace,
                         "labels": {"app.kubernetes.io/managed-by": "atlas-kubekit-e2e"}},
            "spec": {"automountServiceAccountToken": False,
                     "nodeSelector": {"atlas-kubekit-e2e.invalid/pending": "true"},
                     "containers": [{"name": "probe", "image": "registry.k8s.io/pause:3.10",
                                     "imagePullPolicy": "IfNotPresent"}]},
        }
        command(client + ["apply", "-f", "-"], input_text=json.dumps(manifest))
        deadline = time.monotonic() + 180
        firing = False
        while time.monotonic() < deadline:
            current = matching_alerts(args.prometheus_url, namespace, pod)
            if any(item.get("state") == "firing" for item in current):
                firing = True
                break
            time.sleep(5)
        if not firing:
            raise RuntimeError("real Prometheus Pending Pod alert did not fire")
        print("PASS: a real unschedulable Pod fired AtlasPodPendingTooLong with namespace/pod labels.")
    except BaseException:
        active_error = True
        raise
    finally:
        if created:
            error = delete_namespace(client, namespace)
            if error:
                if active_error:
                    print("CLEANUP_FAILED: " + error)
                else:
                    raise RuntimeError(error)
            elif not active_error:
                deadline = time.monotonic() + 150
                while time.monotonic() < deadline:
                    if not matching_alerts(args.prometheus_url, namespace, pod):
                        print("PASS: temporary namespace was removed and the alert resolved.")
                        break
                    time.sleep(5)
                else:
                    raise RuntimeError("alert did not resolve after namespace cleanup")


if __name__ == "__main__":
    main()
