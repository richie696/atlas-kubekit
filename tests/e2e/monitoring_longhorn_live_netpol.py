#!/usr/bin/env python3
"""Exercise the managed Longhorn NetworkPolicy on the retained 10.20.1.28-30 lab."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.monitoring.longhorn_collection import manifests

LAB_PROFILES = {
    "retained-28-30": {
        "context": "atlas-e2e-lab",
        "nodes": {"cp1": "10.20.1.28", "w1": "10.20.1.29", "w2": "10.20.1.30"},
        "confirmation": "atlas-longhorn-live-netpol-e2e-28-30",
    },
    "install-34-36": {
        "context": "kubernetes-admin@kubernetes",
        "nodes": {"cp1": "10.20.1.34", "w1": "10.20.1.35", "w2": "10.20.1.36"},
        "confirmation": "atlas-longhorn-live-netpol-e2e-34-36",
    },
}
MONITORING_NAMESPACE = "monitoring"
LONGHORN_NAMESPACE = "longhorn-system"
PROMETHEUS_NAME = "kube-prometheus-stack-prometheus"
PROMETHEUS_RELEASE = "kube-prometheus-stack"
PROMETHEUS_POD_LABEL = "kube-prometheus-stack-prometheus"


def run(args, *, timeout=60, check=True, input_text=None):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("command failed: " + " ".join(args) + "\n" + result.stderr[-3000:])
    return result


def kubectl(client, *args, timeout=60, check=True, input_text=None):
    return run(client + list(args), timeout=timeout, check=check, input_text=input_text)


def json_resource(client, *args):
    return json.loads(kubectl(client, *args, "-o", "json").stdout)


def apply(client, value):
    kubectl(client, "apply", "-f", "-", input_text=json.dumps(value))


def verify_lab(client, kubeconfig, context, expected_nodes):
    if not context or not isinstance(expected_nodes, dict):
        raise RuntimeError("an explicit isolated-lab profile is required")
    if not kubeconfig.is_absolute() or not kubeconfig.is_file():
        raise RuntimeError("kubeconfig must be an existing absolute path")
    mode = kubeconfig.stat().st_mode & 0o777
    if mode & 0o077:
        raise RuntimeError("kubeconfig permissions must be 0600 or stricter")

    current = json_resource(client, "config", "view", "--minify")
    active_context = current.get("contexts", [{}])[0].get("name")
    if active_context != context:
        raise RuntimeError("active kubeconfig context does not match the retained lab")
    nodes = json_resource(client, "get", "nodes").get("items", [])
    observed = {}
    for item in nodes:
        addresses = item.get("status", {}).get("addresses", [])
        internal = next((address.get("address") for address in addresses
                         if address.get("type") == "InternalIP"), None)
        observed[item.get("metadata", {}).get("name")] = internal
    if observed != expected_nodes:
        raise RuntimeError("refusing unexpected cluster node identity: " + repr(observed))
    for item in nodes:
        conditions = item.get("status", {}).get("conditions", [])
        if not any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
            raise RuntimeError("lab node is not Ready: " + item["metadata"]["name"])

    cilium = json_resource(client, "-n", "kube-system", "get", "daemonset", "cilium")
    cilium_status = cilium.get("status", {})
    if (cilium_status.get("desiredNumberScheduled") != len(nodes) or
            cilium_status.get("numberReady") != len(nodes)):
        raise RuntimeError("Cilium must be Ready on all lab nodes")

    prometheus = json_resource(client, "-n", MONITORING_NAMESPACE, "get", "prometheus", PROMETHEUS_NAME)
    monitor, expected_policy = manifests(MONITORING_NAMESPACE, PROMETHEUS_RELEASE,
                                         PROMETHEUS_POD_LABEL, LONGHORN_NAMESPACE)
    actual_prom_selector = prometheus.get("spec", {}).get("serviceMonitorSelector", {}).get("matchLabels", {})
    if actual_prom_selector.get("release") != PROMETHEUS_RELEASE:
        raise RuntimeError("Prometheus ServiceMonitor selector differs from the tested lab")

    actual_monitor = json_resource(client, "-n", MONITORING_NAMESPACE, "get", "servicemonitor",
                                   monitor["metadata"]["name"])
    if actual_monitor.get("spec") != monitor["spec"]:
        raise RuntimeError("managed Longhorn ServiceMonitor differs from its expected manifest")
    actual_policy = json_resource(client, "-n", LONGHORN_NAMESPACE, "get", "networkpolicy",
                                  expected_policy["metadata"]["name"])
    if actual_policy.get("spec") != expected_policy["spec"]:
        raise RuntimeError("managed Longhorn NetworkPolicy differs from its expected manifest")

    service = json_resource(client, "-n", LONGHORN_NAMESPACE, "get", "service", "longhorn-backend")
    if (service.get("spec", {}).get("selector") != {"app": "longhorn-manager"} or
            not any(p.get("name") == "manager" and p.get("port") == 9500
                    for p in service.get("spec", {}).get("ports", []))):
        raise RuntimeError("Longhorn manager metrics Service is not the expected target")
    manager_pods = json_resource(client, "-n", LONGHORN_NAMESPACE, "get", "pods", "-l",
                                 "app=longhorn-manager").get("items", [])
    targets = [pod for pod in manager_pods
               if pod.get("status", {}).get("phase") == "Running" and
               any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in pod.get("status", {}).get("conditions", [])) and
               pod.get("spec", {}).get("nodeName") in ("w1", "w2")]
    if not targets:
        raise RuntimeError("no Ready Longhorn Manager on a worker node")
    target = next((p for p in targets if p["spec"]["nodeName"] == "w2"), targets[0])
    source_node = next(name for name in ("w1", "w2") if name != target["spec"]["nodeName"])
    target_ip = target.get("status", {}).get("podIP")
    if not target_ip:
        raise RuntimeError("Longhorn manager has no pod IP")
    return source_node, target_ip


def job(name, namespace, node, image, labels, shell):
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"app.kubernetes.io/managed-by": "atlas-kubekit-e2e"}},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 60,
                 "template": {"metadata": {"labels": labels},
                              "spec": {"automountServiceAccountToken": False,
                                       "nodeSelector": {"kubernetes.io/hostname": node},
                                       "restartPolicy": "Never",
                                       "containers": [{"name": "probe", "image": image,
                                                       "imagePullPolicy": "IfNotPresent",
                                                       "command": ["/bin/sh", "-ec", shell],
                                                       "securityContext": {"allowPrivilegeEscalation": False,
                                                                           "capabilities": {"drop": ["ALL"]},
                                                                           "runAsNonRoot": True,
                                                                           "runAsUser": 1000,
                                                                           "seccompProfile": {"type": "RuntimeDefault"}}}]}}}}


def wait_for_job(client, namespace, name):
    kubectl(client, "-n", namespace, "wait", "--for=condition=complete", "job/" + name,
            "--timeout=90s", timeout=100)
    return kubectl(client, "-n", namespace, "logs", "job/" + name).stdout


def delete_job(client, namespace, name):
    kubectl(client, "-n", namespace, "delete", "job", name, "--wait=true", "--timeout=60s",
            timeout=70, check=False)
    observed = kubectl(client, "-n", namespace, "get", "job", name, "--ignore-not-found", "-o", "name")
    return None if not observed.stdout.strip() else "temporary job remains: " + namespace + "/" + name


def delete_namespace(client, name):
    kubectl(client, "delete", "namespace", name, "--wait=true", "--timeout=90s", timeout=100, check=False)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = kubectl(client, "get", "namespace", name, "--ignore-not-found", "-o", "name", check=False)
        if result.returncode != 0:
            return "could not verify namespace cleanup: " + result.stderr[-1000:]
        if not result.stdout.strip():
            return None
        time.sleep(1)
    return "temporary namespace remains: " + name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--lab-profile", required=True, choices=tuple(LAB_PROFILES),
                        help="Exact supported isolated-lab identity and confirmation profile")
    parser.add_argument("--probe-image", required=True,
                        help="preloaded busybox image, e.g. docker.io/library/busybox:1.38.0")
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    profile = LAB_PROFILES[args.lab_profile]
    if args.context != profile["context"] or args.confirm != profile["confirmation"]:
        parser.error("context and explicit confirmation must match the selected isolated-lab profile")

    client = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context]
    source_node, target_ip = verify_lab(client, args.kubeconfig, args.context, profile["nodes"])
    suffix = uuid.uuid4().hex[:10]
    source_job = "atlas-lh-netpol-" + suffix
    unauthorized_ns = "atlas-untrusted-netpol-" + suffix
    unauthorized_job = "probe-" + suffix
    created_jobs = []
    created_namespace = False
    active_error = False
    try:
        authorized_shell = ("body=$(wget -q -T 8 -O - http://" + target_ip +
                            ":9500/metrics); printf '%s\\n' \"$body\" | "
                            "grep -q '^longhorn_volume_robustness' && "
                            "echo AUTHORIZED_SCRAPE_OK")
        blocked_shell = ("if wget -q -T 8 -O - http://" + target_ip +
                         ":9500/metrics >/dev/null; then "
                         "echo UNEXPECTED_ALLOWED; exit 1; else echo UNAUTHORIZED_BLOCKED; fi")
        apply(client, job(source_job, MONITORING_NAMESPACE, source_node, args.probe_image,
                          {"prometheus": PROMETHEUS_POD_LABEL, "app.kubernetes.io/name": "atlas-e2e-probe"},
                          authorized_shell))
        created_jobs.append((MONITORING_NAMESPACE, source_job))
        run(client + ["create", "namespace", unauthorized_ns])
        created_namespace = True
        apply(client, job(unauthorized_job, unauthorized_ns, source_node, args.probe_image,
                          {"app": "untrusted-e2e-probe"}, blocked_shell))
        created_jobs.append((unauthorized_ns, unauthorized_job))

        authorized = wait_for_job(client, MONITORING_NAMESPACE, source_job)
        blocked = wait_for_job(client, unauthorized_ns, unauthorized_job)
        if "AUTHORIZED_SCRAPE_OK" not in authorized:
            raise RuntimeError("authorized Prometheus-shaped source could not scrape real Longhorn metrics")
        if "UNAUTHORIZED_BLOCKED" not in blocked or "UNEXPECTED_ALLOWED" in blocked:
            raise RuntimeError("NetworkPolicy did not block the unrelated namespace")
        print("PASS: monitoring/w1 -> Longhorn Manager/w2 returned longhorn_volume_robustness.")
        print("PASS: unrelated namespace -> Longhorn Manager/w2 was blocked by the managed policy.")
    except BaseException:
        active_error = True
        raise
    finally:
        errors = []
        for namespace, name in reversed(created_jobs):
            error = delete_job(client, namespace, name)
            if error:
                errors.append(error)
        if created_namespace:
            error = delete_namespace(client, unauthorized_ns)
            if error:
                errors.append(error)
        if errors:
            text = "cleanup not verified: " + "; ".join(errors)
            if active_error:
                print("CLEANUP_FAILED: " + text, file=sys.stderr)
            else:
                raise RuntimeError(text)
        else:
            print("PASS: temporary probe Jobs and namespace were removed and verified absent.")


if __name__ == "__main__":
    main()
