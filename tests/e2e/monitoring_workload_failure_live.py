#!/usr/bin/env python3
"""Exercise real image-pull, CrashLoop and OOMKilled alerts in the retained isolated lab."""
import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


EXPECTED_NODES = {"cp1": "10.20.1.28", "w1": "10.20.1.29", "w2": "10.20.1.30"}
CONTEXT = "atlas-e2e-lab"
MONITORING = "monitoring"
PROMETHEUS = "kube-prometheus-stack-prometheus"
RELEASE = "kube-prometheus-stack"
BUSYBOX = "m.daocloud.io/docker.io/library/busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"


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
    rule = resource(client, "-n", MONITORING, "get", "prometheusrule", "atlas-workload-health")
    labels = rule.get("metadata", {}).get("labels", {})
    rule_names = {item.get("alert") for group in rule.get("spec", {}).get("groups", [])
                  for item in group.get("rules", [])}
    required = {"AtlasPodContainerStartFailure", "AtlasPodContainerRestartBurst", "AtlasPodOOMKilled"}
    if (labels.get("release") != RELEASE or labels.get("atlas-kubekit.io/managed-by") != "monitoring" or
            not required.issubset(rule_names)):
        raise RuntimeError("managed workload failure rules are not selected as expected")
    configs = resource(client, "get", "alertmanagerconfigs.monitoring.coreos.com", "--all-namespaces")
    if configs.get("items"):
        raise RuntimeError("refusing to send real alerts while AlertmanagerConfig routes are present")
    parsed = command(client + ["config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"])
    if not parsed.stdout.startswith("https://"):
        raise RuntimeError("kubeconfig API endpoint is not HTTPS")
    with urlopen(prometheus_url.rstrip("/") + "/-/ready", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("Prometheus is not ready")


def query(prometheus_url, expression):
    with urlopen(prometheus_url.rstrip("/") + "/api/v1/query?" + urlencode({"query": expression}),
                 timeout=5) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError("Prometheus query failed")
    return payload.get("data", {}).get("result", [])


def matching_alerts(prometheus_url, namespace, pod, alert_names):
    with urlopen(prometheus_url.rstrip("/") + "/api/v1/alerts", timeout=5) as response:
        alerts = json.load(response)["data"]["alerts"]
    return [item for item in alerts if item.get("labels", {}).get("alertname") in alert_names
            and item.get("labels", {}).get("namespace") == namespace
            and item.get("labels", {}).get("pod") == pod]


def wait_for(client, prom_url, namespace, pod, names, predicate, timeout, description):
    deadline = time.monotonic() + timeout
    last = []
    while time.monotonic() < deadline:
        last = matching_alerts(prom_url, namespace, pod, names)
        if predicate(last):
            print("PASS: " + description)
            return last
        time.sleep(5)
    pod_json = command(client + ["-n", namespace, "get", "pod", pod, "-o", "json"], check=False)
    status = pod_json.stdout[:3000] if pod_json.returncode == 0 else pod_json.stderr[:1000]
    raise RuntimeError(description + " did not complete; alerts=" + repr(last) + "; pod=" + status)


def verify_pod_status(client, namespace, pod, reason):
    result = resource(client, "-n", namespace, "get", "pod", pod)
    statuses = result.get("status", {}).get("containerStatuses", [])
    if not statuses:
        raise RuntimeError("pod has no container status: " + pod)
    item = statuses[0]
    terminated = item.get("lastState", {}).get("terminated", {})
    if terminated.get("reason") != reason or item.get("restartCount", 0) < 1:
        raise RuntimeError("expected restarted container with last termination " + reason + ": " +
                           json.dumps(item, ensure_ascii=False))


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
    namespace = "atlas-workload-failure-e2e-" + suffix
    created = False
    active_error = False
    try:
        command(client + ["create", "namespace", namespace])
        created = True
        pull_pod = "image-pull-probe"
        pull_manifest = {"apiVersion": "v1", "kind": "Pod",
                         "metadata": {"name": pull_pod, "namespace": namespace,
                                      "labels": {"app.kubernetes.io/managed-by": "atlas-kubekit-e2e"}},
                         "spec": {"automountServiceAccountToken": False,
                                  "containers": [{"name": "probe",
                                                  "image": "registry.atlas-e2e.invalid/not-found/probe:missing",
                                                  "imagePullPolicy": "Always"}]}}
        command(client + ["apply", "-f", "-"], input_text=json.dumps(pull_manifest))
        deadline = time.monotonic() + 180
        pull_failure_observed = False
        while time.monotonic() < deadline:
            pod_json = resource(client, "-n", namespace, "get", "pod", pull_pod)
            statuses = pod_json.get("status", {}).get("containerStatuses", [])
            events = resource(client, "-n", namespace, "get", "events").get("items", [])
            waiting = statuses[0].get("state", {}).get("waiting", {}).get("reason") if statuses else None
            has_pull_failure = any(event.get("reason") in {"Failed", "BackOff"} and
                                   event.get("involvedObject", {}).get("name") == pull_pod
                                   for event in events)
            if waiting in {"ErrImagePull", "ImagePullBackOff"} and has_pull_failure:
                pull_failure_observed = True
                break
            time.sleep(5)
        if not pull_failure_observed:
            raise RuntimeError("Kubernetes did not report an image pull failure and backoff")
        print("PASS: Kubernetes reported a real unavailable-registry pull failure and backoff.", flush=True)
        wait_for(client, args.prometheus_url, namespace, pull_pod,
                 {"AtlasPodContainerStartFailure"},
                 lambda items: any(item.get("state") == "firing" and
                                    item.get("labels", {}).get("reason") in {"ErrImagePull", "ImagePullBackOff"}
                                    for item in items),
                 360, "unavailable registry fired AtlasPodContainerStartFailure with pull reason")

        common = {"automountServiceAccountToken": False,
                  "containers": [{"name": "probe", "image": BUSYBOX, "imagePullPolicy": "IfNotPresent"}]}
        crash_pod = "crashloop-probe"
        crash_manifest = {"apiVersion": "v1", "kind": "Pod",
                          "metadata": {"name": crash_pod, "namespace": namespace,
                                       "labels": {"app.kubernetes.io/managed-by": "atlas-kubekit-e2e"}},
                          "spec": {**common, "restartPolicy": "Always",
                                   "containers": [{**common["containers"][0],
                                                   "command": ["/bin/sh", "-c", "sleep 5; exit 42"]}]}}
        command(client + ["apply", "-f", "-"], input_text=json.dumps(crash_manifest))
        deadline = time.monotonic() + 180
        crashloop_observed = False
        while time.monotonic() < deadline:
            pod_json = resource(client, "-n", namespace, "get", "pod", crash_pod)
            statuses = pod_json.get("status", {}).get("containerStatuses", [])
            events = resource(client, "-n", namespace, "get", "events").get("items", [])
            has_backoff = any(event.get("reason") == "BackOff" and
                              crash_pod in event.get("message", "") for event in events)
            if statuses and statuses[0].get("restartCount", 0) >= 3 and has_backoff:
                crashloop_observed = True
                break
            time.sleep(5)
        if not crashloop_observed:
            raise RuntimeError("Kubernetes did not report repeated CrashLoop restart/backoff events")
        print("PASS: Kubernetes reported a real CrashLoop restart count and BackOff event.", flush=True)
        wait_for(client, args.prometheus_url, namespace, crash_pod,
                 {"AtlasPodContainerRestartBurst"},
                 lambda items: any(item.get("state") == "firing" for item in items),
                 360, "real repeated crash fired AtlasPodContainerRestartBurst with namespace/pod labels")

        oom_pod = "oom-probe"
        oom_manifest = {"apiVersion": "v1", "kind": "Pod",
                        "metadata": {"name": oom_pod, "namespace": namespace,
                                     "labels": {"app.kubernetes.io/managed-by": "atlas-kubekit-e2e"}},
                        "spec": {"restartPolicy": "Always", "automountServiceAccountToken": False,
                                 "containers": [{"name": "probe", "image": BUSYBOX,
                                                 "imagePullPolicy": "IfNotPresent",
                                                 "command": ["/bin/sh", "-c",
                                                             "sleep 20; payload=$(head -c 300000000 /dev/zero | tr '\\000' x); sleep 600"],
                                                 "resources": {"requests": {"memory": "64Mi"},
                                                               "limits": {"memory": "256Mi"}}}]}}
        command(client + ["apply", "-f", "-"], input_text=json.dumps(oom_manifest))
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            pod_json = command(client + ["-n", namespace, "get", "pod", oom_pod, "-o", "json"], check=False)
            if pod_json.returncode == 0:
                statuses = json.loads(pod_json.stdout).get("status", {}).get("containerStatuses", [])
                if statuses and statuses[0].get("lastState", {}).get("terminated", {}).get("reason") == "OOMKilled":
                    break
            time.sleep(5)
        else:
            raise RuntimeError("test workload did not produce a real OOMKilled termination")
        verify_pod_status(client, namespace, oom_pod, "OOMKilled")
        print("PASS: Kubernetes reports a real OOMKilled termination and container restart.", flush=True)
        wait_for(client, args.prometheus_url, namespace, oom_pod,
                 {"AtlasPodOOMKilled"},
                 lambda items: any(item.get("state") == "firing" and
                                    item.get("labels", {}).get("reason") == "OOMKilled" for item in items),
                 240, "real OOMKilled plus restart fired AtlasPodOOMKilled with correct labels")
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
            else:
                deadline = time.monotonic() + 180
                alert_names = {"AtlasPodContainerStartFailure", "AtlasPodContainerRestartBurst",
                               "AtlasPodOOMKilled"}
                while time.monotonic() < deadline:
                    if not matching_alerts(args.prometheus_url, namespace,
                                           "image-pull-probe", alert_names) and \
                            not matching_alerts(args.prometheus_url, namespace,
                                           "crashloop-probe", alert_names) and \
                            not matching_alerts(args.prometheus_url, namespace, "oom-probe", alert_names):
                        print("PASS: temporary namespace was removed and workload failure alerts resolved.",
                              flush=True)
                        break
                    time.sleep(5)
                else:
                    message = "workload alerts did not resolve after namespace cleanup"
                    if active_error:
                        print("CLEANUP_FAILED: " + message)
                    else:
                        raise RuntimeError(message)


if __name__ == "__main__":
    main()
