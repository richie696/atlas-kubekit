#!/usr/bin/env python3
"""Inject Job and CronJob timeout alerts in a disposable single-node Kind cluster."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from tests.e2e import monitoring_workload_timeouts_live as workload_timeouts


MANIFEST = "/etc/kubernetes/manifests/kube-controller-manager.yaml"
WATCHDOG_SECONDS = 240


def _command(arguments, *, timeout=60, check=True):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("command failed: " + " ".join(arguments) + "\n" + result.stderr[-2000:])
    return result


def _restore_manifest(container, backup):
    exists = _command(["docker", "exec", container, "test", "-f", backup], check=False)
    if exists.returncode == 0:
        _command(["docker", "exec", container, "mv", "--", backup, MANIFEST], timeout=30)


def _watchdog(backup, manifest, cancel, timeout, kubeconfig, namespace, rule_name, container):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel.exists():
            return 0
        time.sleep(1)
    _restore_manifest(container, backup)
    client = ["kubectl", "--kubeconfig", kubeconfig]
    subprocess.run(client + ["delete", "namespace", namespace, "--wait=false"],
                   capture_output=True, text=True, timeout=30, check=False)
    subprocess.run(client + ["-n", "monitoring", "delete", "prometheusrule", rule_name],
                   capture_output=True, text=True, timeout=30, check=False)
    return 0


def _verify_control_plane(container, cluster_name, client):
    inspected = json.loads(_command(["docker", "inspect", container]).stdout)[0]
    labels = inspected.get("Config", {}).get("Labels", {}) or {}
    if labels.get("io.x-k8s.kind.cluster") != cluster_name or labels.get("io.x-k8s.kind.role") != "control-plane":
        raise RuntimeError("refusing to stop a container that is not this test's Kind control plane")
    nodes = json.loads(_command(client + ["get", "nodes", "-o", "json"]).stdout).get("items", [])
    if len(nodes) != 1 or nodes[0].get("metadata", {}).get("name") != container:
        raise RuntimeError("workload timeout fault injection requires the isolated single-node Kind cluster")
    return inspected


def _wait_controller(client, present, timeout=90):
    workload_timeouts.wait_controller_manager(client, present, timeout)


def _cleanup(client, prometheus_url, namespace, rule_name):
    workload_timeouts.wait_clean(client, prometheus_url, namespace, rule_name)


def _kind_watchdog_entry(arguments):
    _, _, container, backup, manifest, cancel, timeout, kubeconfig, namespace, rule_name = arguments
    if manifest != MANIFEST:
        raise RuntimeError("unexpected kube-controller-manager manifest path")
    return _watchdog(backup, manifest, Path(cancel), int(timeout), kubeconfig,
                     namespace, rule_name, container)


def run(client, kubeconfig, cluster_name, prometheus_url, temp_directory):
    """Verify real firing/recovery using only resources owned by a temporary Kind cluster."""
    container = cluster_name + "-control-plane"
    _verify_control_plane(container, cluster_name, client)
    temp_directory = Path(temp_directory)
    namespace = "atlas-workload-timeout-" + uuid.uuid4().hex[:8]
    rule_name = ""
    cancel = temp_directory / ("controller-manager-watchdog-" + uuid.uuid4().hex)
    backup = "/etc/kubernetes/.atlas-e2e-" + uuid.uuid4().hex + "-kube-controller-manager.bak"
    manifest_moved = False
    watchdog = None
    error = None
    source = json.loads(_command(client + ["-n", "monitoring", "get", "prometheusrule",
                                              "atlas-workload-health", "-o", "json"]).stdout)
    test_rule = workload_timeouts.shortened_rules(source)
    rule_name = test_rule["metadata"]["name"]
    try:
        applied = subprocess.run(client + ["-n", "monitoring", "apply", "-f", "-"],
                                 input=json.dumps(test_rule), capture_output=True, text=True, timeout=60)
        if applied.returncode:
            raise RuntimeError("could not install temporary workload timeout rule: " + applied.stderr[-2000:])
        _command(client + ["create", "namespace", namespace])
        job = {"apiVersion": "batch/v1", "kind": "Job",
               "metadata": {"name": "job-probe", "namespace": namespace},
               "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 3600,
                        "template": {"spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                                                "containers": [{"name": "worker", "image": "python:3.13-slim",
                                                                "command": ["sleep", "3600"],
                                                                "imagePullPolicy": "IfNotPresent"}]}}}}
        applied = subprocess.run(client + ["-n", namespace, "apply", "-f", "-"],
                                 input=json.dumps(job), capture_output=True, text=True, timeout=60)
        if applied.returncode:
            raise RuntimeError("could not create the temporary Job: " + applied.stderr[-2000:])
        workload_timeouts.wait_alert(prometheus_url, "AtlasJobRunningTooLong", namespace,
                                      "job_name", "job-probe")

        cronjob = {"apiVersion": "batch/v1", "kind": "CronJob",
                   "metadata": {"name": "cronjob-probe", "namespace": namespace},
                   "spec": {"schedule": "0 * * * *", "startingDeadlineSeconds": 1,
                            "suspend": False, "concurrencyPolicy": "Forbid",
                            "jobTemplate": {"spec": {"backoffLimit": 0,
                                "template": {"spec": {"restartPolicy": "Never",
                                    "automountServiceAccountToken": False,
                                    "containers": [{"name": "worker", "image": "python:3.13-slim",
                                                    "command": ["sleep", "3600"],
                                                    "imagePullPolicy": "IfNotPresent"}]}}}}}}
        _command(["docker", "exec", container, "test", "-f", MANIFEST], timeout=30)
        _command(["docker", "exec", container, "mv", "--", MANIFEST, backup], timeout=30)
        manifest_moved = True
        watchdog = subprocess.Popen(
            [sys.executable, "-m", "tests.e2e.monitoring_workload_timeouts_kind",
             "--restore-watchdog", container, backup, MANIFEST, str(cancel),
             str(WATCHDOG_SECONDS), str(kubeconfig), namespace, rule_name],
            cwd=Path(__file__).resolve().parents[2], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        _wait_controller(client, present=False, timeout=45)
        applied = subprocess.run(client + ["-n", namespace, "apply", "-f", "-"],
                                 input=json.dumps(cronjob), capture_output=True, text=True, timeout=60)
        if applied.returncode:
            raise RuntimeError("could not create the temporary CronJob: " + applied.stderr[-2000:])
        old_schedule = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200))
        _command(client + ["-n", namespace, "patch", "cronjob", "cronjob-probe", "--subresource=status",
                           "--type=merge", "-p", json.dumps({"status": {"lastScheduleTime": old_schedule}})])
        workload_timeouts.wait_alert(prometheus_url, "AtlasCronJobScheduleOverdue", namespace,
                                      "cronjob", "cronjob-probe")
        print("Real Job timeout and missed CronJob alerts fired with Chinese annotations in disposable Kind.")
    except BaseException as caught:
        error = caught
    finally:
        if manifest_moved:
            try:
                _restore_manifest(container, backup)
                manifest_moved = False
                _wait_controller(client, present=True, timeout=120)
                nodes = json.loads(_command(client + ["get", "nodes", "-o", "json"]).stdout).get("items", [])
                if len(nodes) != 1 or not any(condition.get("type") == "Ready" and condition.get("status") == "True"
                                              for condition in nodes[0].get("status", {}).get("conditions", [])):
                    raise RuntimeError("Kind control-plane node did not return Ready")
            except BaseException as restore_error:
                if error is None:
                    error = restore_error
                else:
                    print("CONTROL_PLANE_RESTORE_FAILED: " + str(restore_error), file=sys.stderr)
        if watchdog is not None:
            cancel.touch()
            try:
                watchdog.wait(timeout=5)
            except subprocess.TimeoutExpired:
                watchdog.terminate()
                watchdog.wait(timeout=5)
        try:
            _cleanup(client, prometheus_url, namespace, rule_name)
        except BaseException as cleanup_error:
            if error is None:
                error = cleanup_error
            else:
                print("WORKLOAD_TIMEOUT_CLEANUP_FAILED: " + str(cleanup_error), file=sys.stderr)
        cancel.unlink(missing_ok=True)
        if manifest_moved:
            raise RuntimeError("Kind controller-manager manifest remains moved; watchdog is responsible for restoration")
    if error is not None:
        raise error


def main():
    return _kind_watchdog_entry(sys.argv)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--restore-watchdog":
        raise SystemExit(main())
    raise SystemExit("This module is invoked by monitoring_operator.py only.")
