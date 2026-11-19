#!/usr/bin/env python3
"""Verify a real three-member etcd quorum and Prometheus quorum alert in Docker."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import socket
from urllib.request import urlopen
from urllib.parse import quote
import uuid
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.operations.monitoring.scrape_rules import etcd_leader_rules
from scripts.operations.monitoring.observer import runtime as observer_runtime

ETCD_IMAGE = "registry.k8s.io/etcd:3.6.8-0"
PROMETHEUS_IMAGE = "prom/prometheus:v3.14.0"
CONFIRMATION = "atlas-etcd-quorum-e2e"


def docker(*args, check=True, timeout=60):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("docker " + " ".join(args[:3]) + " failed: " + result.stderr.strip())
    return result


def wait_until(label, predicate, timeout=60):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                print(label + ": passed")
                return
        except (OSError, RuntimeError, ValueError, KeyError):
            pass
        time.sleep(1)
    raise RuntimeError(label + " timed out; last result=" + repr(last))


def container_exec(name, *args, check=True):
    return docker("exec", name, *args, check=check)


def etcd_health(name, endpoints):
    return container_exec(name, "etcdctl", "--endpoints=" + endpoints, "endpoint", "health", check=False)


def query_alert(prometheus_url, alert_name):
    query = 'ALERTS{alertname="' + alert_name + '"}'
    with urlopen(prometheus_url + "/api/v1/query?query=" + quote(query), timeout=4) as response:
        payload = json.loads(response.read(1024 * 1024))
    return payload["status"] == "success" and bool(payload["data"]["result"])


def owned_cleanup(suffix, network, containers):
    errors = []
    for name in containers:
        found = docker("inspect", "--format", '{{ index .Config.Labels "atlas-kubekit.e2e" }}', name, check=False)
        if found.returncode:
            if "no such" not in found.stderr.lower():
                errors.append(name + " could not be inspected")
            continue
        if found.stdout.strip() != suffix:
            errors.append(name + " ownership label changed; refusing removal")
            continue
        docker("rm", "-f", name, check=False)
        after = docker("inspect", name, check=False)
        if after.returncode == 0:
            errors.append(name + " remains after cleanup")
    found = docker("network", "inspect", "--format", '{{ index .Labels "atlas-kubekit.e2e" }}', network,
                   check=False)
    if found.returncode == 0:
        if found.stdout.strip() != suffix:
            errors.append(network + " ownership label changed; refusing removal")
        else:
            docker("network", "rm", network, check=False)
            if docker("network", "inspect", network, check=False).returncode == 0:
                errors.append(network + " remains after cleanup")
    elif "not found" not in found.stderr.lower() and "no such" not in found.stderr.lower():
        errors.append(network + " could not be inspected")
    if errors:
        raise RuntimeError("cleanup not verified: " + "; ".join(errors))
    print("Owned test containers and network removed and verified.")


def start_member(name, network, suffix):
    peer_urls = ",".join("etcd" + str(index) + "=http://etcd" + str(index) + ":2380"
                         for index in range(1, 4))
    docker("run", "-d", "--pull=never", "--name", name,
           "--label", "atlas-kubekit.e2e=" + suffix, "--network", network,
           ETCD_IMAGE, "/usr/local/bin/etcd", "--name=" + name,
           "--data-dir=/etcd-data",
           "--listen-peer-urls=http://0.0.0.0:2380",
           "--initial-advertise-peer-urls=http://" + name + ":2380",
           "--listen-client-urls=http://0.0.0.0:2379",
           "--advertise-client-urls=http://" + name + ":2379",
           "--listen-metrics-urls=http://0.0.0.0:2381",
           "--initial-cluster=" + peer_urls,
           "--initial-cluster-token=" + suffix,
           "--initial-cluster-state=new")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True, help="Type " + CONFIRMATION)
    if parser.parse_args().confirm != CONFIRMATION:
        parser.error("Explicit confirmation " + CONFIRMATION + " is required")

    for image in (ETCD_IMAGE, PROMETHEUS_IMAGE):
        if docker("image", "inspect", image, check=False).returncode:
            raise SystemExit("Required cached image is missing: " + image + "; this E2E does not pull images")

    suffix = uuid.uuid4().hex[:12]
    network = "atlas-etcd-e2e-" + suffix
    members = ["etcd1", "etcd2", "etcd3"]
    prometheus = "atlas-etcd-prometheus-" + suffix
    containers = members + [prometheus]
    temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None

    try:
        with tempfile.TemporaryDirectory(prefix="atlas-etcd-e2e-", dir=temp_root) as temp_dir:
            root = Path(temp_dir).resolve()
            targets = [{"job": "atlas-etcd-e2e", "instance": member + ":2381"} for member in members]
            generated = etcd_leader_rules("monitoring", "atlas", targets, 4, 30, "critical",
                                          "隔离 E2E etcd 多数派检查", "仅用于临时三成员 etcd 仲裁验证")
            group = generated["spec"]["groups"][0]
            config = {
                "global": {"scrape_interval": "2s", "evaluation_interval": "2s", "scrape_timeout": "1s"},
                "rule_files": ["/etc/prometheus/etcd-rules.json"],
                "scrape_configs": [{"job_name": "atlas-etcd-e2e", "static_configs": [
                    {"targets": [member + ":2381" for member in members]}]}],
            }
            (root / "prometheus.yml").write_text(
                yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            (root / "etcd-rules.json").write_text(json.dumps({"groups": [group]}, ensure_ascii=False),
                                                  encoding="utf-8")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                host_port = listener.getsockname()[1]

            docker("network", "create", "--label", "atlas-kubekit.e2e=" + suffix, network)
            for member in members:
                start_member(member, network, suffix)

            endpoint_list = ",".join("http://" + member + ":2379" for member in members)
            wait_until("Three-member etcd cluster health", lambda: etcd_health("etcd1", endpoint_list).returncode == 0)
            container_exec("etcd1", "etcdctl", "--endpoints=" + endpoint_list, "put", "atlas-e2e-key", "persisted")
            print("Initial key write with all three members: passed")

            docker("run", "-d", "--pull=never", "--name", prometheus,
                   "--label", "atlas-kubekit.e2e=" + suffix, "--network", network,
                   "-p", "127.0.0.1:" + str(host_port) + ":9090",
                   "-v", str(root / "prometheus.yml") + ":/etc/prometheus/prometheus.yml:ro",
                   "-v", str(root / "etcd-rules.json") + ":/etc/prometheus/etcd-rules.json:ro",
                   PROMETHEUS_IMAGE, "--config.file=/etc/prometheus/prometheus.yml",
                   "--web.enable-lifecycle")
            prometheus_url = "http://127.0.0.1:" + str(host_port)
            wait_until("Prometheus readiness", lambda: ready(prometheus_url))
            wait_until("Three fresh etcd metric targets", lambda: targets_up(prometheus_url, members))
            wait_until("Quorum alert inactive with three members", lambda: not query_alert(
                prometheus_url, "AtlasEtcdQuorumOrTelemetryMissing"))

            docker("stop", "--timeout", "10", "etcd3")
            survivor_endpoints = "http://etcd1:2379,http://etcd2:2379"
            wait_until("Two-member quorum remains writable", lambda: etcd_health("etcd1", survivor_endpoints).returncode == 0)
            container_exec("etcd1", "etcdctl", "--endpoints=" + survivor_endpoints,
                           "put", "atlas-e2e-key", "written-with-one-member-down")
            print("One member stopped; quorum retained and write succeeded: passed")
            wait_until("Quorum alert remains inactive with two of three members", lambda: not query_alert(
                prometheus_url, "AtlasEtcdQuorumOrTelemetryMissing"))

            docker("stop", "--timeout", "10", "etcd2")
            wait_until("Two stopped members remove etcd majority", lambda: etcd_health(
                "etcd1", "http://etcd1:2379").returncode != 0)
            wait_until("Prometheus quorum alert fires", lambda: query_alert(
                prometheus_url, "AtlasEtcdQuorumOrTelemetryMissing"), timeout=45)
            print("Two members stopped; etcd lost quorum and Prometheus alert fired: passed")

            docker("start", "etcd2")
            wait_until("Quorum recovers after one member restarts", lambda: etcd_health(
                "etcd1", survivor_endpoints).returncode == 0, timeout=90)
            value = container_exec("etcd1", "etcdctl", "--endpoints=" + survivor_endpoints,
                                   "get", "atlas-e2e-key", "--print-value-only").stdout.strip()
            if value != "written-with-one-member-down":
                raise RuntimeError("etcd key did not survive member failure/recovery")
            wait_until("Prometheus quorum alert resolves", lambda: not query_alert(
                prometheus_url, "AtlasEtcdQuorumOrTelemetryMissing"), timeout=60)
            print("Recovered quorum, data integrity and alert resolution: passed")
            exercise_external_observer(prometheus_url, prometheus, root)
    finally:
        owned_cleanup(suffix, network, containers)


def ready(url):
    with urlopen(url + "/-/ready", timeout=3) as response:
        return response.status == 200


def targets_up(url, members):
    with urlopen(url + "/api/v1/targets", timeout=3) as response:
        payload = json.loads(response.read(1024 * 1024))
    active = payload.get("data", {}).get("activeTargets", [])
    observed = {item.get("labels", {}).get("instance"): item.get("health")
                for item in active if item.get("labels", {}).get("job") == "atlas-etcd-e2e"}
    return all(observed.get(member + ":2381") == "up" for member in members)


def exercise_external_observer(prometheus_url, prometheus_container, root):
    stop = threading.Event()
    delivered = []
    state_dir = root / "observer-state"
    state_dir.mkdir(mode=0o700)
    target_url = prometheus_url + "/-/healthy"

    def deliver(name, event, reason):
        delivered.append((name, event, reason))
        return "accepted"

    def observe():
        with observer_runtime.database(state_dir) as connection:
            observer_runtime.run(connection, "prometheus-stack", target_url, 2, 2, 1, deliver,
                                 stop=stop.is_set, sleep=stop.wait)

    thread = threading.Thread(target=observe, name="isolated-prometheus-observer", daemon=True)
    thread.start()
    try:
        time.sleep(2.5)
        docker("stop", "--timeout", "5", prometheus_container)
        wait_until("External observer detects Prometheus outage", lambda: any(
            row[0] == "prometheus-stack" and row[1] == "firing" for row in delivered), timeout=20)
        docker("start", prometheus_container)
        try:
            wait_until("Prometheus readiness after restart", lambda: ready(prometheus_url), timeout=60)
        except RuntimeError:
            state = docker("inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", prometheus_container,
                           check=False).stdout.strip()
            ports = docker("port", prometheus_container, "9090/tcp", check=False).stdout.strip()
            logs = docker("logs", "--tail", "30", prometheus_container, check=False).stdout
            raise RuntimeError("Prometheus did not recover; state=" + state + ", port=" + ports + ", logs=" + logs)
        wait_until("External observer sends monitoring-stack recovery", lambda: any(
            row[0] == "prometheus-stack" and row[1] == "resolved" for row in delivered), timeout=20)
        if not any(row[0] == "prometheus-stack" and row[1] == "firing" and
                   row[2] == "unreachable-from-observer" for row in delivered):
            raise RuntimeError("observer outage message did not identify a failed HTTP probe")
        print("Independent observer detected Prometheus outage and recovery; isolated delivery callback accepted both: passed")
    finally:
        stop.set()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("independent observer worker did not stop")


if __name__ == "__main__":
    main()
