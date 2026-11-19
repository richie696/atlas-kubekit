#!/usr/bin/env python3
"""Verify the managed Longhorn NetworkPolicy on an isolated Cilium cluster."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.monitoring.longhorn_collection import manifests

BUSYBOX_IMAGE = "quay.io/prometheus/busybox@sha256:7379672d53c28113469275f2eb48adffa19954f39f8ef20c1e2992278f658e6c"


def command(args, timeout=60, check=True, input_text=None):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("command failed: " + " ".join(args) + "\n" + result.stderr[-3000:])
    return result


def kubectl_prefix(kubeconfig, context):
    prefix = ["kubectl", "--kubeconfig", str(kubeconfig)]
    return prefix + (["--context", context] if context else [])


def apply(client, document):
    command(client + ["apply", "-f", "-"], input_text=json.dumps(document))


def namespace(name, labels=None):
    result = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}
    if labels:
        result["metadata"]["labels"] = labels
    return result


def policy_baseline(target_namespace):
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": "existing-manager-ingress-isolation", "namespace": target_namespace},
            "spec": {"podSelector": {"matchLabels": {"app": "longhorn-manager"}},
                     "policyTypes": ["Ingress"], "ingress": []}}


def server(target_namespace, target_node):
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": "longhorn-metrics-e2e", "namespace": target_namespace,
                         "labels": {"app": "longhorn-manager"}},
            "spec": {"nodeSelector": {"kubernetes.io/hostname": target_node},
                     "containers": [{"name": "metrics", "image": BUSYBOX_IMAGE,
                                     "imagePullPolicy": "IfNotPresent",
                                     "command": ["sh", "-c", "mkdir -p /www; echo 'atlas_longhorn_network_policy_test 1' > /www/metrics; httpd -f -p 9500 -h /www"],
                                     "ports": [{"name": "manager", "containerPort": 9500}]}]}}


def service(target_namespace):
    return {"apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "longhorn-backend", "namespace": target_namespace,
                         "labels": {"app": "longhorn-manager"}},
            "spec": {"selector": {"app": "longhorn-manager"},
                     "ports": [{"name": "manager", "port": 9500, "targetPort": 9500}]}}


def probe_job(name, source_namespace, source_node, image, labels, command_text, tolerations):
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": name, "namespace": source_namespace},
            "spec": {"backoffLimit": 0,
                     "template": {"metadata": {"labels": labels},
                                  "spec": {"restartPolicy": "Never",
                                           "nodeSelector": {"kubernetes.io/hostname": source_node},
                                           "tolerations": tolerations,
                                           "containers": [{"name": "probe", "image": image,
                                                           "imagePullPolicy": "IfNotPresent",
                                                           "command": ["/bin/bash", "-c", command_text]}]}}}}


def verify_cilium(client, nodes):
    daemonset = json.loads(command(client + ["-n", "kube-system", "get", "daemonset", "cilium", "-o", "json"]).stdout)
    status = daemonset.get("status", {})
    expected = len(nodes)
    if status.get("desiredNumberScheduled") != expected or status.get("numberReady") != expected:
        raise RuntimeError("Cilium must be Ready on every node in the isolated test cluster")
    containers = daemonset.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    images = [item.get("image", "") for item in containers if item.get("name") == "cilium-agent"]
    if len(images) != 1 or not images[0].startswith("quay.io/cilium/cilium:v"):
        raise RuntimeError("could not identify the installed Cilium agent image")
    return images[0]


def delete_namespace(client, name):
    """Delete an owned test namespace and verify the API no longer exposes it."""
    deletion = command(client + ["delete", "namespace", name, "--wait=true", "--timeout=90s"],
                       check=False, timeout=100)
    deadline = time.monotonic() + 90
    while True:
        observed = command(client + ["get", "namespace", name, "--ignore-not-found", "-o", "name"],
                           check=False, timeout=15)
        if observed.returncode != 0:
            return "could not verify namespace removal for " + name + ": " + observed.stderr[-1000:]
        if not observed.stdout.strip():
            return None
        if time.monotonic() >= deadline:
            detail = deletion.stderr[-1000:] or deletion.stdout[-1000:]
            return "namespace remains after cleanup: " + name + ("; " + detail if detail else "")
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != "atlas-netpol-e2e" or not args.kubeconfig.is_absolute() or not args.kubeconfig.is_file():
        parser.error("Use an absolute kubeconfig and explicit confirmation atlas-netpol-e2e")

    client = kubectl_prefix(args.kubeconfig, args.context)
    current = command(client + ["config", "view", "--minify", "-o", "json"]).stdout
    context = json.loads(current).get("contexts", [{}])[0].get("name", "")
    if not context.startswith("kind-atlas-netpol-e2e-"):
        parser.error("context must identify a disposable kind-atlas-netpol-e2e-* cluster")
    node_list = json.loads(command(client + ["get", "nodes", "-o", "json"]).stdout).get("items", [])
    if len(node_list) < 2:
        parser.error("the isolated Cilium test cluster must have at least two nodes")
    node_names = [item.get("metadata", {}).get("name", "") for item in node_list]
    image = verify_cilium(client, node_names)
    worker_nodes = [item for item in node_list
                    if "node-role.kubernetes.io/control-plane" not in item.get("metadata", {}).get("labels", {})]
    target_node = (worker_nodes or node_list)[-1]["metadata"]["name"]
    source_node = next(name for name in node_names if name != target_node)
    tolerations = [{"operator": "Exists", "effect": "NoSchedule"}]

    suffix = uuid.uuid4().hex[:10]
    target_ns = "atlas-lh-netpol-" + suffix
    source_ns = "atlas-mon-netpol-" + suffix
    prometheus_name = "atlas-prometheus"
    _, managed_policy = manifests(source_ns, "stack", prometheus_name, target_ns)
    target_created = source_created = False
    active_error = False
    try:
        command(client + ["create", "namespace", target_ns])
        target_created = True
        command(client + ["create", "namespace", source_ns])
        source_created = True
        apply(client, policy_baseline(target_ns))
        apply(client, server(target_ns, target_node))
        apply(client, service(target_ns))
        command(client + ["-n", target_ns, "wait", "--for=condition=Ready", "pod/longhorn-metrics-e2e", "--timeout=120s"])
        apply(client, managed_policy)
        service_ip = command(client + ["-n", target_ns, "get", "service", "longhorn-backend",
                                       "-o", "jsonpath={.spec.clusterIP}"]).stdout.strip()
        if not re.fullmatch(r"[0-9.]+", service_ip):
            raise RuntimeError("test service has no usable ClusterIP")

        request = ("for attempt in $(seq 1 10); do "
                   "result=$(timeout 5 /bin/bash -c 'exec 3<>/dev/tcp/" + service_ip + "/9500; "
                   "printf \"GET /metrics HTTP/1.0\\r\\n\\r\\n\" >&3; cat <&3' 2>/dev/null) "
                   "&& { printf '%s\\n' \"$result\"; exit 0; }; sleep 1; done; exit 1")
        reject = ("if timeout 8 /bin/bash -c 'exec 3<>/dev/tcp/" + service_ip + "/9500' 2>/dev/null; "
                  "then echo UNEXPECTED_ALLOWED; exit 1; else echo BLOCKED; fi")
        apply(client, probe_job("prometheus-authorized", source_ns, source_node, image,
                                {"prometheus": prometheus_name}, request, tolerations))
        apply(client, probe_job("prometheus-unauthorized", source_ns, source_node, image,
                                {"app": "unrelated-monitor"}, reject, tolerations))
        command(client + ["-n", source_ns, "wait", "--for=condition=complete", "job/prometheus-authorized", "--timeout=90s"])
        command(client + ["-n", source_ns, "wait", "--for=condition=complete", "job/prometheus-unauthorized", "--timeout=90s"])
        allowed = command(client + ["-n", source_ns, "logs", "job/prometheus-authorized"]).stdout
        blocked = command(client + ["-n", source_ns, "logs", "job/prometheus-unauthorized"]).stdout
        if "HTTP/1.1 200 OK" not in allowed or "atlas_longhorn_network_policy_test 1" not in allowed:
            raise RuntimeError("selected Prometheus source did not scrape the cross-node target")
        if "BLOCKED" not in blocked or "UNEXPECTED_ALLOWED" in blocked:
            raise RuntimeError("NetworkPolicy did not block the unrelated source")
        print("Cilium enforced the managed Longhorn ingress policy across nodes: authorized scrape succeeded; unrelated source was blocked.")
    except BaseException:
        active_error = True
        raise
    finally:
        cleanup_errors = []
        for name, created in ((source_ns, source_created), (target_ns, target_created)):
            if created:
                error = delete_namespace(client, name)
                if error:
                    cleanup_errors.append(error)
        if cleanup_errors:
            message = "Temporary namespace cleanup was not verified: " + "; ".join(cleanup_errors)
            if active_error:
                print("CLEANUP_FAILED: " + message, file=sys.stderr)
            else:
                raise RuntimeError(message)
        else:
            print("Temporary monitoring and Longhorn test namespaces were deleted and verified absent.")


if __name__ == "__main__":
    main()
