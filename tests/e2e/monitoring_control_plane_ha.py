#!/usr/bin/env python3
"""Verify authenticated API availability, telemetry and data in a disposable kubeadm HA lab."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.operations.monitoring.scrape_rules import control_plane_rules
from tests.e2e.monitoring_control_plane_live import get_json, query
from tests.e2e.monitoring_etcd_quorum_live import docker, wait_until

KIND_IMAGE = "kindest/node:v1.36.1"
PROM_IMAGE = "prom/prometheus:v3.14.0"
LB_IMAGE = "haproxy:2.8-alpine"
CONFIRM = "atlas-control-plane-ha-e2e"
ALERT = "AtlasControlPlaneTargetOrTelemetryMissing"
ROLE = "atlas-ha-e2e-metrics"


def kubectl(kubeconfig, *args, value=None, check=True, timeout=8):
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), f"--request-timeout={timeout}s", *args],
        input=json.dumps(value) if value is not None else None,
        text=True, capture_output=True, timeout=timeout + 5,
    )
    if check and result.returncode:
        raise RuntimeError("kubectl " + args[0] + " failed in the disposable HA cluster")
    return result


def node_instance(node):
    address = docker("inspect", "--format", '{{(index .NetworkSettings.Networks "kind").IPAddress}}', node)
    return address.stdout.strip() + ":6443"


def start_load_balancer(root, kubeconfig, cluster, instances, container):
    backend = "".join(f"    server cp{index} {address} check inter 2s fall 3 rise 2\n"
                      for index, address in enumerate(instances.values(), 1))
    (root / "haproxy.cfg").write_text(
        "defaults\n    mode tcp\n    timeout connect 5s\n    timeout client 30s\n    timeout server 30s\n"
        "frontend kube-apiserver\n    bind *:6443\n    default_backend kube-apiserver-backend\n"
        "backend kube-apiserver-backend\n    balance roundrobin\n    option tcp-check\n" + backend
    )
    docker("run", "-d", "--pull=never", "--name", container,
           "--label", "atlas-kubekit.e2e=" + cluster, "--network", "kind", "-p", "127.0.0.1::6443",
           "--user", f"{root.stat().st_uid}:{root.stat().st_gid}",
           "-v", str(root / "haproxy.cfg") + ":/usr/local/etc/haproxy/haproxy.cfg:ro", LB_IMAGE)
    port = docker("port", container, "6443/tcp").stdout.strip().rsplit(":", 1)[1]
    kubectl(kubeconfig, "config", "set-cluster", "kind-" + cluster,
            "--server=https://127.0.0.1:" + port)
    wait_until("Authenticated API through the test HAProxy",
               lambda: kubectl(kubeconfig, "get", "--raw", "/readyz", check=False).returncode == 0)


def start_prometheus(root, kubeconfig, cluster, instances, container):
    for kind, version, fields in (
        ("ServiceAccount", "v1", {}),
        ("ClusterRole", "rbac.authorization.k8s.io/v1",
         {"rules": [{"nonResourceURLs": ["/metrics"], "verbs": ["get"]}]}),
        ("ClusterRoleBinding", "rbac.authorization.k8s.io/v1", {
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": ROLE},
            "subjects": [{"kind": "ServiceAccount", "name": ROLE, "namespace": "default"}],
        }),
    ):
        kubectl(kubeconfig, "create", "-f", "-",
                value={"apiVersion": version, "kind": kind, "metadata": {"name": ROLE}, **fields})
    ca = kubectl(kubeconfig, "config", "view", "--raw", "--minify",
                 "-o", "jsonpath={.clusters[0].cluster.certificate-authority-data}").stdout
    (root / "ca.pem").write_bytes(base64.b64decode(ca, validate=True))
    token_file = root / "token"
    token_file.touch(mode=0o600)
    token_file.write_text(kubectl(kubeconfig, "create", "token", ROLE, "--duration=15m").stdout.strip())
    rules = control_plane_rules(
        "monitoring", "atlas-e2e", [{"job": "apiserver", "instance": endpoint} for endpoint in instances.values()],
        12, 24, "critical", "控制面指标目标不可用", "检查 API Server 与采集路径。",
    )
    (root / "rules.json").write_text(json.dumps(rules["spec"]))
    prometheus = {
        "global": {"scrape_interval": "4s", "scrape_timeout": "3s", "evaluation_interval": "2s"},
        "rule_files": ["/fixture/rules.json"],
        "scrape_configs": [{
            "job_name": "apiserver", "scheme": "https",
            "authorization": {"credentials_file": "/fixture/token"},
            "tls_config": {"ca_file": "/fixture/ca.pem"},
            "static_configs": [{"targets": list(instances.values())}],
        }],
    }
    (root / "prometheus.json").write_text(json.dumps(prometheus))
    uid = root.stat().st_uid
    gid = root.stat().st_gid
    docker("run", "-d", "--pull=never", "--name", container,
           "--label", "atlas-kubekit.e2e=" + cluster, "--network", "kind",
           "-p", "127.0.0.1::9090", "--user", f"{uid}:{gid}",
           "--tmpfs", "/prometheus:rw,nodev,nosuid,size=256m,mode=1777",
           "-v", str(root) + ":/fixture:ro", PROM_IMAGE,
           "--config.file=/fixture/prometheus.json", "--storage.tsdb.path=/prometheus")
    port = docker("port", container, "9090/tcp").stdout.strip().rsplit(":", 1)[1]
    return "http://127.0.0.1:" + port


def check_monitoring(url, expected_targets, down):
    observed = query(url, 'up{job="apiserver"}', 5)
    target_values = {sample["metric"]["instance"]: sample["value"][1] for sample in observed}
    if set(target_values) != expected_targets or any(
            value != ("0" if instance in down else "1") for instance, value in target_values.items()):
        return False
    groups = get_json(url, "/api/v1/rules?type=alert", 5)["data"]["groups"]
    rules = [rule for group in groups for rule in group["rules"] if rule["name"] == ALERT]
    if (len(rules) != len(expected_targets) or
            any(rule["health"] != "ok" or rule["labels"].get("risk_id") != "R-CP-01" for rule in rules)):
        return False
    return all(rule["state"] == ("firing" if rule["labels"]["instance"] in down else "inactive")
               for rule in rules)


def writes_through_entry(kubeconfig, name, phase):
    kubectl(kubeconfig, "get", "--raw", "/readyz")
    kubectl(kubeconfig, "patch", "configmap", name, "--type=merge",
            "-p", json.dumps({"data": {"phase": phase}}))


def verify(kubeconfig, url, cluster, nodes, instances):
    expected = set(instances.values())
    wait_until("Authenticated /metrics and all three API component rules", lambda: check_monitoring(url, expected, set()))
    for instance in expected:
        records = query(url, 'count(apiserver_request_total{instance="' + instance + '"})', 5)
        assert len(records) == 1 and float(records[0]["value"][1]) > 0, "Expected authenticated API metrics"

    sentinel = uuid.uuid4().hex
    kubectl(kubeconfig, "create", "configmap", "ha-sentinel", "--from-literal=token=" + sentinel)
    baseline = kubectl(kubeconfig, "get", "configmap", "ha-sentinel", "-o", "json").stdout
    first, second = nodes[-1], nodes[-2]
    docker("stop", "--time", "8", first, timeout=35)
    first_down = {instances[first]}
    wait_until("Single lost control-plane alert", lambda: check_monitoring(url, expected, first_down))
    writes_through_entry(kubeconfig, "ha-sentinel", "one-node-lost")
    print("Authenticated API through the load balancer accepted a write with one node down.")

    docker("stop", "--time", "8", second, timeout=35)
    double_down = {instances[first], instances[second]}
    wait_until("Two lost control-plane alerts", lambda: check_monitoring(url, expected, double_down))

    def etcd_unready():
        state = kubectl(kubeconfig, "get", "--raw", "/readyz?verbose", check=False, timeout=8)
        return state.returncode != 0 and "[-]etcd" in state.stderr + state.stdout

    wait_until("Readiness reports etcd failure after quorum is lost", etcd_unready, timeout=90)
    rejected = kubectl(kubeconfig, "create", "configmap", "quorum-loss-write",
                       "--from-literal=value=trial", check=False, timeout=6)
    assert rejected.returncode != 0, "API acknowledged a write during quorum loss"
    print("API through the load balancer did not acknowledge a write after quorum loss.")

    docker("start", second)
    wait_until("API readiness after majority recovery",
               lambda: kubectl(kubeconfig, "get", "--raw", "/readyz", check=False).returncode == 0, timeout=180)
    actual = json.loads(kubectl(kubeconfig, "get", "configmap", "ha-sentinel", "-o", "json").stdout)
    assert actual["metadata"]["uid"] == json.loads(baseline)["metadata"]["uid"]
    assert actual["data"] == {"token": sentinel, "phase": "one-node-lost"}
    assert kubectl(kubeconfig, "get", "configmap", "quorum-loss-write", "--ignore-not-found").stdout == ""
    writes_through_entry(kubeconfig, "ha-sentinel", "majority-restored")
    docker("start", first)
    wait_until("All API rules recovered", lambda: check_monitoring(url, expected, set()), timeout=180)
    updated = json.loads(kubectl(kubeconfig, "get", "configmap", "ha-sentinel", "-o", "json").stdout)
    assert updated["data"] == {"token": sentinel, "phase": "majority-restored"}
    assert all(node_instance(node) == instances[node] for node in nodes)
    print("Data and authenticated writes recovered; all three API alerts inactive in " + cluster + ".")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, type=Path)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != CONFIRM or not args.kind.is_absolute():
        parser.error(f"A verified absolute kind path and --confirm {CONFIRM} are required")
    for image in (KIND_IMAGE, PROM_IMAGE, LB_IMAGE):
        docker("image", "inspect", image)
    suffix = uuid.uuid4().hex[:10]
    cluster = "atlas-ha-" + suffix
    prometheus = "atlas-ha-prom-" + suffix
    haproxy = "atlas-ha-lb-" + suffix
    temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="atlas-control-plane-ha-", dir=temp_root) as directory:
        root = Path(directory)
        kubeconfig = root / "kubeconfig"
        configuration = root / "kind.json"
        configuration.write_text(json.dumps({
            "apiVersion": "kind.x-k8s.io/v1alpha4", "kind": "Cluster",
            "networking": {"apiServerAddress": "127.0.0.1"},
            "nodes": [{"role": "control-plane"} for _ in range(3)],
        }))
        try:
            subprocess.run([str(args.kind), "create", "cluster", "--name", cluster, "--image", KIND_IMAGE,
                            "--config", str(configuration), "--kubeconfig", str(kubeconfig), "--wait", "180s"],
                           check=True, timeout=360)
            controls = (cluster + "-control-plane", cluster + "-control-plane2", cluster + "-control-plane3")
            name = kubectl(kubeconfig, "config", "current-context").stdout.strip()
            assert name == "kind-" + cluster
            for node in controls:
                label = docker("inspect", "--format", '{{ index .Config.Labels "io.x-k8s.kind.cluster" }}', node)
                assert label.stdout.strip() == cluster, "Refusing to operate on a node outside the disposable cluster"
            instances = {node: node_instance(node) for node in controls}
            start_load_balancer(root, kubeconfig, cluster, instances, haproxy)
            url = start_prometheus(root, kubeconfig, cluster, instances, prometheus)
            verify(kubeconfig, url, cluster, controls, instances)
        finally:
            for fixture in (prometheus, haproxy):
                owner = docker("inspect", "--format", '{{ index .Config.Labels "atlas-kubekit.e2e" }}',
                               fixture, check=False)
                if owner.returncode == 0:
                    assert owner.stdout.strip() == cluster
                    docker("rm", "-f", fixture)
            subprocess.run([str(args.kind), "delete", "cluster", "--name", cluster,
                            "--kubeconfig", str(kubeconfig)], check=True, timeout=130)
            resources = docker("ps", "-aq", "--filter", "label=io.x-k8s.kind.cluster=" + cluster).stdout
            assert not resources.strip() and all(docker("inspect", name, check=False).returncode
                                                 for name in (prometheus, haproxy))
            print("Temporary HA cluster, monitor and node credentials cleaned up.")


if __name__ == "__main__":
    main()
