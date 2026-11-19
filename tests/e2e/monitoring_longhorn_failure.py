#!/usr/bin/env python3
"""Exercise real Longhorn replica degradation, alerting, recovery, and data integrity.

This destructive E2E is only for a disposable multi-node lab. It creates a
unique test namespace, StorageClass, PVC, Pod, and PrometheusRule, then disables
scheduling on one selected Longhorn node and deletes only this test volume's
replica from that node. The node's original scheduling setting is restored in
the cleanup path.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.monitoring.longhorn_rules import volume_robustness_rules

CONFIRMATION = "atlas-longhorn-replica-failure-e2e"
ALERT_NAME = "AtlasLonghornVolumeRobustnessOrTelemetryMissing"
DATA = (b"Atlas KubeKit Longhorn recovery E2E sentinel\n" * 1024)


class LabError(RuntimeError):
    pass


def run(arguments, *, data=None, timeout=90, check=True):
    result = subprocess.run(arguments, input=(json.dumps(data).encode() if data is not None else None),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    stdout = result.stdout.decode(errors="replace")
    stderr = result.stderr.decode(errors="replace")
    if check and result.returncode:
        raise LabError("command failed: " + " ".join(arguments) + "\n" + stderr[-3000:] + stdout[-1000:])
    return stdout


def safe_prometheus_url(value):
    parts = urllib.parse.urlsplit(value)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or
            parts.query or parts.fragment or (parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "::1", "localhost"))):
        raise ValueError("Prometheus URL must use HTTPS or local loopback HTTP")
    return value.rstrip("/")


def query(prometheus, expression):
    url = prometheus + "/api/v1/query?" + urllib.parse.urlencode({"query": expression})
    with urllib.request.urlopen(url, timeout=5) as response:
        body = json.loads(response.read())
    if body.get("status") != "success":
        raise LabError("Prometheus query failed")
    return body.get("data", {}).get("result", [])


def wait_for(description, predicate, seconds, interval=3):
    deadline = time.monotonic() + seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError, LabError) as error:
            last_error = error
        time.sleep(interval)
    suffix = (": " + str(last_error)) if last_error else ""
    raise LabError("timed out waiting for " + description + suffix)


def metric_state(prometheus, namespace, claim):
    labels = '{pvc_namespace="' + namespace + '",pvc="' + claim + '"'
    result = {}
    for state in ("healthy", "degraded", "faulted", "unknown"):
        samples = query(prometheus, "longhorn_volume_robustness" + labels + ',state="' + state + '"}')
        # Each Longhorn Manager exposes the same cluster-wide series. Accept
        # duplicate scrape targets only when they agree on the state value.
        value = collapse_samples(samples)
        if value is None:
            return None
        result[state] = value
    return result


def collapse_samples(samples):
    values = {float(sample["value"][1]) for sample in samples}
    return values.pop() if len(values) == 1 else None


def expected_state(actual, name):
    if not actual:
        return False
    return all(value == (1.0 if state == name else 0.0) for state, value in actual.items())


def active_alert(prometheus, namespace, claim):
    alerts = query(prometheus, 'ALERTS{alertname="' + ALERT_NAME + '",alertstate="firing",namespace="' +
                   namespace + '",persistentvolumeclaim="' + claim + '"}')
    return bool(alerts)


def robustness_rule_loaded(prometheus):
    with urllib.request.urlopen(prometheus + "/api/v1/rules?type=alert", timeout=10) as response:
        body = json.loads(response.read())
    if body.get("status") != "success":
        return False
    return any(item.get("name") == ALERT_NAME
               for group in body.get("data", {}).get("groups", [])
               for item in group.get("rules", []))


def kubectl_json(kubectl, *arguments):
    return json.loads(run(kubectl + list(arguments) + ["-o", "json"]))


def make_resources(namespace, storage_class, pvc, pod, survivor, image):
    return [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": namespace + "-payload", "namespace": namespace},
         "binaryData": {"sentinel.bin": base64.b64encode(DATA).decode("ascii")}},
        {"apiVersion": "storage.k8s.io/v1", "kind": "StorageClass", "metadata": {"name": storage_class},
         "provisioner": "driver.longhorn.io", "allowVolumeExpansion": True,
         "reclaimPolicy": "Delete", "volumeBindingMode": "Immediate",
         "parameters": {"numberOfReplicas": "2", "dataLocality": "disabled"}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": pvc, "namespace": namespace},
         "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": storage_class,
                  "resources": {"requests": {"storage": "1Gi"}}}},
        {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": pod, "namespace": namespace},
         "spec": {"nodeSelector": {"kubernetes.io/hostname": survivor},
          "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": pvc}},
                      {"name": "seed", "configMap": {"name": namespace + "-payload", "defaultMode": 292}}],
          "containers": [{"name": "writer", "image": image, "command": ["sh", "-c", "sleep 86400"],
            "volumeMounts": [{"name": "data", "mountPath": "/data"},
                             {"name": "seed", "mountPath": "/seed", "readOnly": True}]}]}}
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, help="Absolute path to a private disposable-lab kubeconfig")
    parser.add_argument("--expected-context", required=True, help="Exact disposable-lab kubeconfig context")
    parser.add_argument("--failed-replica-node", required=True, help="Longhorn node hosting the replica to remove")
    parser.add_argument("--survivor-node", required=True, help="Different node that hosts the test Pod and retains a replica")
    parser.add_argument("--prometheus-url", required=True, help="Prometheus URL; local HTTP or certificate-verified HTTPS")
    parser.add_argument("--prometheus-release", required=True, help="Release label selected by the Prometheus resource")
    parser.add_argument("--monitoring-namespace", default="monitoring")
    parser.add_argument("--hold-seconds", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--image", default=(
        "m.daocloud.io/docker.io/library/busybox@"
        "sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"))
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    kubeconfig = Path(args.kubeconfig)
    if (args.confirm != CONFIRMATION or not kubeconfig.is_absolute() or not kubeconfig.is_file() or
            kubeconfig.stat().st_mode & 0o077 or args.expected_context.strip() == "" or
            not re.search(r"(?:e2e|lab|test)", args.expected_context, re.IGNORECASE) or
            args.failed_replica_node == args.survivor_node or
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", args.failed_replica_node) or
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", args.survivor_node) or
            not 1 <= args.hold_seconds <= 3600 or not args.hold_seconds * 3 <= args.timeout_seconds <= 7200):
        parser.error("Private kubeconfig, exact lab confirmation/context, distinct node names, and bounded timings are required")
    try:
        prometheus = safe_prometheus_url(args.prometheus_url)
    except ValueError as error:
        parser.error(str(error))

    kubectl_base = ["kubectl", "--kubeconfig", str(kubeconfig)]
    contexts = set(run(kubectl_base + ["config", "get-contexts", "-o", "name"]).splitlines())
    if args.expected_context not in contexts:
        parser.error("--expected-context is not present in the supplied kubeconfig")
    kubectl = kubectl_base + ["--context", args.expected_context]

    suffix = uuid.uuid4().hex[:10]
    namespace = "atlas-e2e-longhorn-" + suffix
    storage_class = namespace
    pvc, pod = "sentinel", "writer"
    rule_name = "atlas-longhorn-e2e-" + suffix
    longhorn_namespace = "longhorn-system"
    namespace_created = class_created = rule_created = False
    original_allow_scheduling = None
    node_resource = args.failed_replica_node
    test_replica = None
    test_volume = None
    data_hash = hashlib.sha256(DATA).hexdigest()
    print("Disposable Longhorn E2E context:", args.expected_context)
    print("Test nodes:", args.failed_replica_node, "(replica removal),", args.survivor_node, "(surviving replica and writer)")

    try:
        namespaces = kubectl_json(kubectl, "get", "namespaces")
        existing = {item["metadata"]["name"] for item in namespaces["items"]}
        if namespace in existing:
            raise LabError("generated E2E namespace unexpectedly exists")
        node_list = kubectl_json(kubectl, "-n", longhorn_namespace, "get", "nodes.longhorn.io")
        nodes = {item["metadata"]["name"]: item for item in node_list["items"]}
        if args.failed_replica_node not in nodes or args.survivor_node not in nodes:
            raise LabError("both selected nodes must exist as Longhorn nodes")
        if not nodes[args.failed_replica_node].get("spec", {}).get("allowScheduling", True):
            raise LabError("selected replica node is already unschedulable in Longhorn")

        # Prove the rule selector can load the exact test rule before injecting a fault.
        rule = volume_robustness_rules(args.monitoring_namespace, args.prometheus_release,
            [{"namespace": namespace, "claim": pvc}], args.hold_seconds, 120, "warning",
            "Longhorn E2E volume degraded", "Disposable Longhorn replica recovery test.")
        rule["metadata"]["name"] = rule_name
        rule["spec"]["groups"][0]["name"] = rule_name

        run(kubectl + ["create", "namespace", namespace])
        namespace_created = True
        run(kubectl + ["label", "namespace", namespace, "atlas-kubekit.io/e2e=longhorn-replica-failure"])
        run(kubectl + ["apply", "-f", "-"], data=rule)
        rule_created = True
        resources = make_resources(namespace, storage_class, pvc, pod, args.survivor_node, args.image)
        run(kubectl + ["apply", "-f", "-"], data=resources[0])
        run(kubectl + ["apply", "-f", "-"], data=resources[1])
        class_created = True
        run(kubectl + ["apply", "-f", "-"], data={"apiVersion": "v1", "kind": "List", "items": resources[2:]})
        run(kubectl + ["-n", namespace, "wait", "--for=jsonpath={.status.phase}=Bound", "pvc/" + pvc,
                       "--timeout=" + str(args.timeout_seconds) + "s"], timeout=args.timeout_seconds + 15)
        run(kubectl + ["-n", namespace, "wait", "--for=condition=Ready", "pod/" + pod,
                       "--timeout=" + str(args.timeout_seconds) + "s"], timeout=args.timeout_seconds + 15)
        test_volume = kubectl_json(kubectl, "-n", namespace, "get", "pvc", pvc)["spec"]["volumeName"]

        replicas = kubectl_json(kubectl, "-n", longhorn_namespace, "get", "replicas.longhorn.io")
        matches = [item for item in replicas["items"] if item.get("spec", {}).get("volumeName") == test_volume]
        selected = [item for item in matches if item.get("spec", {}).get("nodeID") == args.failed_replica_node]
        survivor = [item for item in matches if item.get("spec", {}).get("nodeID") == args.survivor_node]
        if len(matches) != 2 or len(selected) != 1 or len(survivor) != 1:
            raise LabError("test volume must have exactly two replicas placed one per selected Longhorn node")
        test_replica = selected[0]["metadata"]["name"]

        wait_for("healthy Longhorn volume metrics",
                 lambda: expected_state(metric_state(prometheus, namespace, pvc), "healthy"),
                 args.timeout_seconds)
        # Rule publication is asynchronous through Prometheus Operator. Wait
        # for the actual Prometheus API state before claiming it is loaded.
        wait_for("Prometheus to load the Longhorn robustness rule",
                 lambda: robustness_rule_loaded(prometheus), min(args.timeout_seconds, 180))
        if active_alert(prometheus, namespace, pvc):
            raise LabError("test volume raised a robustness alert before fault injection")
        print("Initial volume state: healthy; robustness rule loaded; sentinel SHA256:", data_hash)

        run(kubectl + ["-n", namespace, "exec", pod, "--", "cp", "/seed/sentinel.bin", "/data/sentinel.bin"])
        written_result = run(kubectl + ["-n", namespace, "exec", pod, "--", "sha256sum", "/data/sentinel.bin"])
        written_hash = written_result.split()[0]
        if written_hash != data_hash:
            size = run(kubectl + ["-n", namespace, "exec", pod, "--", "wc", "-c", "/data/sentinel.bin"]).strip()
            raise LabError("sentinel payload checksum did not match before fault injection: expected " +
                           data_hash + ", got " + written_hash + ", bytes " + size)
        original_allow_scheduling = nodes[args.failed_replica_node].get("spec", {}).get("allowScheduling", True)
        run(kubectl + ["-n", longhorn_namespace, "patch", "nodes.longhorn.io", node_resource,
                       "--type=merge", "-p", '{"spec":{"allowScheduling":false}}'])
        wait_for("Longhorn node scheduling isolation",
                 lambda: kubectl_json(kubectl, "-n", longhorn_namespace, "get", "nodes.longhorn.io",
                                      node_resource).get("spec", {}).get("allowScheduling") is False,
                 60, interval=2)
        run(kubectl + ["-n", longhorn_namespace, "delete", "replicas.longhorn.io", test_replica,
                       "--wait=true", "--timeout=60s"])

        wait_for("Longhorn degraded metric and firing alert", lambda:
            expected_state(metric_state(prometheus, namespace, pvc), "degraded") and
            active_alert(prometheus, namespace, pvc), args.timeout_seconds)
        print("Fault state: degraded metric observed and alert firing.")

        after_fault_hash = run(kubectl + ["-n", namespace, "exec", pod, "--", "sha256sum", "/data/sentinel.bin"]).split()[0]
        if after_fault_hash != data_hash:
            raise LabError("sentinel data checksum changed while one replica was unavailable")
        run(kubectl + ["-n", longhorn_namespace, "patch", "nodes.longhorn.io", node_resource,
                       "--type=merge", "-p", json.dumps({"spec": {"allowScheduling": original_allow_scheduling}})])
        original_allow_scheduling = None

        wait_for("healthy two-replica recovery and resolved alert", lambda:
            expected_state(metric_state(prometheus, namespace, pvc), "healthy") and
            not active_alert(prometheus, namespace, pvc), args.timeout_seconds)
        replicas = kubectl_json(kubectl, "-n", longhorn_namespace, "get", "replicas.longhorn.io")
        recovered = [item for item in replicas["items"] if item.get("spec", {}).get("volumeName") == test_volume
                     and item.get("status", {}).get("currentState") == "running"]
        if len(recovered) != 2:
            raise LabError("volume did not return to two running replicas")
        check = run(kubectl + ["-n", namespace, "exec", pod, "--", "sha256sum", "/data/sentinel.bin"]).split()[0]
        if check != data_hash:
            raise LabError("sentinel data checksum changed after replica recovery")
        print("Recovery state: two replicas running; alert resolved; sentinel SHA256 unchanged:", data_hash)
    finally:
        cleanup_errors = []
        def cleanup(command, timeout=90):
            try:
                run(command, timeout=timeout, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                cleanup_errors.append("command cleanup failed: " + str(error))

        if original_allow_scheduling is not None:
            cleanup(kubectl + ["-n", longhorn_namespace, "patch", "nodes.longhorn.io", node_resource,
                               "--type=merge", "-p", json.dumps({"spec": {"allowScheduling": original_allow_scheduling}})])
            try:
                wait_for("restoration of original Longhorn node scheduling setting",
                         lambda: kubectl_json(kubectl, "-n", longhorn_namespace,
                                              "get", "nodes.longhorn.io", node_resource)
                         .get("spec", {}).get("allowScheduling") is original_allow_scheduling,
                         60, interval=2)
            except (LabError, OSError, subprocess.TimeoutExpired) as error:
                cleanup_errors.append("Longhorn scheduling state: " + str(error))
        if namespace_created:
            cleanup(kubectl + ["delete", "namespace", namespace, "--wait=true", "--timeout=180s"], timeout=200)
        if class_created:
            cleanup(kubectl + ["delete", "storageclass", storage_class, "--wait=true", "--timeout=90s"], timeout=100)
        if rule_created:
            cleanup(kubectl + ["-n", args.monitoring_namespace, "delete", "prometheusrule", rule_name,
                               "--wait=true", "--timeout=60s"], timeout=70)
        if namespace_created:
            try:
                remaining = run(kubectl + ["get", "namespace", namespace, "-o", "name"], check=False)
                if remaining.strip():
                    cleanup_errors.append("test namespace remains: " + namespace)
            except (OSError, subprocess.TimeoutExpired) as error:
                cleanup_errors.append("test namespace cleanup check failed: " + str(error))
        if test_volume:
            try:
                wait_for("deletion of the disposable Longhorn volume",
                         lambda: not run(kubectl + ["-n", longhorn_namespace, "get", "volumes.longhorn.io",
                                                     test_volume, "-o", "name"], check=False).strip(),
                         180, interval=3)
            except (LabError, OSError, subprocess.TimeoutExpired) as error:
                cleanup_errors.append("test Longhorn volume cleanup: " + str(error))
        if class_created:
            try:
                remaining = run(kubectl + ["get", "storageclass", storage_class, "-o", "name"], check=False)
                if remaining.strip():
                    cleanup_errors.append("test StorageClass remains: " + storage_class)
            except (OSError, subprocess.TimeoutExpired) as error:
                cleanup_errors.append("test StorageClass cleanup check failed: " + str(error))
        if rule_created:
            try:
                remaining = run(kubectl + ["-n", args.monitoring_namespace, "get", "prometheusrule", rule_name,
                                           "-o", "name"], check=False)
                if remaining.strip():
                    cleanup_errors.append("test PrometheusRule remains: " + rule_name)
            except (OSError, subprocess.TimeoutExpired) as error:
                cleanup_errors.append("test PrometheusRule cleanup check failed: " + str(error))
        if cleanup_errors:
            raise LabError("cleanup verification failed: " + "; ".join(cleanup_errors))
        print("Disposable Longhorn resources and node scheduling state cleanup verified.")


if __name__ == "__main__":
    main()
