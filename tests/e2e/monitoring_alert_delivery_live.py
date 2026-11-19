#!/usr/bin/env python3
"""Prove a real Prometheus rule fires and resolves through managed Alertmanager routing."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import time
import uuid
from urllib.error import URLError
from urllib.request import urlopen


EXPECTED_NODES = {"cp1": "10.20.1.28", "w1": "10.20.1.29", "w2": "10.20.1.30"}
CONTEXT = "atlas-e2e-lab"
NAMESPACE = "monitoring"
RELEASE = "kube-prometheus-stack"
PROMETHEUS = "kube-prometheus-stack-prometheus"
ALERTMANAGER = "kube-prometheus-stack-alertmanager"
OWNER_LABEL = "atlas-kubekit.io/managed-by"
OWNER_VALUE = "monitoring"

RECEIVER_SCRIPT = """#!/bin/sh
printf 'Content-Type: text/plain\\r\\n\\r\\n'
cat >> /tmp/e2e/alerts.jsonl
printf '\\n' >> /tmp/e2e/alerts.jsonl
printf 'accepted\\n'
"""


def command(kubeconfig, args, *, input_text=None, timeout=45, check=True):
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig), "--context", CONTEXT,
         "--request-timeout=20s", *args],
        input=input_text, text=True, capture_output=True, timeout=timeout,
    )
    if check and result.returncode:
        raise RuntimeError("kubectl failed: " + " ".join(args[:4]) + "\n" + result.stderr[-1600:])
    return result


def resource(kubeconfig, *args):
    return json.loads(command(kubeconfig, [*args, "-o", "json"]).stdout)


def apply(kubeconfig, manifest):
    command(kubeconfig, ["apply", "-f", "-"], input_text=json.dumps(manifest))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_forward(kubeconfig, resource_name, local_port, remote_port):
    process = subprocess.Popen(
        ["kubectl", "--kubeconfig", str(kubeconfig), "--context", CONTEXT,
         "-n", NAMESPACE, "port-forward", "--address=127.0.0.1",
         resource_name, f"{local_port}:{remote_port}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return process


def get_json(url, timeout=5):
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_ready(url, timeout=5):
    with urlopen(url, timeout=timeout) as response:
        return response.status == 200


def wait_for(predicate, timeout, message, process=None):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("port-forward exited before verification completed")
        try:
            result = predicate()
            if result:
                return result
        except (OSError, URLError, ValueError, KeyError, IndexError, TypeError) as exc:
            last_error = exc
        time.sleep(2)
    detail = "" if last_error is None else "; last observation: " + type(last_error).__name__
    raise RuntimeError(message + detail)


def verify_lab(kubeconfig):
    if not kubeconfig.is_absolute() or not kubeconfig.is_file():
        raise RuntimeError("an absolute lab kubeconfig is required")
    if kubeconfig.stat().st_mode & 0o077:
        raise RuntimeError("lab kubeconfig must be mode 0600 or stricter")
    selected = resource(kubeconfig, "config", "view", "--minify")
    if selected.get("contexts", [{}])[0].get("name") != CONTEXT:
        raise RuntimeError("refusing a kubeconfig that is not the retained E2E context")
    nodes = resource(kubeconfig, "get", "nodes").get("items", [])
    observed = {}
    for node in nodes:
        name = node.get("metadata", {}).get("name")
        internal_ip = next((address.get("address") for address in
                            node.get("status", {}).get("addresses", [])
                            if address.get("type") == "InternalIP"), None)
        ready = any(condition.get("type") == "Ready" and condition.get("status") == "True"
                    for condition in node.get("status", {}).get("conditions", []))
        if not ready:
            raise RuntimeError("lab node is not Ready: " + str(name))
        observed[name] = internal_ip
    if observed != EXPECTED_NODES:
        raise RuntimeError("refusing unexpected cluster identity: " + repr(observed))

    prometheus = resource(kubeconfig, "-n", NAMESPACE, "get", "prometheus", PROMETHEUS)
    prometheus_spec = prometheus.get("spec", {})
    if (prometheus_spec.get("ruleSelector", {}).get("matchLabels", {}).get("release") != RELEASE or
            prometheus_spec.get("ruleNamespaceSelector") != {}):
        raise RuntimeError("Prometheus rule selector differs from the retained lab contract")
    alertmanager = resource(kubeconfig, "-n", NAMESPACE, "get", "alertmanager", ALERTMANAGER)
    selector = alertmanager.get("spec", {}).get("alertmanagerConfigSelector", {}).get("matchLabels", {})
    if selector.get(OWNER_LABEL) != OWNER_VALUE:
        raise RuntimeError("Alertmanager does not select the Atlas managed test route")
    configs = resource(kubeconfig, "get", "alertmanagerconfigs.monitoring.coreos.com",
                       "--all-namespaces").get("items", [])
    if configs:
        raise RuntimeError("refusing to test while AlertmanagerConfig routes already exist")
    for kind, name in (("pod", "atlas-notify-receiver"), ("service", "atlas-notify-receiver"),
                       ("configmap", "atlas-notify-receiver"),
                       ("prometheusrule", "atlas-notification-chain-e2e"),
                       ("alertmanagerconfig", "atlas-notification-chain-e2e")):
        existing = command(kubeconfig, ["-n", NAMESPACE, "get", kind, name,
                                        "--ignore-not-found", "-o", "name"], check=False)
        if existing.returncode or existing.stdout.strip():
            raise RuntimeError("a deterministic test resource name already exists; refusing collision")


def receiver_resources(token):
    owned = {"app.kubernetes.io/managed-by": "atlas-kubekit-e2e",
             "atlas-kubekit.io/e2e": token}
    return [
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": "atlas-notify-receiver", "namespace": NAMESPACE,
                      "labels": owned},
         "data": {"receive": RECEIVER_SCRIPT}},
        {"apiVersion": "v1", "kind": "Pod",
         "metadata": {"name": "atlas-notify-receiver", "namespace": NAMESPACE,
                      "labels": {**owned, "app": "atlas-notify-receiver"}},
         "spec": {"nodeSelector": {"kubernetes.io/hostname": "w1"},
                  "securityContext": {"runAsNonRoot": True, "runAsUser": 65534,
                                      "runAsGroup": 65534, "fsGroup": 65534},
                  "containers": [{"name": "receiver", "image": "busybox:1.38.0",
                      "imagePullPolicy": "Never", "command": ["/bin/sh", "-ec",
                      "exec httpd -f -p 18080 -h /srv"],
                      "ports": [{"name": "webhook", "containerPort": 18080}],
                      "securityContext": {"allowPrivilegeEscalation": False,
                                          "readOnlyRootFilesystem": True,
                                          "capabilities": {"drop": ["ALL"]}},
                      "volumeMounts": [{"name": "cgi", "mountPath": "/srv/cgi-bin",
                                        "readOnly": True},
                                       {"name": "data", "mountPath": "/tmp/e2e"}]}],
                  "volumes": [{"name": "cgi", "configMap": {"name": "atlas-notify-receiver",
                                                               "defaultMode": 365}},
                              {"name": "data", "emptyDir": {}}]}},
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": "atlas-notify-receiver", "namespace": NAMESPACE,
                      "labels": owned},
         "spec": {"selector": {"app": "atlas-notify-receiver",
                                 "atlas-kubekit.io/e2e": token},
                  "ports": [{"name": "webhook", "port": 18080, "targetPort": 18080}]}}
    ]


def alertmanager_config(token):
    return {"apiVersion": "monitoring.coreos.com/v1alpha1", "kind": "AlertmanagerConfig",
            "metadata": {"name": "atlas-notification-chain-e2e", "namespace": NAMESPACE,
                         "labels": {"release": RELEASE, OWNER_LABEL: OWNER_VALUE,
                                    "atlas-kubekit.io/e2e": token}},
            "spec": {"route": {"receiver": "local-e2e-webhook",
                                "matchers": [{"name": "atlas_e2e_id", "value": token,
                                              "matchType": "="}],
                                "groupBy": ["alertname", "atlas_e2e_id"],
                                "groupWait": "1s", "groupInterval": "1s",
                                "repeatInterval": "10m"},
                      "receivers": [{"name": "local-e2e-webhook",
                                     "webhookConfigs": [{"url": "http://atlas-notify-receiver.monitoring.svc:18080/cgi-bin/receive",
                                                         "sendResolved": True}]}]}}


def prometheus_rule(token, alert_name, *, firing=True):
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": "atlas-notification-chain-e2e", "namespace": NAMESPACE,
                         "labels": {"release": RELEASE, OWNER_LABEL: OWNER_VALUE,
                                    "atlas-kubekit.io/e2e": token}},
            "spec": {"groups": [{"name": "atlas.notification.delivery.e2e",
                                  "interval": "15s", "rules": [{"alert": alert_name,
                                      "expr": "vector(1) > 0" if firing else "vector(0) > 0",
                                      "for": "15s",
                                      "labels": {"severity": "warning", "atlas_e2e_id": token,
                                                 "team": "platform"},
                                      "annotations": {"summary": "Atlas 隔离集群告警送达测试",
                                                      "description": "验证 Prometheus 告警经受管 Alertmanager 路由到达集群内测试接收器。",
                                                      "observed_value": "vector(1)"}}]}]}}


def active_alert(prometheus_url, alert_name, token, state):
    data = get_json(prometheus_url + "/api/v1/alerts").get("data", {}).get("alerts", [])
    return any(item.get("labels", {}).get("alertname") == alert_name and
               item.get("labels", {}).get("atlas_e2e_id") == token and
               item.get("state") == state for item in data)


def received_status(kubeconfig, status, token):
    result = command(kubeconfig, ["-n", NAMESPACE, "exec", "pod/atlas-notify-receiver",
                                  "--", "cat", "/tmp/e2e/alerts.jsonl"], check=False)
    if result.returncode:
        return False
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        alerts = message.get("alerts", [])
        if (message.get("status") == status and any(
                alert.get("labels", {}).get("atlas_e2e_id") == token and
                alert.get("annotations", {}).get("summary") == "Atlas 隔离集群告警送达测试"
                for alert in alerts)):
            return message
    return False


def delete_owned(kubeconfig, kind, name):
    command(kubeconfig, ["-n", NAMESPACE, "delete", kind, name,
                        "--ignore-not-found", "--wait=true", "--timeout=60s"],
            timeout=70, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != "atlas-monitor-notification-chain-e2e":
        parser.error("explicit confirmation atlas-monitor-notification-chain-e2e is required")
    verify_lab(args.kubeconfig)

    token = "atlas-notify-" + uuid.uuid4().hex[:12]
    alert_name = "AtlasNotificationDeliveryE2E"
    config_name = "atlas-notification-chain-e2e"
    rule_name = "atlas-notification-chain-e2e"
    resources_created = {"receiver": False, "config": False, "rule": False}
    forwards = []
    try:
        # The names were checked absent before this point. Mark ownership up front
        # so a partial manifest-apply failure still removes earlier receiver objects.
        resources_created["receiver"] = True
        for manifest in receiver_resources(token):
            apply(args.kubeconfig, manifest)
        command(args.kubeconfig, ["-n", NAMESPACE, "wait", "--for=condition=Ready",
                                  "pod/atlas-notify-receiver", "--timeout=90s"], timeout=100)

        # Validate the managed route before letting Prometheus create an alert.
        command(args.kubeconfig, ["-n", NAMESPACE, "create", "--dry-run=server", "-f", "-"],
                input_text=json.dumps(alertmanager_config(token)))
        apply(args.kubeconfig, alertmanager_config(token))
        resources_created["config"] = True

        prom_port, am_port = free_port(), free_port()
        prom_forward = port_forward(args.kubeconfig, "svc/kube-prometheus-stack-prometheus",
                                    prom_port, 9090)
        am_forward = port_forward(args.kubeconfig, "svc/kube-prometheus-stack-alertmanager",
                                  am_port, 9093)
        forwards.extend([prom_forward, am_forward])
        prom_url = "http://127.0.0.1:" + str(prom_port)
        am_url = "http://127.0.0.1:" + str(am_port)
        wait_for(lambda: http_ready(prom_url + "/-/ready"),
                 30, "Prometheus port-forward did not become ready", prom_forward)
        wait_for(lambda: http_ready(am_url + "/-/ready"),
                 30, "Alertmanager port-forward did not become ready", am_forward)

        def route_loaded():
            status = get_json(am_url + "/api/v2/status")
            original = status.get("config", {}).get("original", "")
            return token in original
        wait_for(route_loaded, 90, "Alertmanager Operator did not load the unique local test route",
                 am_forward)

        rule = prometheus_rule(token, alert_name)
        command(args.kubeconfig, ["-n", NAMESPACE, "create", "--dry-run=server", "-f", "-"],
                input_text=json.dumps(rule))
        apply(args.kubeconfig, rule)
        resources_created["rule"] = True

        def rule_healthy():
            document = get_json(prom_url + "/api/v1/rules")
            for group in document.get("data", {}).get("groups", []):
                for item in group.get("rules", []):
                    if item.get("name") == alert_name:
                        return item.get("health") == "ok"
            return False
        wait_for(rule_healthy, 120, "Prometheus did not load the isolated delivery rule", prom_forward)
        wait_for(lambda: active_alert(prom_url, alert_name, token, "firing"),
                 180, "Prometheus rule did not enter firing", prom_forward)
        firing = wait_for(lambda: received_status(args.kubeconfig, "firing", token),
                          90, "local Alertmanager receiver did not capture the firing payload")
        payload = firing["alerts"][0]
        if payload.get("labels", {}).get("severity") != "warning" or not payload.get("annotations", {}).get("description"):
            raise RuntimeError("firing payload lost the rule's actionable labels or description")
        print("PASS: Prometheus firing rule reached the managed Alertmanager route and local HTTP receiver.")

        recovery_rule = prometheus_rule(token, alert_name, firing=False)
        apply(args.kubeconfig, recovery_rule)
        print("Prometheus rule condition changed to false; waiting for Alertmanager resolution.")
        resolved = wait_for(lambda: received_status(args.kubeconfig, "resolved", token),
                            180, "local Alertmanager receiver did not capture the resolved payload")
        if not any(alert.get("labels", {}).get("atlas_e2e_id") == token
                   for alert in resolved.get("alerts", [])):
            raise RuntimeError("resolved payload did not preserve the unique alert identity")
        print("PASS: the false rule condition produced a resolved webhook with the same alert identity.")
    finally:
        if resources_created["rule"]:
            delete_owned(args.kubeconfig, "prometheusrule", rule_name)
        if resources_created["config"]:
            delete_owned(args.kubeconfig, "alertmanagerconfig", config_name)
        if resources_created["receiver"]:
            for kind in ("service", "pod", "configmap"):
                delete_owned(args.kubeconfig, kind, "atlas-notify-receiver")
        for process in forwards:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        residual = command(args.kubeconfig, ["-n", NAMESPACE, "get",
            "alertmanagerconfig,service,pod,configmap,prometheusrule",
            "-l", "atlas-kubekit.io/e2e=" + token, "-o", "name"], check=False)
        if residual.returncode == 0 and residual.stdout.strip():
            raise RuntimeError("temporary E2E resources remain: " + residual.stdout.strip())
        print("Temporary route, rule, receiver and test resources cleaned up.")


if __name__ == "__main__":
    main()
