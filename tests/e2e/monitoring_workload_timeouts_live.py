#!/usr/bin/env python3
"""Exercise real Job timeout and missed CronJob alerts in the isolated 28-30 lab.

The CronJob case briefly stops the single kube-controller-manager static pod.
Run only on cp1 in the fixed lab, as root, with explicit confirmation.
"""
import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.request import urlopen


EXPECTED_NODES = {"cp1": "10.20.1.28", "w1": "10.20.1.29", "w2": "10.20.1.30"}
MONITORING = "monitoring"
PROMETHEUS = "kube-prometheus-stack-prometheus"
RELEASE = "kube-prometheus-stack"
CONFIRMATION = "atlas-monitor-e2e-timeout"
PAUSE_IMAGE = "registry.k8s.io/pause:3.10"


def command(args, *, timeout=60, check=True, input_text=None):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("command failed: " + " ".join(args) + "\n" + result.stderr[-2000:])
    return result


def kubectl(client, *args, **kwargs):
    return command(client + list(args), **kwargs)


def resource(client, *args):
    return json.loads(kubectl(client, *args, "-o", "json").stdout)


def watchdog_main(backup, manifest, cancel, timeout_seconds, kubeconfig, namespace, rule_name):
    """Restore the static-pod manifest if the driver or SSH session disappears."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if cancel.exists():
            return 0
        time.sleep(1)
    if backup.exists():
        shutil.move(str(backup), str(manifest))
    client = ["kubectl", "--kubeconfig", kubeconfig]
    subprocess.run(client + ["delete", "namespace", namespace, "--wait=false"],
                   check=False, capture_output=True, text=True, timeout=30)
    subprocess.run(client + ["-n", MONITORING, "delete", "prometheusrule", rule_name],
                   check=False, capture_output=True, text=True, timeout=30)
    return 0


def verify_lab(client, kubeconfig):
    if os.geteuid() != 0:
        raise RuntimeError("run as root on cp1 so the watchdog can restore the static-pod manifest")
    if not kubeconfig.is_absolute() or not kubeconfig.is_file() or kubeconfig.stat().st_mode & 0o077:
        raise RuntimeError("kubeconfig must be an absolute path with mode 0600 or stricter")
    server = kubectl(client, "config", "view", "--minify", "-o",
                     "jsonpath={.clusters[0].cluster.server}").stdout.strip()
    if server not in ("https://10.20.1.28:6443", "https://10.20.1.31:6443"):
        raise RuntimeError("refusing API endpoint outside the isolated lab: " + server)
    nodes = resource(client, "get", "nodes").get("items", [])
    observed = {}
    for node in nodes:
        name = node.get("metadata", {}).get("name")
        internal_ip = next((item.get("address") for item in node.get("status", {}).get("addresses", [])
                            if item.get("type") == "InternalIP"), None)
        observed[name] = internal_ip
        if not any(item.get("type") == "Ready" and item.get("status") == "True"
                   for item in node.get("status", {}).get("conditions", [])):
            raise RuntimeError("lab node is not Ready: " + str(name))
    if observed != EXPECTED_NODES:
        raise RuntimeError("refusing unexpected cluster node identity: " + repr(observed))

    prometheus = resource(client, "-n", MONITORING, "get", "prometheus", PROMETHEUS)
    if (prometheus.get("spec", {}).get("ruleSelector", {}).get("matchLabels", {}).get("release") != RELEASE or
            prometheus.get("spec", {}).get("ruleNamespaceSelector") != {}):
        raise RuntimeError("Prometheus rule selectors differ from the isolated lab contract")
    rule = resource(client, "-n", MONITORING, "get", "prometheusrule", "atlas-workload-health")
    labels = rule.get("metadata", {}).get("labels", {})
    rules = {item.get("alert"): item for group in rule.get("spec", {}).get("groups", [])
             for item in group.get("rules", [])}
    if (labels.get("release") != RELEASE or labels.get("atlas-kubekit.io/managed-by") != "monitoring" or
            not {"AtlasJobRunningTooLong", "AtlasCronJobScheduleOverdue"}.issubset(rules)):
        raise RuntimeError("managed workload timeout rules are not selected as expected")
    configs = resource(client, "get", "alertmanagerconfigs.monitoring.coreos.com", "--all-namespaces")
    if configs.get("items"):
        raise RuntimeError("refusing to generate real alerts while custom notification routes exist")
    return rule


def shortened_rules(source):
    """Copy the deployed production expressions and shorten only test thresholds."""
    rules = {item.get("alert"): item for group in source.get("spec", {}).get("groups", [])
             for item in group.get("rules", [])}
    selected = []
    thresholds = {
        "AtlasJobRunningTooLong": (r"(time\(\) - kube_job_status_start_time > )\d+", "60"),
        "AtlasCronJobScheduleOverdue": (r"(time\(\) - kube_cronjob_next_schedule_time > )\d+", "60"),
    }
    for name, (pattern, threshold) in thresholds.items():
        rule = copy.deepcopy(rules[name])
        expression, count = re.subn(pattern, lambda match: match.group(1) + threshold,
                                    rule.get("expr", ""), count=1)
        if count != 1:
            raise RuntimeError("could not safely shorten the deployed " + name + " expression")
        rule["expr"] = expression
        rule["for"] = "1s"
        selected.append(rule)
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": "atlas-workload-timeout-e2e-" + uuid.uuid4().hex[:8],
            "namespace": MONITORING,
            "labels": copy.deepcopy(source.get("metadata", {}).get("labels", {})),
        },
        "spec": {"groups": [{"name": "atlas.workload.timeout.e2e", "rules": selected}]},
    }


def alert_items(prometheus_url):
    with urlopen(prometheus_url.rstrip("/") + "/api/v1/alerts", timeout=5) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError("Prometheus alert query failed")
    return payload.get("data", {}).get("alerts", [])


def wait_alert(prometheus_url, alert_name, namespace, identifying_label, identifying_value, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = [item for item in alert_items(prometheus_url)
                   if item.get("state") == "firing" and
                   item.get("labels", {}).get("alertname") == alert_name and
                   item.get("labels", {}).get("namespace") == namespace and
                   item.get("labels", {}).get(identifying_label) == identifying_value]
        if matches:
            summary = matches[0].get("annotations", {}).get("summary", "")
            if not re.search(r"[\u4e00-\u9fff]", summary):
                raise RuntimeError(alert_name + " summary is not localized in Chinese")
            print("PASS: " + alert_name + " fired with " + identifying_label + "=" + identifying_value)
            print("Chinese summary: " + summary)
            return
        time.sleep(5)
    raise RuntimeError("timed out waiting for " + alert_name + " in " + namespace)


def wait_controller_manager(client, present, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = kubectl(client, "-n", "kube-system", "get", "pods", "-l",
                         "component=kube-controller-manager", "-o", "json", check=False)
        if result.returncode == 0:
            items = json.loads(result.stdout).get("items", [])
            running = any(any(condition.get("type") == "Ready" and condition.get("status") == "True"
                              for condition in pod.get("status", {}).get("conditions", []))
                          for pod in items)
            if running == present and bool(items) == present:
                return
        time.sleep(2)
    raise RuntimeError("kube-controller-manager did not reach expected presence=" + str(present))


def wait_clean(client, prometheus_url, namespace, rule_name, timeout=120):
    kubectl(client, "delete", "namespace", namespace, "--wait=true", "--timeout=90s", check=False,
            timeout=100)
    kubectl(client, "-n", MONITORING, "delete", "prometheusrule", rule_name, "--wait=true", check=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ns = kubectl(client, "get", "namespace", namespace, "--ignore-not-found", "-o", "name", check=False)
        rule = kubectl(client, "-n", MONITORING, "get", "prometheusrule", rule_name,
                       "--ignore-not-found", "-o", "name", check=False)
        alerts = [item for item in alert_items(prometheus_url)
                  if item.get("labels", {}).get("namespace") == namespace]
        if not ns.stdout.strip() and not rule.stdout.strip() and not alerts:
            print("PASS: temporary namespace/rule were removed and alerts resolved.")
            return
        time.sleep(5)
    raise RuntimeError("temporary resources or alerts remain after cleanup")


def run(args):
    client = ["kubectl", "--kubeconfig", str(args.kubeconfig)]
    source = verify_lab(client, args.kubeconfig)
    test_rule = shortened_rules(source)
    rule_name = test_rule["metadata"]["name"]
    namespace = "atlas-workload-timeout-e2e-" + uuid.uuid4().hex[:8]
    manifest = Path("/etc/kubernetes/manifests/kube-controller-manager.yaml")
    backup = manifest.with_name("." + manifest.name + ".atlas-e2e-" + uuid.uuid4().hex + ".bak")
    cancel = Path(tempfile.gettempdir()) / ("atlas-e2e-watchdog-" + uuid.uuid4().hex)
    watchdog = None
    port_forward = None
    port_forward_log = tempfile.TemporaryFile(mode="w+")
    created_namespace = False
    created_rule = False
    manifest_moved = False
    test_error = None
    prometheus_url = args.prometheus_url

    try:
        kubectl(client, "-n", MONITORING, "apply", "-f", "-", input_text=json.dumps(test_rule))
        created_rule = True
        kubectl(client, "create", "namespace", namespace)
        created_namespace = True
        job = {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": "job-probe", "namespace": namespace,
                         "labels": {"app.kubernetes.io/managed-by": "atlas-kubekit-e2e"}},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 3600,
                     "template": {"spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                                             "containers": [{"name": "pause", "image": PAUSE_IMAGE,
                                                             "imagePullPolicy": "IfNotPresent"}]}}},
        }
        kubectl(client, "-n", namespace, "apply", "-f", "-", input_text=json.dumps(job))
        port_forward = subprocess.Popen(
            client + ["-n", MONITORING, "port-forward", "--address", "127.0.0.1",
                      "svc/kube-prometheus-stack-prometheus", "19090:9090"],
            stdin=subprocess.DEVNULL, stdout=port_forward_log, stderr=subprocess.STDOUT,
            start_new_session=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with urlopen(prometheus_url.rstrip("/") + "/-/ready", timeout=2) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("Prometheus port-forward did not become ready")

        wait_alert(prometheus_url, "AtlasJobRunningTooLong", namespace, "job_name", "job-probe")

        if not manifest.is_file() or backup.exists():
            raise RuntimeError("controller-manager manifest is missing or a backup already exists")
        watchdog = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--restore-watchdog",
             str(backup), str(manifest), str(cancel), "240", str(args.kubeconfig), namespace, rule_name],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        shutil.move(str(manifest), str(backup))
        manifest_moved = True
        wait_controller_manager(client, present=False, timeout=45)
        print("PASS: isolated kube-controller-manager stopped under a 240-second restore watchdog.")

        cronjob = {
            "apiVersion": "batch/v1", "kind": "CronJob",
            "metadata": {"name": "cronjob-probe", "namespace": namespace,
                         "labels": {"app.kubernetes.io/managed-by": "atlas-kubekit-e2e"}},
            "spec": {"schedule": "0 * * * *", "startingDeadlineSeconds": 1, "suspend": False,
                     "concurrencyPolicy": "Forbid", "jobTemplate": {"spec": {
                         "backoffLimit": 0, "template": {"spec": {"restartPolicy": "Never",
                             "automountServiceAccountToken": False,
                             "containers": [{"name": "pause", "image": PAUSE_IMAGE,
                                             "imagePullPolicy": "IfNotPresent"}]}}}}},
        }
        kubectl(client, "-n", namespace, "apply", "-f", "-", input_text=json.dumps(cronjob))
        old_schedule = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200))
        kubectl(client, "-n", namespace, "patch", "cronjob", "cronjob-probe", "--subresource=status",
                "--type=merge", "-p", json.dumps({"status": {"lastScheduleTime": old_schedule}}))
        wait_alert(prometheus_url, "AtlasCronJobScheduleOverdue", namespace, "cronjob", "cronjob-probe")
    except BaseException as error:
        test_error = error
    finally:
        if manifest_moved and backup.exists():
            shutil.move(str(backup), str(manifest))
            manifest_moved = False
        if watchdog is not None:
            cancel.touch()
            try:
                watchdog.wait(timeout=5)
            except subprocess.TimeoutExpired:
                watchdog.terminate()
                watchdog.wait(timeout=5)
        try:
            wait_controller_manager(client, present=True, timeout=120)
            nodes = resource(client, "get", "nodes").get("items", [])
            if len(nodes) != len(EXPECTED_NODES) or any(
                    not any(condition.get("type") == "Ready" and condition.get("status") == "True"
                            for condition in node.get("status", {}).get("conditions", []))
                    for node in nodes):
                raise RuntimeError("lab nodes are not all Ready after control-plane restoration")
            if port_forward is not None:
                wait_clean(client, prometheus_url, namespace, rule_name)
            else:
                kubectl(client, "delete", "namespace", namespace, "--wait=true", "--timeout=90s",
                        check=False, timeout=100)
                kubectl(client, "-n", MONITORING, "delete", "prometheusrule", rule_name,
                        check=False)
        except BaseException as cleanup_error:
            if test_error is None:
                test_error = cleanup_error
            else:
                print("CLEANUP_FAILED: " + str(cleanup_error), file=sys.stderr)
        if port_forward is not None:
            port_forward.terminate()
            try:
                port_forward.wait(timeout=5)
            except subprocess.TimeoutExpired:
                port_forward.kill()
                port_forward.wait(timeout=5)
        cancel.unlink(missing_ok=True)
        port_forward_log.close()
        if created_namespace or created_rule:
            # Idempotent fallback in case cleanup verification itself failed.
            kubectl(client, "delete", "namespace", namespace, "--wait=true", "--timeout=90s",
                    check=False, timeout=100)
            kubectl(client, "-n", MONITORING, "delete", "prometheusrule", rule_name,
                    check=False)
    if test_error is not None:
        raise test_error


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--restore-watchdog":
        _, _, backup, manifest, cancel, timeout, kubeconfig, namespace, rule_name = sys.argv
        return watchdog_main(Path(backup), Path(manifest), Path(cancel), int(timeout),
                             kubeconfig, namespace, rule_name)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:19090")
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != CONFIRMATION:
        parser.error("explicit confirmation " + CONFIRMATION + " is required")
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
