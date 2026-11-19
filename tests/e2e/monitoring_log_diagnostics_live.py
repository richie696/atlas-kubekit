#!/usr/bin/env python3
"""Verify real Loki log correlation and Grafana link policy in the retained lab."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.operations.monitoring import relay_publication  # noqa: E402

CONTEXT = "atlas-e2e-lab"
NAMESPACE = "monitoring"
RELEASE = "kube-prometheus-stack"
PYTHON_IMAGE = "m.daocloud.io/docker.io/library/python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f"
LOKI_ENDPOINT = "http://loki.logging.svc.cluster.local:3100"

RUNNER = r'''import os, sys, time, json
from datetime import datetime, timezone
sys.path.insert(0, "/opt/code/relay.pyz")
os.environ["ATLAS_LOCALES_DIR"] = "/opt/code/locales"
from scripts.operations.monitoring import loki, log_diagnostics
from scripts.operations.monitoring.channels.alertmanager import Notification

namespace, pod, marker = sys.argv[1:]
now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
labels = {"namespace": namespace, "pod": pod, "container": "logger", "node": "w2"}
alert = {"name": "AtlasLogDiagnosticsE2E", "status": "firing", "labels": labels,
         "startsAt": now, "endsAt": "0001-01-01T00:00:00Z"}
settings = {"loki_endpoint": "http://loki.logging.svc.cluster.local:3100",
            "before_seconds": 300, "after_seconds": 60, "max_lines": 20,
            "max_message_bytes": 8192}
result = None
deadline = time.monotonic() + 150
while time.monotonic() < deadline:
    result = loki.query(settings["loki_endpoint"], alert, 300, 60, 20)
    if result.status == "ok" and any(marker in line for line in result.lines):
        break
    time.sleep(5)
else:
    raise SystemExit("unique test log marker was not found in real Loki query results")

node_alert = {"name": "AtlasNodeLogDiagnosticsE2E", "status": "firing",
              "labels": {"node": "w2"}, "startsAt": now,
              "endsAt": "0001-01-01T00:00:00Z"}
node_result = loki.query(settings["loki_endpoint"], node_alert, 300, 60, 20)
if node_result.status != "ok" or not any(marker in line for line in node_result.lines):
    raise SystemExit("node-only alert context did not find the unique real Loki marker")

notification = Notification("e2e", "中文测试告警：日志关联检查", "firing", (alert,))
query = lambda endpoint, record, before, after, limit: loki.query(endpoint, record, before, after, limit)
without_link = log_diagnostics.enrich_text(notification, settings, query, "zh_CN")
assert marker in without_link, "bounded diagnostic text omitted the real Loki marker"
assert "/explore?" not in without_link, "Grafana link was emitted without a configured public HTTPS entry"

linked_settings = {**settings, "grafana_explore_url": "https://grafana.example.com",
                   "loki_datasource_uid": "loki-main"}
with_link = log_diagnostics.enrich_text(notification, linked_settings, query, "zh_CN")
assert marker in with_link, "linked notification omitted the real Loki marker"
assert "https://grafana.example.com/explore?" in with_link, "configured HTTPS Explore link was omitted"
node_notification = Notification("e2e-node", "中文测试告警：节点日志关联检查", "firing", (node_alert,))
node_text = log_diagnostics.enrich_text(node_notification, settings, query, "zh_CN")
assert marker in node_text, "node-level alert diagnostics omitted the real Loki marker"
assert "/explore?" not in node_text, "node-level alert emitted an unconfigured Grafana link"
print("LOG_DIAGNOSTICS_E2E_PASS real_loki_marker=yes node_selector=yes firing_no_link=yes configured_https_link=yes")
'''


def kubectl(kubeconfig, *args, input_text=None, check=True, timeout=60):
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), "--context", CONTEXT,
         "--request-timeout=20s", *args], input=input_text, text=True,
        capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("kubectl failed: " + " ".join(args[:4]) + "\n" + result.stderr[-1600:])
    return result


def resource(kubeconfig, *args):
    return json.loads(kubectl(kubeconfig, *args, "-o", "json").stdout)


def verify_lab(kubeconfig):
    if not kubeconfig.is_absolute() or not kubeconfig.is_file() or kubeconfig.stat().st_mode & 0o077:
        raise RuntimeError("an absolute mode-0600 lab kubeconfig is required")
    selected = resource(kubeconfig, "config", "view", "--minify")
    if selected.get("contexts", [{}])[0].get("name") != CONTEXT:
        raise RuntimeError("refusing a kubeconfig outside the retained E2E context")
    nodes = resource(kubeconfig, "get", "nodes").get("items", [])
    observed = {item["metadata"]["name"]: next(
        (a["address"] for a in item.get("status", {}).get("addresses", [])
         if a.get("type") == "InternalIP"), None) for item in nodes}
    if observed != {"cp1": "10.20.1.28", "w1": "10.20.1.29", "w2": "10.20.1.30"}:
        raise RuntimeError("refusing unexpected lab identity: " + repr(observed))
    loki = resource(kubeconfig, "-n", "logging", "get", "statefulset", "loki")
    if loki.get("status", {}).get("readyReplicas", 0) < 1:
        raise RuntimeError("Loki is not Ready")
    alloy = resource(kubeconfig, "-n", "logging", "get", "deployment", "alloy")
    if alloy.get("status", {}).get("readyReplicas", 0) < 1:
        raise RuntimeError("Alloy is not Ready")
    for kind, name in (("pod", "atlas-log-diagnostics-e2e"),
                       ("job", "atlas-log-diagnostics-e2e"),
                       ("configmap", "atlas-log-diagnostics-e2e")):
        found = kubectl(kubeconfig, "-n", "monitoring", "get", kind, name,
                        "--ignore-not-found", "-o", "name", check=False)
        if found.returncode or found.stdout.strip():
            raise RuntimeError("test resource name collision: " + kind + "/" + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != "atlas-log-diagnostics-e2e":
        parser.error("explicit confirmation atlas-log-diagnostics-e2e is required")
    verify_lab(args.kubeconfig)
    marker = "ATLAS_LOG_DIAGNOSTICS_" + uuid.uuid4().hex
    created = []
    code = relay_publication.code_config_map(NAMESPACE, RELEASE)
    existing_code = kubectl(args.kubeconfig, "-n", NAMESPACE, "get", "configmap",
                            code["metadata"]["name"], "--ignore-not-found", "-o", "name", check=False)
    if existing_code.returncode or existing_code.stdout.strip():
        raise RuntimeError("content-addressed code ConfigMap already exists; refusing collision")
    logger = {"apiVersion": "v1", "kind": "Pod",
              "metadata": {"name": "atlas-log-diagnostics-e2e", "namespace": NAMESPACE,
                           "labels": {"app": "atlas-log-diagnostics-e2e",
                                      "atlas-kubekit.io/managed-by": "monitoring-e2e"}},
              "spec": {"nodeSelector": {"kubernetes.io/hostname": "w2"},
                       "restartPolicy": "Always", "automountServiceAccountToken": False,
                       "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                           "runAsGroup": 10001},
                       "containers": [{"name": "logger", "image": PYTHON_IMAGE,
                         "imagePullPolicy": "IfNotPresent", "command": ["python", "-u", "-c",
                         "import time\nmarker=" + repr(marker) + "\nwhile True:\n print(marker, flush=True)\n time.sleep(2)"],
                         "securityContext": {"allowPrivilegeEscalation": False,
                                             "readOnlyRootFilesystem": True,
                                             "capabilities": {"drop": ["ALL"]}}}]}}
    code["metadata"].setdefault("labels", {})["atlas-kubekit.io/managed-by"] = "monitoring-e2e"
    code["metadata"].setdefault("annotations", {})["atlas-kubekit.io/e2e"] = marker
    runner_cm = {"apiVersion": "v1", "kind": "ConfigMap",
                 "metadata": {"name": "atlas-log-diagnostics-e2e", "namespace": NAMESPACE,
                              "labels": {"atlas-kubekit.io/managed-by": "monitoring-e2e"},
                              "annotations": {"atlas-kubekit.io/e2e": marker}},
                 "data": {"runner.py": RUNNER}}
    runner = {"apiVersion": "batch/v1", "kind": "Job",
              "metadata": {"name": "atlas-log-diagnostics-e2e", "namespace": NAMESPACE,
                           "labels": {"atlas-kubekit.io/managed-by": "monitoring-e2e"},
                           "annotations": {"atlas-kubekit.io/e2e": marker}},
              "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 240,
                       "template": {"metadata": {"labels": {"atlas-kubekit.io/managed-by": "monitoring-e2e"}},
                         "spec": {"nodeSelector": {"kubernetes.io/hostname": "w2"},
                                  "restartPolicy": "Never", "automountServiceAccountToken": False,
                                  "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                                      "runAsGroup": 10001},
                                  "containers": [{"name": "query", "image": PYTHON_IMAGE,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["python", "/run/runner.py", NAMESPACE,
                                                "atlas-log-diagnostics-e2e", marker],
                                    "securityContext": {"allowPrivilegeEscalation": False,
                                                        "readOnlyRootFilesystem": True,
                                                        "capabilities": {"drop": ["ALL"]}},
                                    "volumeMounts": [{"name": "code", "mountPath": "/opt/code", "readOnly": True},
                                                     {"name": "runner", "mountPath": "/run", "readOnly": True}]}],
                                  "volumes": [{"name": "code", "configMap": {"name": code["metadata"]["name"],
                                    "items": [{"key": "relay.pyz", "path": "relay.pyz"},
                                              {"key": "en.json", "path": "locales/en.json"},
                                              {"key": "zh_cn.json", "path": "locales/zh_cn.json"},
                                              {"key": "zh_tw.json", "path": "locales/zh_tw.json"}]}},
                                              {"name": "runner", "configMap": {"name": runner_cm["metadata"]["name"]}}]}}}}
    try:
        for manifest in (code, runner_cm, logger, runner):
            kubectl(args.kubeconfig, "create", "-f", "-", input_text=json.dumps(manifest))
            created.append((manifest["kind"], manifest["metadata"]["name"]))
        kubectl(args.kubeconfig, "-n", NAMESPACE, "wait", "--for=condition=Ready",
                "pod/atlas-log-diagnostics-e2e", "--timeout=120s", timeout=130)
        kubectl(args.kubeconfig, "-n", NAMESPACE, "wait", "--for=condition=complete",
                "job/atlas-log-diagnostics-e2e", "--timeout=210s", timeout=220)
        logs = kubectl(args.kubeconfig, "-n", NAMESPACE, "logs", "job/atlas-log-diagnostics-e2e",
                       timeout=30).stdout
        if "LOG_DIAGNOSTICS_E2E_PASS" not in logs:
            raise RuntimeError("log diagnostic E2E did not report success: " + logs[-1200:])
        print(logs.strip())
    finally:
        for kind, name in reversed(created):
            kubectl(args.kubeconfig, "-n", NAMESPACE, "delete", kind, name,
                    "--ignore-not-found", "--wait=true", "--timeout=60s", check=False, timeout=70)


if __name__ == "__main__":
    main()
