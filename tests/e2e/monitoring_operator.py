#!/usr/bin/env python3
"""Isolated kind + Prometheus Operator: publish rule and verify actual loading."""
import argparse
import contextlib
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
from unittest.mock import patch
from types import SimpleNamespace
from urllib.request import urlopen
from urllib.parse import urlencode
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.adapters.kubernetes import Kubernetes
from scripts.operations import configuration, cli as operations_cli
from scripts.operations.capabilities.registry import CAPABILITIES
from scripts.operations.monitoring import coverage_report, prometheus, relay_publication, inventory, notification_secrets
from scripts.addons.sources import yaml_module
from scripts.addons.monitoring_values import safe_monitoring_values
from scripts.operations.capabilities import monitoring_scrapes, monitoring_control_plane, monitoring_dns, monitoring_certificates, monitoring_workloads, monitoring_workload_health, monitoring_storage, monitoring_statefulsets, monitoring_daemonsets, monitoring_longhorn_collection, monitoring_longhorn_disk, monitoring_lb, monitoring_email, monitoring_relay_metrics
from scripts.operations.changes import execution, plans
from scripts.operations.changes.coordinator import ClusterCoordinator
from scripts.operations.models import OperationError, fingerprint
from tests.e2e.monitoring_kubernetes import command


MONITORING_CHART_NAME = "kube-prometheus-stack"
MONITORING_CHART_VERSION = "92.22.0"


def _unexpected_reapply(_values, _checkpoint):
    raise RuntimeError("successful plan replay attempted to apply resources")


def validate_monitoring_chart(path):
    """Accept only the exact chart archive used by this isolated test suite."""
    chart_path = Path(path)
    if not chart_path.is_absolute() or not chart_path.is_file():
        raise ValueError("monitoring chart must be an absolute path to a local chart archive")
    try:
        with tarfile.open(chart_path, mode="r:gz") as archive:
            member = archive.getmember(MONITORING_CHART_NAME + "/Chart.yaml")
            if not member.isfile() or member.size > 65536:
                raise ValueError("invalid monitoring chart metadata entry")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("monitoring chart metadata is unreadable")
            yaml = yaml_module()
            with source:
                try:
                    metadata = yaml.safe_load(source.read())
                except yaml.YAMLError as error:
                    raise ValueError("monitoring chart metadata is invalid") from error
    except (OSError, tarfile.TarError, KeyError, ValueError) as error:
        raise ValueError("invalid local monitoring chart archive: " + str(error)) from None
    if (not isinstance(metadata, dict) or metadata.get("name") != MONITORING_CHART_NAME
            or metadata.get("version") != MONITORING_CHART_VERSION):
        raise ValueError("local monitoring chart must be " + MONITORING_CHART_NAME + " "
                         + MONITORING_CHART_VERSION)
    return str(chart_path.resolve())


def exercise_rule_identity(client, kubectl, url, prometheus_name):
    """A healthy same-name rule must not mask a declaration excluded by the selector."""
    rule_selector = client.get(
        "prometheuses.monitoring.coreos.com", prometheus_name, "monitoring",
    )["spec"]["ruleSelector"]
    if rule_selector.get("matchLabels", {}).get("release") != "stack":
        raise RuntimeError("disposable Prometheus selector does not filter rules by release")
    group = "atlas.e2e.identity"
    risk_id = "R-OBS-01"
    selected, excluded = "atlas-e2e-rule-identity-selected", "atlas-e2e-rule-identity-excluded"

    def document(name, release):
        return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
                "metadata": {"name": name, "namespace": "monitoring", "labels": {"release": release}},
                "spec": {"groups": [{"name": group, "rules": [
                    {"alert": "AtlasE2ERuleIdentity", "expr": "vector(0) == 1",
                     "labels": {"risk_id": risk_id, "severity": "info"}},
                ]}]}}

    def observe(expected):
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            report = coverage_report.collect(client, {}, prometheus.collect_alert_rules(url))
            risk = next(item for item in report["risks"] if item["riskId"] == risk_id)
            checks = {row["resource"]: row["state"] for row in risk["ruleEvidence"]["checks"]
                      if row.get("group") == group}
            if checks == expected:
                return risk
            time.sleep(2)
        raise RuntimeError("rule identity check did not converge to " + str(expected))

    try:
        command(kubectl + ["create", "-f", "-"], data=document(selected, "stack"))
        observe({selected: "healthy"})
        command(kubectl + ["create", "-f", "-"], data=document(excluded, "outside-stack"))
        risk = observe({selected: "healthy", excluded: "missing"})
        if risk["ruleEvidence"]["state"] != "failed" or "rule-failed" not in risk["reasons"]:
            raise RuntimeError("a healthy same-name rule masked an excluded declaration")
        command(kubectl + ["label", "prometheusrule", excluded, "release=stack", "--overwrite"])
        risk = observe({selected: "healthy", excluded: "healthy"})
        if risk["ruleEvidence"]["state"] != "verified":
            raise RuntimeError("reselecting the excluded rule did not restore rule evidence")
        print("Prometheus rule audit detected an excluded same-name declaration and recovered after selection.")
    finally:
        command(kubectl + ["delete", "prometheusrule", selected, excluded, "--ignore-not-found=true"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--workload", action="store_true", help="Also test a real kube-state-metrics Deployment transition")
    parser.add_argument("--controllers", action="store_true", help="Also test real StatefulSet and DaemonSet transitions")
    parser.add_argument("--storage", action="store_true", help="Also test real kubelet metrics of a disposable hostPath PVC")
    parser.add_argument("--longhorn-collection", action="store_true", help="Verify ServiceMonitor selection and scraping with a controlled Longhorn-like target")
    parser.add_argument("--email", action="store_true", help="Verify Operator selection of a managed email route")
    parser.add_argument("--robot", action="store_true", help="Verify Operator selection of an authenticated robot route")
    parser.add_argument("--lb", action="store_true", help="Verify managed HAProxy endpoint scraping and HAProxy health rules")
    parser.add_argument("--dns-failure", action="store_true",
                        help="Exercise real CoreDNS upstream errors, latency and scrape loss in the disposable cluster")
    parser.add_argument("--keda", action="store_true", help="Install KEDA and verify real ServiceMonitor scraping plus scaler error alert/recovery")
    parser.add_argument("--workload-timeouts", action="store_true",
                        help="Also inject real Job timeout and missed CronJob alerts in the disposable Kind cluster")
    parser.add_argument("--self-monitoring", action="store_true", help="Enable chart defaults and verify Prometheus/Alertmanager self-monitoring rules and scrapes")
    parser.add_argument("--tsdb-capacity", action="store_true", help="Persist Prometheus TSDB on a test PVC and verify its real kubelet capacity alert fires and recovers")
    parser.add_argument("--csi-source", help="Absolute path to the pinned CSI hostpath test checkout")
    parser.add_argument("--monitoring-chart", help="Use a local kube-prometheus-stack chart archive instead of the Helm repository")
    parser.add_argument("--csi-attachment-failure", action="store_true",
                        help="Inject and recover a real CSI ControllerPublish failure; requires --csi-source")
    args = parser.parse_args()
    if args.controllers:
        args.workload = True
    if args.workload_timeouts:
        args.keda = True
    if args.tsdb_capacity and not args.csi_source:
        parser.error("--tsdb-capacity requires the pinned disposable CSI source via --csi-source")
    if args.csi_source:
        if not Path(args.csi_source).is_absolute():
            parser.error("CSI source path must be absolute")
        if not args.tsdb_capacity:
            args.storage = True
        from tests.e2e import csi_lab
    if args.monitoring_chart:
        try:
            monitoring_chart = validate_monitoring_chart(args.monitoring_chart)
        except ValueError as error:
            parser.error(str(error))
    else:
        monitoring_chart = "kube-prometheus-stack"
    if args.csi_attachment_failure:
        if not args.csi_source or args.tsdb_capacity:
            parser.error("--csi-attachment-failure requires --csi-source and cannot be combined with --tsdb-capacity")
        args.storage = True
        args.self_monitoring = True
    if args.confirm != "atlas-monitor-e2e" or not Path(args.kind).is_absolute():
        parser.error("A verified absolute kind path and confirmation atlas-monitor-e2e are required")
    name = "atlas-operator-" + uuid.uuid4().hex[:12]
    if name in command([args.kind, "get", "clusters"]).splitlines():
        raise RuntimeError("test cluster name already exists")
    temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="atlas-operator-test-", dir=temp_root) as directory:
        root = Path(directory)
        kubeconfig = str(root / "kubeconfig")
        forward = None
        try:
            create = [args.kind, "create", "cluster", "--name", name, "--image", "kindest/node:v1.36.1",
                      "--kubeconfig", kubeconfig, "--wait", "120s"]
            if args.csi_source:
                config_path = root / "kind-config.json"
                config_path.write_text(json.dumps({"kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4",
                                                   "nodes": [{"role": "control-plane"}, {"role": "worker"}]}))
                create += ["--config", str(config_path)]
            command(create, timeout=180)
            # Only locally cached images are used; the kind node may not share
            # the host's proxy and must not silently pull a different version.
            images = ["quay.io/prometheus-operator/prometheus-operator:v0.94.2",
                          "quay.io/prometheus-operator/prometheus-config-reloader:v0.94.2",
                          "prom/prometheus:v3.14.0"]
            if args.self_monitoring:
                images.append("prom/node-exporter:v1.9.1")
            # The shared CoreDNS replica baseline is checked in every
            # operator scenario, so kube-state-metrics is a base fixture.
            images.append("registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.17.0")
            if (args.workload or args.storage or args.longhorn_collection or args.lb or args.robot
                    or args.keda or args.dns_failure):
                images.append("python:3.13-slim")
            if args.keda:
                images.extend(("ghcr.io/kedacore/keda:2.21.0",
                               "ghcr.io/kedacore/keda-metrics-apiserver:2.21.0",
                               "ghcr.io/kedacore/keda-admission-webhooks:2.21.0"))
            if args.lb:
                images.append("haproxy:2.8-alpine")
            if args.email or args.robot or args.self_monitoring:
                images.append("prom/alertmanager:v0.28.1")
            if args.csi_source:
                images.extend(csi_lab.IMAGES.values())
                if args.csi_attachment_failure:
                    images.append(csi_lab.ATTACHER_IMAGE)
            kind_nodes = command([args.kind, "get", "nodes", "--name", name]).splitlines()
            if not kind_nodes:
                raise RuntimeError("kind did not report any nodes for the isolated cluster")
            architectures = {command(["docker", "image", "inspect", image,
                                      "--format", "{{.Architecture}}"]).strip() for image in images}
            if len(architectures) != 1 or not architectures <= {"arm64", "amd64"}:
                raise RuntimeError("cached test images must share a supported CPU architecture")
            archive = str(root / "images.tar")
            try:
                command(["docker", "image", "save", "--platform", "linux/" + architectures.pop(),
                         "-o", archive, *images], timeout=300)
                command([args.kind, "load", "image-archive", "--name", name, archive], timeout=420)
                for node in kind_nodes:
                    for image in images:
                        command(["docker", "exec", node, "crictl", "inspecti", image], timeout=30)
            finally:
                Path(archive).unlink(missing_ok=True)
            if args.tsdb_capacity:
                # Isolate the Prometheus TSDB test on its own single-node CSI
                # fixture; the standard CSI fault test keeps its worker fixture.
                csi_lab.install(["kubectl", "--kubeconfig", kubeconfig], args.csi_source,
                                name + "-control-plane")
            env = {**os.environ, "HELM_CACHE_HOME": str(root / "helm-cache"),
                   "HELM_CONFIG_HOME": str(root / "helm-config"), "HELM_DATA_HOME": str(root / "helm-data")}
            helm = ["helm", "upgrade", "--install", "stack", monitoring_chart]
            if not args.monitoring_chart:
                helm += ["--repo", "https://prometheus-community.github.io/helm-charts",
                         "--version", MONITORING_CHART_VERSION]
            helm += [
                    "--kubeconfig", kubeconfig, "--namespace", "monitoring", "--create-namespace", "--wait",
                    "--timeout", "6m", "--set", "grafana.enabled=false", "--set", "alertmanager.enabled=" + ("true" if args.email or args.robot or args.self_monitoring else "false"),
                    "--set", "kubeStateMetrics.enabled=true",
                    "--set", "nodeExporter.enabled=" + ("true" if args.self_monitoring else "false"),
                    "--set", "defaultRules.create=" + ("true" if args.self_monitoring else "false"),
                    "--set", "prometheusOperator.admissionWebhooks.enabled=false",
                    "--set", "prometheusOperator.tls.enabled=false",
                    "--set", "prometheus.prometheusSpec.image.registry=docker.io",
                    "--set", "prometheus.prometheusSpec.image.repository=prom/prometheus",
                    "--set", "prometheus.prometheusSpec.image.tag=v3.14.0",
                    "--set", "prometheus.prometheusSpec.evaluationInterval=5s",
                    "--set", "prometheus.prometheusSpec.scrapeInterval=5s"]
            if args.tsdb_capacity:
                helm += ["--set", "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.accessModes[0]=ReadWriteOnce",
                         "--set-string", "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.storageClassName=atlas-csi-e2e",
                         "--set", "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage=1Gi"]
            if args.email or args.robot or args.self_monitoring:
                helm += ["--set", "alertmanager.alertmanagerSpec.image.registry=docker.io",
                         "--set", "alertmanager.alertmanagerSpec.image.repository=prom/alertmanager",
                         "--set", "alertmanager.alertmanagerSpec.image.tag=v0.28.1",
                         "--set-string", "alertmanager.alertmanagerSpec.alertmanagerConfigMatcherStrategy.type=OnNamespaceExceptForAlertmanagerNamespace",
                         "--set-string", "alertmanager.alertmanagerSpec.alertmanagerConfigSelector.matchLabels.atlas-kubekit\\.io/managed-by=monitoring"]
            if args.self_monitoring:
                helm += ["--set", "prometheus-node-exporter.image.registry=prom",
                         "--set", "prometheus-node-exporter.image.repository=node-exporter",
                         "--set", "prometheus-node-exporter.image.tag=v1.9.1",
                         "--set", "prometheus-node-exporter.image.distroless=false"]
                monitoring_values = root / "monitoring-safe-defaults.yaml"
                monitoring_values.write_text(safe_monitoring_values("zh_CN"), encoding="utf-8")
                helm += ["--values", str(monitoring_values)]
            helm += ["--set", "kube-state-metrics.image.registry=registry.k8s.io",
                     "--set", "kube-state-metrics.image.repository=kube-state-metrics/kube-state-metrics",
                     "--set", "kube-state-metrics.image.tag=v2.17.0"]
            if args.csi_source:
                control = name + "-control-plane"
                helm += ["--set-string", "prometheus.prometheusSpec.nodeSelector.kubernetes\\.io/hostname=" + control,
                         "--set", "prometheus.prometheusSpec.tolerations[0].operator=Exists",
                         "--set-string", "prometheusOperator.nodeSelector.kubernetes\\.io/hostname=" + control,
                         "--set", "prometheusOperator.tolerations[0].operator=Exists"]
            result = subprocess.run(helm, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=420)
            if result.returncode:
                raise RuntimeError("isolated Helm installation failed: " + result.stderr.decode(errors="replace")[-4000:])
            if args.keda:
                keda = ["helm", "upgrade", "--install", "keda", "keda", "--repo",
                        "https://kedacore.github.io/charts", "--version", "2.21.0",
                        "--kubeconfig", kubeconfig, "--namespace", "keda", "--create-namespace",
                        "--wait", "--timeout", "6m", "--set", "image.pullPolicy=IfNotPresent",
                        "--set", "prometheus.operator.enabled=true",
                        "--set", "prometheus.operator.serviceMonitor.enabled=true",
                        "--set-string", "prometheus.operator.serviceMonitor.additionalLabels.release=stack"]
                result = subprocess.run(keda, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=420)
                if result.returncode:
                    raise RuntimeError("isolated KEDA installation failed: " + result.stderr.decode(errors="replace")[-4000:])
            kubectl = ["kubectl", "--kubeconfig", kubeconfig, "-n", "monitoring"]
            if args.csi_source and not args.tsdb_capacity:
                csi_lab.install(["kubectl", "--kubeconfig", kubeconfig], args.csi_source, name + "-worker",
                                enable_attach=args.csi_attachment_failure)
            resources = json.loads(command(kubectl + ["get", "prometheus", "-o", "json"]))["items"]
            if len(resources) != 1:
                raise RuntimeError("expected one isolated Prometheus resource")
            prom_name = resources[0]["metadata"]["name"]
            client = Kubernetes(kubeconfig)
            class Environment:
                def __init__(self, client):
                    self.client = client
                    self.vip_metrics = {node["name"]: {"installed": False, "active": False,
                        "enabled": False, "digest": None} for node in configuration.template()["nodes"]
                        if node["name"].startswith("lb")}
                def verify(self, config):
                    return configuration.normalize(config), {}
                def host(self, config, name):
                    environment = self
                    class TestLBHost:
                        def identity(self):
                            return {"identityState": "ok", "hostname": name,
                                    "identityFingerprint": ""}
                        def call(self, operation, parameters=None, run_id=None, **options):
                            if operation == "stats-observe":
                                return {"identity": self.identity(), "active": True,
                                        "configurationSha": "test-lb-config", "addresses": []}
                            if operation == "stats-apply":
                                return {"changed": True, "configurationSha": "test-lb-config"}
                            if operation == "lb-vip-metrics-observe":
                                return {"identity": self.identity(), "state": environment.vip_metrics[name]}
                            if operation == "lb-vip-metrics-apply":
                                state = {"installed": True, "active": True,
                                         "enabled": True, "digest": "test-lb-vip-metrics"}
                                environment.vip_metrics[name] = state
                                return {"identity": self.identity(), "state": state, "changed": True}
                            raise RuntimeError("unexpected isolated LB host action: " + operation)
                    return TestLBHost()
            environment = Environment(client)
            managers = []
            if args.email or args.robot:
                managers = json.loads(command(kubectl + ["get", "alertmanager", "-o", "json"]))['items']
                if len(managers) != 1: raise RuntimeError('expected one isolated Alertmanager')
            if args.email:
                command(kubectl + ["create", "secret", "generic", "atlas-smtp-test", "--from-literal=password=test-only-password"])
                mail_args = SimpleNamespace(lang='zh_CN', monitoring_namespace='monitoring', monitoring_release='stack',
                    monitoring_alertmanager_name=managers[0]['metadata']['name'], monitoring_alertmanager_version='0.28.1',
                    monitoring_smtp_host='smtp.example.test', monitoring_smtp_port=465,
                    monitoring_email_sender='sender@example.test', monitoring_email_recipient='receiver@example.test',
                    monitoring_email_username='sender@example.test', monitoring_email_secret='atlas-smtp-test',
                    monitoring_email_secret_key='password', monitoring_severity='warning',
                    monitoring_notification_language='zh_CN')
                values = monitoring_email.from_arguments(mail_args, configuration.template(), environment)
                command(kubectl + ['create', '--dry-run=server', '-f', '-'], data=monitoring_email._manifest(values))
                observation = monitoring_email.observe(values, environment)
                plan = plans.make('monitoring-email', client.cluster_identity(), observation, values)
                if 'test-only-password' in json.dumps(plan.record()):
                    raise RuntimeError('email credential was serialized into the change plan')
                outcome = execution.execute(plan.record(), plan.id, ClusterCoordinator(client),
                    lambda params: monitoring_email.observe(params, environment),
                    lambda params, checkpoint: monitoring_email.apply(params, environment, checkpoint, plan.id),
                    monitoring_email.parameters)
                if outcome['result']['delivery'] != 'unverified': raise RuntimeError('email delivery incorrectly claimed verified')
                inspected = inventory._email_template_status(client)
                if inspected != {'state': 'present', 'html': 'styled', 'text': 'disabled', 'routes': 1}:
                    raise RuntimeError('monitoring inspection did not identify the published styled email route: ' + repr(inspected))
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    secret = json.loads(command(kubectl + ['get', 'secret', 'alertmanager-' + managers[0]['metadata']['name'] + '-generated', '-o', 'json']))
                    import base64, gzip
                    encoded = secret.get('data', {}).get('alertmanager.yaml.gz', '')
                    config = gzip.decompress(base64.b64decode(encoded)).decode() if encoded else ''
                    if ('atlas-email' in config and 'smtp.example.test:465' in config and
                            '告警通知' in config and '处理建议' in config and
                            '{{ .Annotations.description }}' in config and
                            'html:' in config and 'background-color:#f3f4f6' in config and
                            'background-color:#348bd4' in config and
                            not re.search(r'(?m)^\s*text:\s*', config)): break
                    time.sleep(2)
                else:
                    markers = ('atlas-email', 'smtp.example.test:465', '告警通知', '处理建议',
                               '{{ .Annotations.description }}', 'html:', 'background-color:#f3f4f6',
                               'background-color:#348bd4')
                    observed = {marker: marker in config for marker in markers}
                    observed['plain_text_disabled'] = not bool(re.search(r'(?m)^\s*text:\s*', config))
                    raise RuntimeError('Operator did not preserve the managed Chinese HTML-only email template; markers=' + repr(observed))
                command(kubectl + ['apply', '-f', '-'], data={
                    'apiVersion': 'v1', 'kind': 'Secret',
                    'metadata': {'name': 'atlas-smtp-test', 'namespace': 'monitoring'},
                    'stringData': {'password': 'replacement-only-credential'}})
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    secret = json.loads(command(kubectl + ['get', 'secret',
                        'alertmanager-' + managers[0]['metadata']['name'] + '-generated', '-o', 'json']))
                    encoded = secret.get('data', {}).get('alertmanager.yaml.gz', '')
                    config = gzip.decompress(base64.b64decode(encoded)).decode() if encoded else ''
                    if ('replacement-only-credential' in config and
                            'test-only-password' not in config):
                        break
                    time.sleep(2)
                else:
                    raise RuntimeError('Operator did not rotate the generated Alertmanager credential after Secret update')
                print('06 email plan kept credentials out of plan data; Operator applied the Secret-backed credential and rotated it without retaining the previous value.')
                def create_setup_secret(name, component, receiver=None):
                    owned_labels = {"atlas-kubekit.io/managed-by": "monitoring",
                                    **notification_secrets.labels(component, receiver)}
                    command(kubectl + ['create', '-f', '-'], data={
                        'apiVersion': 'v1', 'kind': 'Secret',
                        'metadata': {'name': name, 'namespace': 'monitoring', 'labels': owned_labels},
                        'type': 'Opaque', 'data': {'password': base64.b64encode(b'test-only').decode('ascii')}})

                create_setup_secret('atlas-old-email-secret', 'route', 'atlas-email')
                create_setup_secret('atlas-still-used-secret', 'route', 'atlas-email')
                create_setup_secret('atlas-scrape-reference-secret', 'route', 'atlas-email')
                create_setup_secret('atlas-old-relay-secret', 'relay-auth')
                create_setup_secret('atlas-pending-route-secret', 'route', 'atlas-email')
                create_setup_secret('atlas-changed-route-secret', 'route', 'atlas-email')
                candidates = notification_secrets.replacement_candidates(client, 'monitoring', 'atlas-email', {
                    'atlas-old-email-secret', 'atlas-still-used-secret', 'atlas-scrape-reference-secret',
                    'atlas-old-relay-secret',
                })
                changed = notification_secrets.replacement_candidates(
                    client, 'monitoring', 'atlas-email', {'atlas-changed-route-secret'})
                command(kubectl + ['label', 'secret', 'atlas-changed-route-secret', 'test-state=updated'])
                try:
                    notification_secrets.cleanup_unreferenced(client, 'monitoring', changed)
                except OperationError:
                    command(kubectl + ['get', 'secret', 'atlas-changed-route-secret', '-o', 'name'])
                else:
                    raise RuntimeError('notification Secret cleanup accepted a changed resourceVersion')
                command(kubectl + ['create', '-f', '-'], data={
                    'apiVersion': 'v1', 'kind': 'Pod',
                    'metadata': {'name': 'atlas-secret-reference-probe', 'namespace': 'monitoring'},
                    'spec': {'containers': [{'name': 'probe', 'image': 'registry.k8s.io/pause:3.10',
                        'env': [{'name': 'TOKEN', 'valueFrom': {'secretKeyRef': {
                            'name': 'atlas-still-used-secret', 'key': 'password'}}}]}]}})
                command(kubectl + ['create', '-f', '-'], data={
                    'apiVersion': 'monitoring.coreos.com/v1', 'kind': 'ServiceMonitor',
                    'metadata': {'name': 'atlas-secret-reference-monitor', 'namespace': 'monitoring'},
                    'spec': {'selector': {'matchLabels': {'atlas-e2e': 'unused'}},
                             'endpoints': [{'port': 'http', 'bearerTokenSecret': {
                                 'name': 'atlas-scrape-reference-secret', 'key': 'password'}}]}})
                first_cleanup = notification_secrets.cleanup_unreferenced(client, 'monitoring', candidates)
                if set(first_cleanup['removed']) != {'atlas-old-email-secret'}:
                    raise RuntimeError('notification Secret cleanup did not remove only unreferenced owned credentials: ' + repr(first_cleanup))
                if set(first_cleanup['retained']) != {'atlas-still-used-secret', 'atlas-scrape-reference-secret'}:
                    raise RuntimeError('notification Secret cleanup did not retain the Pod and ServiceMonitor credentials')
                command(kubectl + ['get', 'secrets', 'atlas-pending-route-secret', 'atlas-old-relay-secret', '-o', 'name'])
                command(kubectl + ['delete', 'pod', 'atlas-secret-reference-probe', '-n', 'monitoring',
                                   '--ignore-not-found=true'])
                command(kubectl + ['wait', '--for=delete', 'pod/atlas-secret-reference-probe',
                                   '-n', 'monitoring', '--timeout=60s'])
                command(kubectl + ['delete', 'servicemonitor', 'atlas-secret-reference-monitor', '-n', 'monitoring'])
                second_cleanup = notification_secrets.cleanup_unreferenced(
                    client, 'monitoring', [item for item in candidates if item['name'] in first_cleanup['retained']])
                if set(second_cleanup['removed']) != {'atlas-still-used-secret', 'atlas-scrape-reference-secret'}:
                    raise RuntimeError('notification Secret cleanup did not remove the credential after its last reference disappeared')
                print('Notification Secret cleanup retained real Pod and ServiceMonitor credentials, then removed them after their last references disappeared.')
                print('Real Operator selected managed HTML-only email route; SMTP delivery remains unverified.')
            if args.robot:
                command(kubectl + ['create', 'secret', 'generic', 'atlas-relay-auth',
                                   '--from-literal=token=test-only-relay-token'])
                robot_config = relay_publication.alertmanager_config(
                    'monitoring', 'stack', 'atlas-e2e-wecom', 'info', 'atlas-relay-auth', 'token')
                command(kubectl + ['create', '--dry-run=server', '-f', '-'], data=robot_config)
                command(kubectl + ['create', '-f', '-'], data=robot_config)
                deadline = time.monotonic() + 90
                generated_config = ''
                while time.monotonic() < deadline:
                    secret = json.loads(command(kubectl + ['get', 'secret',
                        'alertmanager-' + managers[0]['metadata']['name'] + '-generated', '-o', 'json']))
                    import base64, gzip
                    encoded = secret.get('data', {}).get('alertmanager.yaml.gz', '')
                    generated_config = gzip.decompress(base64.b64decode(encoded)).decode() if encoded else ''
                    if ('http://127.0.0.1:18080/v1/alert' in generated_config and
                            'severity="info"' in generated_config and
                            'send_resolved: true' in generated_config):
                        break
                    time.sleep(2)
                else:
                    raise RuntimeError('Operator did not select the managed authenticated robot route')
                # The Operator compiles SecretKeySelector values into its
                # generated Kubernetes Secret, which Alertmanager reads at
                # runtime. This is the protected config Secret, not a public
                # ConfigMap, command line, or test log. The value must be
                # present here for the authenticated webhook to work.
                if 'test-only-relay-token' not in generated_config:
                    raise RuntimeError('Operator did not resolve the Secret-backed robot credential')
                print('Real Operator selected the authenticated robot route and resolved its Secret-backed credential into the generated Alertmanager Secret; delivery remains unverified.')
            if args.longhorn_collection:
                lh = ["kubectl", "--kubeconfig", kubeconfig, "-n", "longhorn-system"]
                command(["kubectl", "--kubeconfig", kubeconfig, "create", "namespace", "longhorn-system"])
                command(lh + ["create", "-f", "-"], data={"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                    "metadata": {"name": "existing-manager-isolation", "namespace": "longhorn-system"},
                    "spec": {"podSelector": {"matchLabels": {"app": "longhorn-manager"}},
                             "policyTypes": ["Ingress"], "ingress": [{}]}})
                command(lh + ["create", "-f", "-"], data={"apiVersion": "v1", "kind": "Service",
                    "metadata": {"name": "longhorn-backend", "namespace": "longhorn-system", "labels": {"app": "longhorn-manager"}},
                    "spec": {"selector": {"app": "longhorn-manager"}, "ports": [{"name": "manager", "port": 9500, "targetPort": 9500}]}})
                server = ("from http.server import BaseHTTPRequestHandler,HTTPServer\n"
                          "class H(BaseHTTPRequestHandler):\n"
                          " def do_GET(self):\n"
                          "  b=(b'atlas_longhorn_collection_test 1\\n'"
                          "+b'longhorn_disk_usage_bytes{disk=\\\"disk-e2e\\\",node=\\\"worker-e2e\\\"} 80\\n'"
                          "+b'longhorn_disk_capacity_bytes{disk=\\\"disk-e2e\\\",node=\\\"worker-e2e\\\"} 100\\n'"
                          "+b'longhorn_disk_status{condition=\\\"ready\\\",disk=\\\"disk-e2e\\\",node=\\\"worker-e2e\\\"} 1\\n'); self.send_response(200); self.send_header('Content-Type','text/plain; version=0.0.4'); self.end_headers(); self.wfile.write(b)\n"
                          " def log_message(self,*args): pass\n"
                          "HTTPServer(('0.0.0.0',9500),H).serve_forever()\n")
                command(lh + ["create", "-f", "-"], data={"apiVersion": "v1", "kind": "Pod",
                    "metadata": {"name": "longhorn-metrics-test", "namespace": "longhorn-system", "labels": {"app": "longhorn-manager"}},
                    "spec": {"containers": [{"name": "metrics", "image": "python:3.13-slim", "command": ["python", "-u", "-c", server],
                                              "ports": [{"containerPort": 9500}]}]}})
                collection_args = SimpleNamespace(monitoring_namespace="monitoring", monitoring_release="stack",
                    monitoring_prometheus_name=prom_name, monitoring_longhorn_namespace="longhorn-system")
                values = monitoring_longhorn_collection.from_arguments(collection_args, configuration.template(), environment)
                observation = monitoring_longhorn_collection.observe(values, environment)
                plan = plans.make("monitoring-longhorn-collection", client.cluster_identity(), observation, values)
                outcome = execution.execute(plan.record(), plan.id, ClusterCoordinator(client),
                    lambda params: monitoring_longhorn_collection.observe(params, environment),
                    lambda params, checkpoint: monitoring_longhorn_collection.apply(params, environment, checkpoint, plan.id),
                    monitoring_longhorn_collection.parameters)
                if outcome["result"]["evaluation"] != "unverified":
                    raise RuntimeError("collection publication incorrectly claimed verification")
                disk_args = SimpleNamespace(lang="zh_CN", monitoring_notification_language="zh_CN",
                    monitoring_namespace="monitoring", monitoring_release="stack",
                    monitoring_prometheus_name=prom_name, monitoring_longhorn_disk_used_percent=70,
                    monitoring_hold_seconds=1, monitoring_max_age=120, monitoring_severity="warning")
                disk_values = monitoring_longhorn_disk.from_arguments(disk_args, configuration.template(), environment)
                disk_observation = monitoring_longhorn_disk.observe(disk_values, environment)
                disk_plan = plans.make("monitoring-longhorn-disk-rules", client.cluster_identity(), disk_observation, disk_values)
                disk_outcome = execution.execute(disk_plan.record(), disk_plan.id, ClusterCoordinator(client),
                    lambda params: monitoring_longhorn_disk.observe(params, environment),
                    lambda params, checkpoint: monitoring_longhorn_disk.apply(params, environment, checkpoint, disk_plan.id),
                    monitoring_longhorn_disk.parameters)
                if disk_outcome["result"]["state"] != "resource-written":
                    raise RuntimeError("Longhorn disk rules publication was not confirmed")
            if args.lb:
                haproxy_config = '''global
    log stdout format raw local0
defaults
    mode http
    timeout connect 2s
    timeout client 10s
    timeout server 10s
frontend prometheus-metrics
    bind *:8405
    acl metrics_source_allowed src 10.0.0.0/8
    http-request deny if !metrics_source_allowed
    mode http
    option socket-stats
    http-request use-service prometheus-exporter if { path /metrics }
    no log
frontend test-app
    bind *:8080
    default_backend test-app-nodes
backend test-app-nodes
    server unavailable 127.0.0.1:1 check
'''
                command(kubectl + ["create", "configmap", "atlas-haproxy-test", "--from-literal=haproxy.cfg=" + haproxy_config])
                command(kubectl + ["create", "-f", "-"], data={"apiVersion": "v1", "kind": "Pod",
                    "metadata": {"name": "atlas-haproxy-test", "namespace": "monitoring"},
                    "spec": {"containers": [{"name": "haproxy", "image": "haproxy:2.8-alpine",
                        "imagePullPolicy": "Never", "command": ["haproxy", "-W", "-db", "-f", "/etc/haproxy/haproxy.cfg"],
                        "ports": [{"name": "metrics", "containerPort": 8405}],
                        "volumeMounts": [{"name": "config", "mountPath": "/etc/haproxy", "readOnly": True}]}],
                        "volumes": [{"name": "config", "configMap": {"name": "atlas-haproxy-test"}}]}})
                command(kubectl + ["wait", "--for=condition=Ready", "pod/atlas-haproxy-test", "--timeout=90s"])
                lb_ip = json.loads(command(kubectl + ["get", "pod", "atlas-haproxy-test", "-o", "json"]))["status"]["podIP"]
                lb_config = configuration.template()
                # The monitored endpoint is a disposable Pod outside the
                # synthetic cluster inventory's declared Pod CIDR. Keep the
                # config invariant (LB node addresses cannot be Pod IPs) true.
                lb_config["cluster"]["podCIDR"] = "10.240.0.0/16"
                lb_config["cluster"]["statsAllowedCIDR"] = "10.0.0.0/8"
                lb_config["nodes"][0]["address"] = lb_ip
                lb_args = SimpleNamespace(lang="zh_CN", monitoring_namespace="monitoring", monitoring_release="stack",
                    monitoring_prometheus_name=prom_name, monitoring_hold_seconds=1, monitoring_max_age=30,
                    monitoring_severity="warning", monitoring_lb_capacity_percent=80,
                    monitoring_lb_connection_error_threshold=5, monitoring_lb_drift_threshold=2,
                    stats_cidr="10.0.0.0/8")
                lb_values = monitoring_lb.from_arguments(lb_args, lb_config, environment)
                lb_observation = monitoring_lb.observe(lb_values, environment)
                lb_plan = plans.make("monitoring-lb", client.cluster_identity(), lb_observation, lb_values)
                lb_result = execution.execute(lb_plan.record(), lb_plan.id, ClusterCoordinator(client),
                    lambda values: monitoring_lb.observe(values, environment),
                    lambda values, checkpoint: monitoring_lb.apply(values, environment, checkpoint, lb_plan.id),
                    monitoring_lb.parameters)
                if lb_result["result"]["state"] != "resources-written":
                    raise RuntimeError("LB facade did not complete both publication phases")
            operation_args = SimpleNamespace(lang="en", monitoring_namespace="monitoring", monitoring_release="stack",
                monitoring_prometheus_name=prom_name, monitoring_hold_seconds=1, monitoring_max_age=120,
                monitoring_severity="warning", monitoring_target=["synthetic=missing"])
            cli_config = root / "monitoring-cli-config.json"
            cli_config.write_text(json.dumps(configuration.template()), encoding="utf-8")
            cli_output = root / "monitoring-cli-output"
            cli_arguments = ["--lang", "zh_CN", "--kubeconfig", kubeconfig,
                "--config", str(cli_config), "--output-dir", str(cli_output),
                "--monitoring-namespace", "monitoring", "--monitoring-release", "stack",
                "--monitoring-prometheus-name", prom_name, "--monitoring-hold-seconds", "1",
                "--monitoring-max-age", "120", "--monitoring-target", "synthetic=missing"]

            def run_monitoring_cli(action, *extra):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    status = operations_cli.main([*cli_arguments, action, *extra])
                return status, stdout.getvalue() + stderr.getvalue()

            resource_cli_arguments = ["--lang", "zh_CN", "--kubeconfig", kubeconfig,
                "--config", str(cli_config), "--output-dir", str(cli_output),
                "--monitoring-resource-kind", "prometheusrule",
                "--monitoring-resource-namespace", "monitoring"]

            def run_resource_cli(action, *extra):
                stdout, stderr = io.StringIO(), io.StringIO()
                original_perform = operations_cli.perform

                def trace_failure(args, translate, run_context):
                    try:
                        return original_perform(args, translate, run_context)
                    except BaseException:
                        traceback.print_exc()
                        raise

                with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                     patch("scripts.operations.cli.perform", side_effect=trace_failure), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    status = operations_cli.main([*resource_cli_arguments, action, *extra])
                return status, stdout.getvalue() + stderr.getvalue()

            def plan_resource_removal(name):
                before = {path for path in (cli_output / "plans").glob("*.json")}
                status, output = run_resource_cli("--change-plan", "--operation", "monitoring-resource-remove",
                    "--monitoring-resource-name", name)
                if status != 0:
                    raise RuntimeError("06 CLI could not plan owned-resource removal: " + output[-2000:])
                created = [path for path in (cli_output / "plans").glob("*.json") if path not in before]
                if len(created) != 1:
                    raise RuntimeError("06 CLI did not create exactly one resource-removal plan")
                return created[0], json.loads(created[0].read_text(encoding="utf-8"))

            plan_status, plan_output = run_monitoring_cli("--change-plan", "--operation",
                                                         "monitoring-scrape-rules")
            if plan_status != 0:
                raise RuntimeError("06 CLI could not create a real-cluster monitoring plan: " + plan_output[-2000:])
            plan_file = next((cli_output / "plans").glob("*.json"))
            plan_record = json.loads(plan_file.read_text(encoding="utf-8"))
            plan_id = plan_record["id"]
            if plan_record["capability"] != "monitoring-scrape-rules":
                raise RuntimeError("06 CLI wrote an unexpected capability plan")

            denied_status, _ = run_monitoring_cli("--change-apply", "--plan-file", str(plan_file),
                                                   "--confirm", "incorrect-confirmation")
            if denied_status == 0 or client.get("prometheusrules.monitoring.coreos.com",
                                                 "atlas-scrape-targets", "monitoring", optional=True):
                raise RuntimeError("06 CLI accepted an incorrect confirmation or wrote a rule")

            capability = CAPABILITIES["monitoring-scrape-rules"]
            original_observe = capability.observe
            conflict_injected = False
            foreign_rule = {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
                "metadata": {"name": "atlas-scrape-targets", "namespace": "monitoring",
                             "labels": {"release": "stack"}},
                "spec": {"groups": [{"name": "atlas.e2e.foreign", "rules": [
                    {"alert": "AtlasE2EForeignOwnershipFixture", "expr": "vector(0)"}]}]}}

            def inject_conflict_after_fresh_observation(values, live_environment):
                nonlocal conflict_injected
                observed = original_observe(values, live_environment)
                if not conflict_injected:
                    command(kubectl + ["create", "-f", "-"], data=foreign_rule)
                    conflict_injected = True
                return observed

            with patch.object(capability, "observe", side_effect=inject_conflict_after_fresh_observation):
                failed_status, failed_output = run_monitoring_cli(
                    "--change-apply", "--plan-file", str(plan_file), "--confirm", plan_id)
            if failed_status == 0 or not conflict_injected:
                raise RuntimeError("06 CLI did not surface the injected apply-time ownership conflict: " + failed_output[-2000:])
            failed_record = client.get("configmap", "atlas-op-" + plan_id[:32], "kube-system")
            journal = json.loads(failed_record.get("data", {}).get("record", "{}"))
            if journal.get("state") != "failed" or journal.get("capability") != "monitoring-scrape-rules":
                raise RuntimeError("06 CLI did not persist the failed capability in the real cluster journal")

            locked = client.get("configmap", "atlas-kubekit-operation-lock", "kube-system")
            lock_owner = locked.get("data", {}).get("owner")
            if not lock_owner:
                raise RuntimeError("apply failure did not retain the owned lock for explicit recovery")
            command(kubectl + ["delete", "prometheusrule", "atlas-scrape-targets", "-n", "monitoring",
                               "--wait=true"])
            recovery_status, recovery_output = run_monitoring_cli("--lock-recover", "--lock-owner", lock_owner,
                "--resource-version", locked["metadata"]["resourceVersion"], "--confirm",
                "OLD COORDINATOR AND REMOTE JOBS STOPPED")
            if recovery_status != 0:
                raise RuntimeError("06 CLI could not recover the exact failed-plan lock: " + recovery_output[-2000:])

            continue_status, continue_output = run_monitoring_cli("--change-continue", "--plan-file", str(plan_file))
            if continue_status != 0:
                raise RuntimeError("06 CLI could not replan failed intent from current state: " + continue_output[-2000:])
            plan_files = sorted((cli_output / "plans").glob("*.json"))
            continued_files = [path for path in plan_files if path != plan_file]
            if len(continued_files) != 1:
                raise RuntimeError("06 CLI continuation did not write exactly one fresh plan")
            continued_file = continued_files[0]
            continued_record = json.loads(continued_file.read_text(encoding="utf-8"))
            continued_id = continued_record["id"]
            if continued_id == plan_id:
                raise RuntimeError("06 CLI reused the failed plan ID instead of creating a fresh plan")

            apply_status, apply_output = run_monitoring_cli("--change-apply", "--plan-file", str(continued_file),
                                                             "--confirm", continued_id)
            if apply_status != 2 or "操作状态：已完成" not in apply_output or "仍未验证" not in apply_output:
                raise RuntimeError("06 CLI did not apply the fresh continuation with an unverified evaluation boundary: " + apply_output[-2000:])
            status_status, status_output = run_monitoring_cli("--change-status", "--plan-id", continued_id)
            if status_status != 2:
                raise RuntimeError("06 CLI could not read the continued plan journal: " + status_output[-2000:])
            replay_status, replay_output = run_monitoring_cli("--change-apply", "--plan-file", str(continued_file),
                                                               "--confirm", continued_id)
            if replay_status != 2 or "先前已完成，本次未重复执行" not in replay_output:
                raise RuntimeError("06 CLI continued-plan replay failed: " + replay_output[-2000:])
            published = client.get("prometheusrules.monitoring.coreos.com", "atlas-scrape-targets",
                                   "monitoring", optional=True)
            if (not published or published.get("metadata", {}).get("labels", {}).get("release") != "stack" or
                    published.get("metadata", {}).get("labels", {}).get("atlas-kubekit.io/managed-by") != "monitoring"):
                raise RuntimeError("06 CLI continuation did not publish the expected owned PrometheusRule")
            if "仍未验证" not in status_output:
                raise RuntimeError("06 CLI status output incorrectly omitted the evaluation boundary")
            lock_owner = uuid.uuid4().hex
            locked = client.get("configmap", "atlas-kubekit-operation-lock", "kube-system")
            if locked.get("metadata", {}).get("labels", {}).get("atlas-kubekit.io/managed-by") != "operations":
                raise RuntimeError("06 CLI did not leave the expected owned lock record")
            locked["data"] = {"owner": lock_owner, "planId": "atlas-e2e-lock", "startedAt": "test"}
            locked = client.replace(locked)
            if locked is None:
                raise RuntimeError("could not create the owned active-lock fixture with resourceVersion fencing")
            if locked.get("data", {}).get("owner") != lock_owner:
                raise RuntimeError("test did not establish the owned stale-lock fixture")
            lock_status, _ = run_monitoring_cli("--lock-status")
            if lock_status != 2:
                raise RuntimeError("06 CLI did not report the active cluster lock")
            recovery_status, recovery_output = run_monitoring_cli("--lock-recover", "--lock-owner", lock_owner,
                "--resource-version", locked["metadata"]["resourceVersion"], "--confirm",
                "OLD COORDINATOR AND REMOTE JOBS STOPPED")
            recovered_lock = client.get("configmap", "atlas-kubekit-operation-lock", "kube-system")
            if recovery_status != 0 or recovered_lock.get("data", {}).get("owner"):
                raise RuntimeError("06 CLI could not safely recover the exact owned lock: " + recovery_output[-2000:])

            removal_spec = {"groups": [{"name": "atlas.e2e.remove", "rules": [
                {"alert": "AtlasE2ERemovalFixture", "expr": "vector(1)"}]}]}

            def create_owned_removal_fixture(name, owned=True):
                labels = {"release": "stack"}
                annotations = {}
                if owned:
                    labels["atlas-kubekit.io/managed-by"] = "monitoring"
                    annotations["atlas-kubekit.io/spec-sha256"] = fingerprint(removal_spec)
                fixture = {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
                    "metadata": {"name": name, "namespace": "monitoring", "labels": labels,
                                 "annotations": annotations}, "spec": removal_spec}
                command(kubectl + ["create", "-f", "-"], data=fixture)

            removal_name = "atlas-e2e-remove-managed"
            create_owned_removal_fixture(removal_name)
            removal_plan_file, removal_plan = plan_resource_removal(removal_name)
            removal_id = removal_plan["id"]
            if removal_plan.get("capability") != "monitoring-resource-remove":
                raise RuntimeError("06 CLI wrote an unexpected resource-removal capability plan")
            denied_status, _ = run_resource_cli("--change-apply", "--plan-file", str(removal_plan_file),
                                                "--confirm", "incorrect-confirmation")
            if denied_status == 0 or not client.get("prometheusrules.monitoring.coreos.com", removal_name,
                                                     "monitoring", optional=True):
                raise RuntimeError("06 CLI accepted an incorrect removal confirmation or deleted the resource")
            removed_status, removed_output = run_resource_cli("--change-apply", "--plan-file",
                str(removal_plan_file), "--confirm", removal_id)
            if removed_status != 0 or client.get("prometheusrules.monitoring.coreos.com", removal_name,
                                                 "monitoring", optional=True):
                raise RuntimeError("06 CLI did not safely remove the confirmed Atlas-owned resource: " + removed_output[-2000:])
            replay_status, replay_output = run_resource_cli("--change-apply", "--plan-file",
                str(removal_plan_file), "--confirm", removal_id)
            if replay_status != 0 or "先前已完成，本次未重复执行" not in replay_output:
                raise RuntimeError("06 CLI removal replay was not idempotent: " + replay_output[-2000:])
            status_status, _ = run_resource_cli("--change-status", "--plan-id", removal_id)
            if status_status != 0:
                raise RuntimeError("06 CLI could not report the completed resource-removal journal")

            stale_name = "atlas-e2e-remove-drift"
            create_owned_removal_fixture(stale_name)
            stale_plan_file, stale_plan = plan_resource_removal(stale_name)
            command(kubectl + ["label", "prometheusrule", stale_name, "atlas-e2e-drift=changed",
                               "--overwrite", "-n", "monitoring"])
            stale_status, stale_output = run_resource_cli("--change-apply", "--plan-file", str(stale_plan_file),
                "--confirm", stale_plan["id"])
            if stale_status == 0 or not client.get("prometheusrules.monitoring.coreos.com", stale_name,
                                                   "monitoring", optional=True):
                raise RuntimeError("06 CLI did not reject drift or deleted a changed resource: " + stale_output[-2000:])

            race_name = "atlas-e2e-remove-race"
            create_owned_removal_fixture(race_name)
            race_plan_file, race_plan = plan_resource_removal(race_name)
            original_delete = client.delete_monitoring_resource

            def race_before_api_delete(*args):
                command(kubectl + ["label", "prometheusrule", race_name, "atlas-e2e-race=changed",
                                   "--overwrite", "-n", "monitoring"])
                return original_delete(*args)

            with patch.object(client, "delete_monitoring_resource", side_effect=race_before_api_delete):
                race_status, race_output = run_resource_cli("--change-apply", "--plan-file",
                    str(race_plan_file), "--confirm", race_plan["id"])
            raced_resource = client.get("prometheusrules.monitoring.coreos.com", race_name,
                                        "monitoring", optional=True)
            raced_lock = client.get("configmap", "atlas-kubekit-operation-lock", "kube-system")
            raced_journal = client.get("configmap", "atlas-op-" + race_plan["id"][:32], "kube-system")
            if (race_status == 0 or raced_resource is None or
                    json.loads(raced_journal.get("data", {}).get("record", "{}")).get("state") != "failed"):
                raise RuntimeError("Kubernetes API did not reject a write-time resourceVersion race: " + race_output[-2000:])
            race_recovery_status, race_recovery_output = run_resource_cli("--lock-recover",
                "--lock-owner", raced_lock.get("data", {}).get("owner", ""),
                "--resource-version", raced_lock["metadata"]["resourceVersion"],
                "--confirm", "OLD COORDINATOR AND REMOTE JOBS STOPPED")
            if race_recovery_status != 0:
                raise RuntimeError("06 CLI could not recover the resource-version race lock: " + race_recovery_output[-2000:])
            command(kubectl + ["delete", "prometheusrule", race_name, "-n", "monitoring", "--wait=true"])

            foreign_name = "atlas-e2e-remove-foreign"
            create_owned_removal_fixture(foreign_name, owned=False)
            foreign_status, _ = run_resource_cli("--change-plan", "--operation", "monitoring-resource-remove",
                "--monitoring-resource-name", foreign_name)
            if foreign_status == 0 or not client.get("prometheusrules.monitoring.coreos.com", foreign_name,
                                                      "monitoring", optional=True):
                raise RuntimeError("06 CLI accepted or deleted a foreign-owned monitoring resource")

            print("Real Kubernetes 06 CLI lifecycle passed: rule publication, ownership conflict/failure recovery/continuation, safe monitoring-resource deletion, wrong-confirmation denial, API UID/resourceVersion fencing including a write-time race, drift and foreign-owner rejection, status and idempotent replay.")
            control_plane_args = SimpleNamespace(lang="zh_CN", monitoring_namespace="monitoring",
                monitoring_release="stack", monitoring_prometheus_name=prom_name,
                monitoring_hold_seconds=1, monitoring_max_age=120, monitoring_severity="warning",
                monitoring_target=["apiserver=unavailable-control-plane:6443"])
            cp_params = monitoring_control_plane.from_arguments(control_plane_args, configuration.template(), environment)
            cp_observation = monitoring_control_plane.observe(cp_params, environment)
            cp_plan = plans.make("monitoring-control-plane-rules", client.cluster_identity(), cp_observation, cp_params)
            cp_outcome = execution.execute(cp_plan.record(), cp_plan.id, ClusterCoordinator(client),
                lambda values: monitoring_control_plane.observe(values, environment),
                lambda values, checkpoint: monitoring_control_plane.apply(values, environment, checkpoint, cp_plan.id),
                monitoring_control_plane.parameters)
            if cp_outcome["result"]["evaluation"] != "unverified":
                raise RuntimeError("control-plane publication incorrectly claimed evaluation")
            dns_args = SimpleNamespace(lang="zh_CN", monitoring_namespace="monitoring", monitoring_release="stack",
                monitoring_prometheus_name=prom_name, monitoring_hold_seconds=1, monitoring_max_age=120,
                monitoring_severity="warning", monitoring_target=["coredns=missing-dns:9153"],
                monitoring_dns_error_percent=5, monitoring_dns_window_seconds=300,
                monitoring_dns_latency_ms=100, monitoring_dns_min_requests=1,
                monitoring_dns_deployment="kube-system/coredns",
                monitoring_dns_min_replicas=1)
            dns_params = monitoring_dns.from_arguments(dns_args, configuration.template(), environment)
            dns_observation = monitoring_dns.observe(dns_params, environment)
            dns_plan = plans.make("monitoring-dns-rules", client.cluster_identity(), dns_observation, dns_params)
            dns_outcome = execution.execute(dns_plan.record(), dns_plan.id, ClusterCoordinator(client),
                lambda values: monitoring_dns.observe(values, environment),
                lambda values, checkpoint: monitoring_dns.apply(values, environment, checkpoint, dns_plan.id),
                monitoring_dns.parameters)
            if dns_outcome["result"]["evaluation"] != "unverified":
                raise RuntimeError("CoreDNS publication incorrectly claimed evaluation")
            certificate_args = SimpleNamespace(lang="zh_CN", monitoring_notification_language="zh_CN",
                monitoring_namespace="monitoring", monitoring_release="stack",
                monitoring_prometheus_name=prom_name, monitoring_hold_seconds=1,
                monitoring_max_age=30, monitoring_severity="warning",
                monitoring_certificate=["monitoring/atlas-e2e-cert"],
                monitoring_certificate_expiry_days=30)
            certificate_params = monitoring_certificates.from_arguments(
                certificate_args, configuration.template(), environment)
            certificate_observation = monitoring_certificates.observe(certificate_params, environment)
            certificate_plan = plans.make("monitoring-certificate-rules", client.cluster_identity(),
                                          certificate_observation, certificate_params)
            certificate_outcome = execution.execute(certificate_plan.record(), certificate_plan.id,
                ClusterCoordinator(client),
                lambda values: monitoring_certificates.observe(values, environment),
                lambda values, checkpoint: monitoring_certificates.apply(values, environment, checkpoint,
                                                                          certificate_plan.id),
                monitoring_certificates.parameters)
            if certificate_outcome["result"]["evaluation"] != "unverified":
                raise RuntimeError("certificate rule publication incorrectly claimed evaluation")
            if args.workload:
                workload = {"apiVersion": "apps/v1", "kind": "Deployment",
                    "metadata": {"name": "atlas-workload-e2e", "namespace": "monitoring"},
                    "spec": {"replicas": 1, "progressDeadlineSeconds": 10,
                        "strategy": {"type": "Recreate"},
                        "selector": {"matchLabels": {"app": "atlas-workload-e2e"}},
                        "template": {"metadata": {"labels": {"app": "atlas-workload-e2e"}},
                            "spec": {"containers": [{"name": "app", "image": "python:3.13-slim",
                                                    "command": ["sleep", "3600"]}]}}}}
                command(kubectl + ["create", "-f", "-"], data=workload)
                command(kubectl + ["rollout", "status", "deployment/atlas-workload-e2e", "--timeout=90s"])
                workload_args = SimpleNamespace(lang="en", monitoring_namespace="monitoring", monitoring_release="stack",
                    monitoring_prometheus_name=prom_name, monitoring_hold_seconds=1, monitoring_max_age=30,
                    monitoring_severity="warning", monitoring_deployment=["monitoring/atlas-workload-e2e"])
                params = monitoring_workloads.from_arguments(workload_args, configuration.template(), environment)
                observation = monitoring_workloads.observe(params, environment)
                plan = plans.make("monitoring-workload-rules", client.cluster_identity(), observation, params)
                outcome = execution.execute(plan.record(), plan.id, ClusterCoordinator(client),
                    lambda values: monitoring_workloads.observe(values, environment),
                    lambda values, checkpoint: monitoring_workloads.apply(values, environment, checkpoint, plan.id),
                    monitoring_workloads.parameters)
                if outcome["result"]["evaluation"] != "unverified":
                    raise RuntimeError("workload publication incorrectly claimed evaluation")
            if args.storage:
                if not args.csi_source:
                    command(kubectl + ["create", "-f", "-"], data={"apiVersion": "v1", "kind": "PersistentVolume",
                        "metadata": {"name": "atlas-pvc-e2e"}, "spec": {"capacity": {"storage": "1Gi"},
                        "accessModes": ["ReadWriteOnce"], "storageClassName": "",
                        "persistentVolumeReclaimPolicy": "Retain", "hostPath": {"path": "/var/local/atlas-pvc-e2e", "type": "DirectoryOrCreate"}}})
                claim_spec = {"accessModes": ["ReadWriteOnce"], "storageClassName": "atlas-csi-e2e" if args.csi_source else "",
                              "resources": {"requests": {"storage": "1Gi"}}}
                if not args.csi_source:
                    claim_spec["volumeName"] = "atlas-pvc-e2e"
                command(kubectl + ["create", "-f", "-"], data={"apiVersion": "v1", "kind": "PersistentVolumeClaim",
                    "metadata": {"name": "atlas-pvc-e2e", "namespace": "monitoring"},
                    "spec": claim_spec})
                pod = {"apiVersion": "v1", "kind": "Pod",
                    "metadata": {"name": "atlas-pvc-e2e", "namespace": "monitoring"}, "spec": {
                    "containers": [{"name": "app", "image": "python:3.13-slim", "command": ["sleep", "3600"],
                                    "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                    "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "atlas-pvc-e2e"}}]}}
                if args.csi_source:
                    pod["spec"]["nodeSelector"] = {"kubernetes.io/hostname": name + "-worker"}
                command(kubectl + ["create", "-f", "-"], data=pod)
                command(kubectl + ["wait", "--for=condition=Ready", "pod/atlas-pvc-e2e", "--timeout=90s"])
            command(kubectl + ["wait", "--for=condition=Ready", "pod", "-l", "prometheus=" + prom_name,
                               "--timeout=180s"], timeout=200)
            pods = json.loads(command(kubectl + ["get", "pod", "-l", "prometheus=" + prom_name, "-o", "json"]))["items"]
            if len(pods) != 1:
                raise RuntimeError("expected one isolated Prometheus pod")
            if args.csi_source and pods[0].get("spec", {}).get("nodeName") != name + "-control-plane":
                raise RuntimeError("Prometheus is not isolated from the test worker")
            tsdb_claim = None
            if args.tsdb_capacity:
                claims = json.loads(command(kubectl + ["get", "pvc", "-o", "json"]))["items"]
                mounted_claims = [volume.get("persistentVolumeClaim", {}).get("claimName")
                                  for volume in pods[0].get("spec", {}).get("volumes", [])]
                matches = [claim for claim in claims if claim.get("metadata", {}).get("name") in mounted_claims]
                if len(matches) != 1 or matches[0].get("status", {}).get("phase") != "Bound":
                    raise RuntimeError("Prometheus TSDB PVC did not bind to the isolated capacity-test volume")
                tsdb_claim = matches[0]["metadata"]["name"]
                pod_claims = mounted_claims
                if tsdb_claim not in pod_claims:
                    raise RuntimeError("Prometheus is not mounting the registered TSDB PVC")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            forward = subprocess.Popen(kubectl + ["port-forward", "pod/" + pods[0]["metadata"]["name"],
                                                   str(port) + ":9090"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 90
            replica_rule_observed = []
            while True:
                try:
                    with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/rules", timeout=3) as response:
                        document = json.load(response)
                    groups = document["data"]["groups"]
                    scrape_firing = any(rule.get("name") == "AtlasScrapeTargetOrTelemetryMissing" and
                        rule.get("state") == "firing" and rule.get("health") == "ok"
                        for group in groups for rule in group["rules"])
                    cp_firing = any(rule.get("name") == "AtlasControlPlaneTargetOrTelemetryMissing" and
                        rule.get("state") == "firing" and rule.get("health") == "ok"
                        for group in groups for rule in group["rules"])
                    dns_firing = any(rule.get("name") == "AtlasCoreDNSTargetOrTelemetryMissing" and
                        rule.get("state") == "firing" and rule.get("health") == "ok"
                        for group in groups for rule in group["rules"])
                    dns_replica_minimum_loaded = any(
                        rule.get("name") == "AtlasCoreDNSReplicasBelowMinimum" and
                        rule.get("health") == "ok" and rule.get("state") == "inactive" and
                        rule.get("labels", {}).get("minimum_replicas") == "1"
                        for group in groups for rule in group["rules"])
                    certificate_rules_healthy = all(any(
                        rule.get("name") == alert and rule.get("health") == "ok"
                        for group in groups for rule in group["rules"])
                        for alert in ("AtlasCertificateExpiringSoon", "AtlasCertificateNotReady",
                                      "AtlasCertificateTelemetryMissing"))
                    with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/alerts", timeout=3) as response:
                        active_alerts = json.load(response)["data"]["alerts"]
                    certificate_missing_firing = any(
                        item.get("labels", {}).get("alertname") == "AtlasCertificateTelemetryMissing" and
                        item.get("labels", {}).get("name") == "atlas-e2e-cert" and
                        item.get("state") == "firing" for item in active_alerts)
                    replica_rule_observed = [{key: rule.get(key) for key in ("name", "state", "health", "labels", "lastError")}
                        for group in groups for rule in group["rules"]
                        if rule.get("name") == "AtlasCoreDNSReplicasBelowMinimum"]
                    lb_backend_firing = any(rule.get("name") == "AtlasLBBackendUnavailableOrMetricsMissing" and
                        rule.get("state") == "firing" and rule.get("health") == "ok"
                        for group in groups for rule in group["rules"])
                    if args.tsdb_capacity:
                        baseline_ready = dns_replica_minimum_loaded and certificate_rules_healthy
                    else:
                        baseline_ready = (scrape_firing and cp_firing and dns_firing and
                                          certificate_missing_firing and (not args.lb or lb_backend_firing))
                    if baseline_ready and dns_replica_minimum_loaded and certificate_rules_healthy:
                        break
                except (OSError, ValueError, KeyError):
                    pass
                if forward.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Prometheus did not load and fire the published rule; CoreDNS replica rule=" +
                                       repr(replica_rule_observed))
                time.sleep(2)
            if args.tsdb_capacity:
                print("Real Prometheus Operator loaded the controlled CoreDNS baseline and certificate rules for the isolated TSDB PVC test.")
            else:
                print("Real Prometheus Operator selected scrape-target, control-plane, CoreDNS and certificate rules; missing certificate telemetry alerted while the healthy CoreDNS replica rule remained inactive.")
            if args.self_monitoring:
                expected_self_rules = {"PrometheusNotIngestingSamples", "PrometheusRuleFailures",
                                       "PrometheusMissingRuleEvaluations", "PrometheusNotConnectedToAlertmanagers",
                                       "AlertmanagerFailedReload", "AlertmanagerClusterDown"}
                expected_node_rules = {"NodeCPUHighUsage", "NodeMemoryHighUtilization",
                                       "NodeFilesystemSpaceFillingUp", "NodeFilesystemAlmostOutOfSpace",
                                       "NodeFilesystemFilesFillingUp", "NodeDiskIOSaturation",
                                       "NodeFileDescriptorLimit", "NodeClockSkewDetected",
                                       "NodeClockNotSynchronising", "AtlasAlertmanagerUnavailableOrTelemetryMissing",
                                       "AtlasCSIVolumeAttachmentNotReady",
                                       "AtlasCSIVolumeAttachmentTelemetryMissing",
                                       "AtlasKubeletServiceUnavailable", "AtlasKubeletSystemdTelemetryMissing",
                                       "AtlasContainerdServiceUnavailable", "AtlasContainerdSystemdTelemetryMissing"}
                deadline = time.monotonic() + 180
                while True:
                    try:
                        with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/rules", timeout=3) as response:
                            groups = json.load(response)["data"]["groups"]
                        selected = {rule.get("name"): rule for group in groups for rule in group["rules"]
                                    if rule.get("name") in expected_self_rules | expected_node_rules}
                        if (expected_self_rules <= set(selected) and
                                expected_node_rules <= set(selected) and
                                all(rule.get("health") == "ok" for rule in selected.values())):
                            break
                    except (OSError, ValueError, KeyError, IndexError, TypeError):
                        pass
                    if forward.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("kube-prometheus-stack self-monitoring rules did not load healthy; missing=" +
                                           repr(sorted((expected_self_rules | expected_node_rules) - set(selected))) +
                                           ", unhealthy=" + repr(sorted(name for name, rule in selected.items()
                                                                          if rule.get("health") != "ok")))
                    time.sleep(2)
                for metric in ("node_cpu_seconds_total", "node_memory_MemAvailable_bytes",
                               "node_filesystem_avail_bytes", "node_filesystem_files_free"):
                    query = urlencode({"query": "count(" + metric + ")"})
                    deadline = time.monotonic() + 120
                    while True:
                        try:
                            with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/query?" + query,
                                         timeout=3) as response:
                                result = json.load(response)["data"]["result"]
                            if result and float(result[0]["value"][1]) > 0:
                                break
                        except (OSError, ValueError, KeyError, IndexError, TypeError):
                            pass
                        if forward.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("node-exporter metric was not scraped: " + metric)
                        time.sleep(2)
                exporter = json.loads(command(kubectl + ["get", "daemonset", "-o", "json"]))["items"]
                exporter = [item for item in exporter if item.get("metadata", {}).get("labels", {}).get(
                    "app.kubernetes.io/name") == "prometheus-node-exporter"]
                if len(exporter) != 1:
                    raise RuntimeError("expected one node-exporter DaemonSet with Atlas textfile values")
                exporter_spec = exporter[0]["spec"]["template"]["spec"]
                exporter_container = exporter_spec["containers"][0]
                if ("--collector.textfile.directory=/host-textfile" not in exporter_container.get("args", []) or
                        not any(mount.get("mountPath") == "/host-textfile" and mount.get("readOnly") is True
                                for mount in exporter_container.get("volumeMounts", [])) or
                        not any(volume.get("hostPath", {}).get("path") ==
                                "/var/lib/atlas-kubekit/node-exporter-textfile"
                                and volume.get("hostPath", {}).get("type") == "DirectoryOrCreate"
                                for volume in exporter_spec.get("volumes", []))):
                    raise RuntimeError("node-exporter did not receive the read-only Atlas textfile directory")

                def push_systemd_fixture(kubelet_active, age_seconds=0):
                    metric_file = root / "atlas_systemd_units.prom"
                    metric_file.write_text(
                        "# HELP atlas_systemd_unit_probe_success Whether systemd state was read successfully.\n"
                        "# TYPE atlas_systemd_unit_probe_success gauge\n"
                        "# HELP atlas_systemd_unit_active Whether the systemd unit is active.\n"
                        "# TYPE atlas_systemd_unit_active gauge\n"
                        'atlas_systemd_unit_probe_success{unit="kubelet"} 1\n'
                        f'atlas_systemd_unit_active{{unit="kubelet"}} {int(kubelet_active)}\n'
                        'atlas_systemd_unit_probe_success{unit="containerd"} 1\n'
                        'atlas_systemd_unit_active{unit="containerd"} 1\n', encoding="utf-8")
                    if age_seconds:
                        written_at = time.time() - age_seconds
                        os.utime(metric_file, (written_at, written_at))
                    for node in kind_nodes:
                        command(["docker", "exec", node, "mkdir", "-p",
                                 "/var/lib/atlas-kubekit/node-exporter-textfile"])
                        command(["docker", "cp", str(metric_file),
                                 node + ":/var/lib/atlas-kubekit/node-exporter-textfile/atlas_systemd_units.prom"])

                push_systemd_fixture(True)
                kubelet_query = urlencode({"query": 'atlas_systemd_unit_active{unit="kubelet"}'})
                deadline = time.monotonic() + 90
                while True:
                    try:
                        with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/query?" + kubelet_query,
                                     timeout=3) as response:
                            samples = json.load(response)["data"]["result"]
                        if samples and all(float(sample["value"][1]) == 1 for sample in samples):
                            break
                    except (OSError, ValueError, KeyError, IndexError, TypeError):
                        pass
                    if forward.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Atlas systemd textfile metrics were not scraped from every test node")
                    time.sleep(2)

                def wait_systemd_rule(name, state, refresh=None):
                    deadline = time.monotonic() + 190
                    next_refresh = time.monotonic() + 30
                    while True:
                        if refresh is not None and time.monotonic() >= next_refresh:
                            refresh()
                            next_refresh = time.monotonic() + 30
                        try:
                            with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/rules", timeout=3) as response:
                                groups = json.load(response)["data"]["groups"]
                            selected = [rule for group in groups for rule in group["rules"]
                                        if rule.get("name") == name]
                            if (len(selected) == 1 and selected[0].get("health") == "ok" and
                                    selected[0].get("state") == state):
                                return selected[0]
                        except (OSError, ValueError, KeyError, IndexError, TypeError):
                            pass
                        if forward.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError(name + " did not reach " + state)
                        time.sleep(2)

                unavailable = "AtlasKubeletServiceUnavailable"
                missing = "AtlasKubeletSystemdTelemetryMissing"
                wait_systemd_rule(unavailable, "inactive")
                push_systemd_fixture(False)
                fired = wait_systemd_rule(unavailable, "firing", refresh=lambda: push_systemd_fixture(False))
                if (fired.get("labels", {}).get("risk_id") != "R-NODE-06" or
                        "kubelet" not in str(fired.get("annotations", {}).get("summary", "")).lower()):
                    raise RuntimeError("Atlas kubelet systemd alert lacks its risk or actionable summary")
                push_systemd_fixture(True)
                wait_systemd_rule(unavailable, "inactive")
                wait_systemd_rule(missing, "inactive")
                push_systemd_fixture(True, age_seconds=180)
                wait_systemd_rule(missing, "firing")
                wait_systemd_rule(unavailable, "inactive")
                push_systemd_fixture(True)
                wait_systemd_rule(missing, "inactive")
                print("Atlas systemd rules detected a stale textfile while its healthy values were still scraped.")
                alertmanager_query = ('count(up{namespace="monitoring",pod=~".*alertmanager.*",'
                                      'job=~".*alertmanager.*"} == 1)')
                deadline = time.monotonic() + 120
                while True:
                    try:
                        with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/query?" +
                                     urlencode({"query": alertmanager_query}), timeout=3) as response:
                            samples = json.load(response)["data"]["result"]
                        if samples and float(samples[0]["value"][1]) >= 1:
                            break
                    except (OSError, ValueError, KeyError, IndexError, TypeError):
                        pass
                    if forward.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Prometheus did not scrape the isolated Alertmanager target")
                    time.sleep(2)
                target_query = ('up{namespace="monitoring",container="alertmanager",'
                                'pod=~".*alertmanager.*",job=~".*alertmanager.*"} == 1')
                with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/query?" +
                             urlencode({"query": target_query}), timeout=3) as response:
                    targets = json.load(response)["data"]["result"]
                if len(targets) != 1:
                    raise RuntimeError("expected exactly one Alertmanager data-plane scrape target")
                target = targets[0]["metric"]
                target_job, target_instance, target_service = target.get("job"), target.get("instance"), target.get("service")
                if not all(isinstance(value, str) and value for value in (target_job, target_instance, target_service)):
                    raise RuntimeError("Alertmanager scrape target is missing job, instance, or service labels")
                target_args = SimpleNamespace(lang="zh_CN", monitoring_namespace="monitoring",
                    monitoring_release="stack", monitoring_prometheus_name=prom_name,
                    monitoring_hold_seconds=1, monitoring_max_age=30, monitoring_severity="warning",
                    monitoring_target=["synthetic=missing", target_job + "=" + target_instance])
                target_values = monitoring_scrapes.from_arguments(target_args, configuration.template(), environment)
                target_observation = monitoring_scrapes.observe(target_values, environment)
                target_plan = plans.make("monitoring-scrape-rules", client.cluster_identity(),
                                         target_observation, target_values)
                execution.execute(target_plan.record(), target_plan.id, ClusterCoordinator(client),
                    lambda values: monitoring_scrapes.observe(values, environment),
                    lambda values, checkpoint: monitoring_scrapes.apply(values, environment, checkpoint,
                                                                          target_plan.id),
                    monitoring_scrapes.parameters)
                monitor = json.loads(command(kubectl + ["get", "servicemonitor", target_service, "-o", "json"]))
                monitor["metadata"] = {key: value for key, value in monitor["metadata"].items()
                                       if key not in ("resourceVersion", "uid", "creationTimestamp", "managedFields",
                                                      "generation")}
                monitor.pop("status", None)

                def wait_alertmanager_target(expected_state):
                    deadline = time.monotonic() + 150
                    while True:
                        try:
                            with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/rules", timeout=3) as response:
                                groups = json.load(response)["data"]["groups"]
                            selected = [rule for group in groups for rule in group["rules"]
                                        if rule.get("name") == "AtlasScrapeTargetOrTelemetryMissing" and
                                        rule.get("labels", {}).get("job") == target_job and
                                        rule.get("labels", {}).get("instance") == target_instance]
                            if (len(selected) == 1 and selected[0].get("health") == "ok" and
                                    selected[0].get("state") == expected_state):
                                return
                        except (OSError, ValueError, KeyError, IndexError, TypeError):
                            pass
                        if forward.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Alertmanager scrape-target alert did not reach " + expected_state)
                        time.sleep(2)

                command(kubectl + ["delete", "servicemonitor", target_service, "--wait=true"])
                wait_alertmanager_target("firing")
                command(kubectl + ["apply", "-f", "-"], data=monitor)
                wait_alertmanager_target("inactive")
                print("kube-prometheus-stack self-monitoring: Prometheus/Alertmanager and node-exporter rules loaded healthy; real CPU, memory, filesystem-space and inode metrics were scraped; Atlas host textfile values reached Prometheus and kubelet failure/recovery rule evaluation passed; Alertmanager target-missing alert fired/resolved after ServiceMonitor removal/recovery.")
            if args.tsdb_capacity:
                def tsdb_metric(name):
                    expression = name + '{namespace="monitoring",persistentvolumeclaim="' + tsdb_claim + '"}'
                    url = "http://127.0.0.1:" + str(port) + "/api/v1/query?" + urlencode({"query": expression})
                    with urlopen(url, timeout=3) as response:
                        samples = json.load(response)["data"]["result"]
                    if len(samples) != 1:
                        raise ValueError("expected one Prometheus TSDB PVC metric")
                    return float(samples[0]["value"][1])

                deadline = time.monotonic() + 150
                while True:
                    try:
                        available = tsdb_metric("kubelet_volume_stats_available_bytes")
                        capacity = tsdb_metric("kubelet_volume_stats_capacity_bytes")
                        if capacity > 0 and 0 <= available <= capacity:
                            break
                    except (OSError, ValueError, KeyError, IndexError, TypeError):
                        pass
                    if forward.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Prometheus TSDB PVC capacity metrics were not scraped")
                    time.sleep(2)
                free_percent = available * 100 / capacity
                high = min(99, math.floor(free_percent) + 1)
                low = max(1, high - 2)
                if not free_percent < high or not low < high:
                    raise RuntimeError("TSDB PVC free space is too high to exercise a safe sub-100% test threshold")
                storage_args = SimpleNamespace(lang="zh_CN", monitoring_namespace="monitoring",
                    monitoring_release="stack", monitoring_prometheus_name=prom_name,
                    monitoring_hold_seconds=1, monitoring_max_age=120, monitoring_severity="warning",
                    monitoring_pvc=["monitoring/" + tsdb_claim], monitoring_free_percent=high)
                params = monitoring_storage.from_arguments(storage_args, configuration.template(), environment)
                expected_capacity_labels = monitoring_storage._manifest(params)["spec"]["groups"][0]["rules"][0]["labels"]
                observation = monitoring_storage.observe(params, environment)
                plan = plans.make("monitoring-storage-rules", client.cluster_identity(), observation, params)
                execution.execute(plan.record(), plan.id, ClusterCoordinator(client),
                    lambda values: monitoring_storage.observe(values, environment),
                    lambda values, checkpoint: monitoring_storage.apply(values, environment, checkpoint, plan.id),
                    monitoring_storage.parameters)

                def wait_tsdb_capacity(expected):
                    deadline = time.monotonic() + 180
                    while True:
                        try:
                            with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/rules", timeout=3) as response:
                                groups = json.load(response)["data"]["groups"]
                            matching = [rule for group in groups for rule in group["rules"]
                                        if rule.get("name") == "AtlasPVCFreeSpaceOrTelemetryMissing" and
                                        all(rule.get("labels", {}).get(key) == value
                                            for key, value in expected_capacity_labels.items())]
                            if len(matching) == 1 and matching[0].get("health") == "ok" and matching[0].get("state") == expected:
                                return
                        except (OSError, ValueError, KeyError, IndexError, TypeError):
                            pass
                        if forward.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Prometheus TSDB PVC capacity alert did not reach " + expected)
                        time.sleep(2)

                wait_tsdb_capacity("firing")
                storage_args.monitoring_free_percent = low
                params = monitoring_storage.from_arguments(storage_args, configuration.template(), environment)
                observation = monitoring_storage.observe(params, environment)
                plan = plans.make("monitoring-storage-rules", client.cluster_identity(), observation, params)
                execution.execute(plan.record(), plan.id, ClusterCoordinator(client),
                    lambda values: monitoring_storage.observe(values, environment),
                    lambda values, checkpoint: monitoring_storage.apply(values, environment, checkpoint, plan.id),
                    monitoring_storage.parameters)
                wait_tsdb_capacity("inactive")
                print("Prometheus TSDB PVC: real kubelet capacity metrics drove the managed low-space alert to firing and recovery without filling the disk.")
            if args.keda:
                deadline = time.monotonic() + 150
                while True:
                    try:
                        query = 'count(up{namespace="keda"})'
                        with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/query?" +
                                     urlencode({"query": query}), timeout=3) as response:
                            samples = json.load(response)["data"]["result"]
                        if samples and float(samples[0]["value"][1]) >= 1:
                            break
                    except (OSError, ValueError, KeyError, IndexError, TypeError):
                        pass
                    if forward.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Prometheus did not scrape the KEDA operator ServiceMonitor")
                    time.sleep(2)
                keda_args = SimpleNamespace(lang="zh_CN", monitoring_notification_language="zh_CN",
                    monitoring_namespace="monitoring", monitoring_release="stack",
                    monitoring_prometheus_name=prom_name, monitoring_hold_seconds=1,
                    monitoring_max_age=30, monitoring_restart_threshold=3,
                    monitoring_restart_window_seconds=60, monitoring_job_timeout_seconds=3600,
                    monitoring_cronjob_overdue_seconds=900, monitoring_keda_latency_seconds=5,
                    monitoring_keda_namespace="keda",
                    monitoring_severity="warning")
                keda_values = monitoring_workload_health.from_arguments(keda_args, configuration.template(), environment)
                keda_observation = monitoring_workload_health.observe(keda_values, environment)
                keda_plan = plans.make("monitoring-workload-health-rules", client.cluster_identity(),
                                       keda_observation, keda_values)
                execution.execute(keda_plan.record(), keda_plan.id, ClusterCoordinator(client),
                    lambda values: monitoring_workload_health.observe(values, environment),
                    lambda values, checkpoint: monitoring_workload_health.apply(values, environment, checkpoint,
                                                                                 keda_plan.id),
                    monitoring_workload_health.parameters)
                keda_namespace = ["kubectl", "--kubeconfig", kubeconfig, "-n", "monitoring"]
                command(keda_namespace + ["create", "-f", "-"], data={"apiVersion": "apps/v1", "kind": "Deployment",
                    "metadata": {"name": "atlas-keda-e2e", "namespace": "monitoring"},
                    "spec": {"replicas": 0, "selector": {"matchLabels": {"app": "atlas-keda-e2e"}},
                        "template": {"metadata": {"labels": {"app": "atlas-keda-e2e"}},
                            "spec": {"containers": [{"name": "app", "image": "python:3.13-slim",
                                                      "command": ["sleep", "3600"]}]}}}})

                def scaled_object(server_address):
                    return {"apiVersion": "keda.sh/v1alpha1", "kind": "ScaledObject",
                        "metadata": {"name": "atlas-keda-e2e", "namespace": "monitoring"},
                        "spec": {"scaleTargetRef": {"name": "atlas-keda-e2e"}, "pollingInterval": 5,
                            "cooldownPeriod": 5, "minReplicaCount": 0, "maxReplicaCount": 1,
                            "triggers": [{"type": "prometheus", "metadata": {"serverAddress": server_address,
                                "metricName": "atlas_keda_e2e", "query": "vector(1)", "threshold": "1"}}]}}

                def wait_keda_alert(expected):
                    deadline = time.monotonic() + (180 if expected else 150)
                    while True:
                        try:
                            with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/alerts", timeout=3) as response:
                                alerts = json.load(response)["data"]["alerts"]
                            matches = [item for item in alerts if
                                item.get("labels", {}).get("alertname") == "AtlasKEDAScaledObjectErrors" and
                                item.get("labels", {}).get("scaledObject") == "atlas-keda-e2e" and
                                item.get("labels", {}).get("exported_namespace") == "monitoring" and
                                item.get("state") == "firing"]
                            if bool(matches) == expected:
                                if matches and ("KEDA" not in matches[0].get("annotations", {}).get("summary", "") or
                                                "扩缩容控制发生错误" not in matches[0].get("annotations", {}).get("summary", "")):
                                    raise RuntimeError("KEDA alert did not include its Chinese summary")
                                return
                        except (OSError, ValueError, KeyError, IndexError, TypeError):
                            pass
                        if forward.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("KEDA scaler error alert did not reach expected state: " + str(expected))
                        time.sleep(2)

                bad_server = "http://missing-keda-prometheus.monitoring.svc:9090"
                command(keda_namespace + ["create", "-f", "-"], data=scaled_object(bad_server))
                deadline = time.monotonic() + 120
                error_query = ('increase(keda_scaled_object_errors_total{exported_namespace="monitoring",'
                               'scaledObject="atlas-keda-e2e"}[60s])')
                while True:
                    try:
                        with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/query?" +
                                     urlencode({"query": error_query}), timeout=3) as response:
                            error_samples = json.load(response)["data"]["result"]
                        if any(float(sample["value"][1]) > 0 for sample in error_samples):
                            break
                    except (OSError, ValueError, KeyError, IndexError, TypeError):
                        pass
                    if forward.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("KEDA scaler error metric was not scraped from the real operator")
                    time.sleep(2)
                wait_keda_alert(True)
                prometheus_services = json.loads(command(kubectl + ["get", "service", "-l",
                    "app=kube-prometheus-stack-prometheus", "-o", "json"]))["items"]
                prometheus_services = [service for service in prometheus_services
                                       if any(port.get("port") == 9090 for port in service.get("spec", {}).get("ports", []))]
                if len(prometheus_services) != 1:
                    raise RuntimeError("could not identify a unique in-cluster Prometheus service for KEDA recovery")
                service = "http://" + prometheus_services[0]["metadata"]["name"] + ".monitoring.svc:9090"
                command(keda_namespace + ["apply", "-f", "-"], data=scaled_object(service))
                wait_keda_alert(False)
                recovered = json.loads(command(keda_namespace + ["get", "scaledobject", "atlas-keda-e2e", "-o", "json"]))
                ready = next((condition for condition in recovered.get("status", {}).get("conditions", [])
                              if condition.get("type") == "Ready"), {})
                if ready.get("status") != "True":
                    raise RuntimeError("KEDA ScaledObject did not return to Ready after repairing its metrics source")
                keda_monitor = ["kubectl", "--kubeconfig", kubeconfig, "-n", "keda"]
                monitor = json.loads(command(keda_monitor + ["get", "servicemonitor", "keda-operator", "-o", "json"]))
                monitor["metadata"] = {key: value for key, value in monitor["metadata"].items()
                                        if key not in ("resourceVersion", "uid", "creationTimestamp", "managedFields",
                                                       "generation")}
                monitor.pop("status", None)
                def wait_keda_target(expected):
                    state = "firing" if expected else "inactive"
                    deadline = time.monotonic() + 150
                    while True:
                        try:
                            with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/rules", timeout=3) as response:
                                groups = json.load(response)["data"]["groups"]
                            selected = [rule for group in groups for rule in group["rules"]
                                        if rule.get("name") == "AtlasKEDAOperatorTargetMissing"]
                            if len(selected) == 1 and selected[0].get("health") == "ok" and selected[0].get("state") == state:
                                return
                        except (OSError, ValueError, KeyError, IndexError, TypeError):
                            pass
                        if forward.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("KEDA Operator target-loss alert did not reach " + state)
                        time.sleep(2)

                command(keda_monitor + ["delete", "servicemonitor", "keda-operator", "--wait=true"])
                wait_keda_target(True)
                command(keda_monitor + ["apply", "-f", "-"], data=monitor)
                wait_keda_target(False)
                deadline = time.monotonic() + 90
                while True:
                    try:
                        query = 'count(up{namespace="keda",service="keda-operator"} == 1)'
                        with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/query?" +
                                     urlencode({"query": query}), timeout=3) as response:
                            samples = json.load(response)["data"]["result"]
                        if samples and float(samples[0]["value"][1]) >= 1:
                            break
                    except (OSError, ValueError, KeyError, IndexError, TypeError):
                        pass
                    if forward.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("KEDA Operator target was not scraped after ServiceMonitor restoration")
                    time.sleep(2)
                print("KEDA 2.21.0: real ServiceMonitor scrape, scaler failure/Chinese alert/recovery, and Operator-target loss alert/recovery passed.")
                if args.workload_timeouts:
                    from tests.e2e import monitoring_workload_timeouts_kind
                    monitoring_workload_timeouts_kind.run(
                        ["kubectl", "--kubeconfig", kubeconfig], kubeconfig, name,
                        "http://127.0.0.1:" + str(port), root)
                    return
            coverage_result = coverage_report.collect(client, {},
                prometheus.collect_alert_rules("http://127.0.0.1:" + str(port)))
            coverage_by_id = {item["riskId"]: item for item in coverage_result["risks"]}
            for risk_id in ("R-OBS-01", "R-CP-01", "R-NET-01", "R-EDGE-04"):
                item = coverage_by_id[risk_id]
                if (not item["configuredRuleNames"] or not item["loadedRules"] or
                        any(rule["health"] != "ok" for rule in item["loadedRules"]) or
                        item["ruleEvidence"]["state"] != "verified" or
                        item["status"] != "unverified"):
                    raise RuntimeError("coverage report did not preserve configured/loaded/unverified evidence for " + risk_id)
            if coverage_result["counts"]["covered"] != 0 or len(coverage_result["risks"]) != 63:
                raise RuntimeError("coverage report must include the full risk catalog without claiming end-to-end coverage")
            print("Real PrometheusRule inventory and Prometheus rules API were reconciled for the full 63-risk report; data and delivery remained unverified.")
            exercise_rule_identity(client, kubectl, "http://127.0.0.1:" + str(port), prom_name)
            if args.dns_failure:
                from tests.e2e import monitoring_dns_failure
                monitoring_dns_failure.run(kubectl, "http://127.0.0.1:" + str(port), environment, dns_args)
            if args.lb:
                deadline = time.monotonic() + 120
                while True:
                    try:
                        query = 'haproxy_backend_active_servers{job="atlas-lb-metrics",lb="lb1"}'
                        url = "http://127.0.0.1:" + str(port) + "/api/v1/query?" + urlencode({"query": query})
                        with urlopen(url, timeout=3) as response:
                            samples = json.load(response)["data"]["result"]
                        if len(samples) == 1 and float(samples[0]["value"][1]) == 0:
                            with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/alerts", timeout=3) as response:
                                alerts = json.load(response)["data"]["alerts"]
                            lb_alert = next((item for item in alerts
                                if item.get("labels", {}).get("alertname") ==
                                "AtlasLBBackendUnavailableOrMetricsMissing" and item.get("state") == "firing"), None)
                            if lb_alert:
                                annotations = lb_alert.get("annotations", {})
                                if ("LB 节点的后端全部不可用或后端指标缺失" not in annotations.get("summary", "") or
                                        "请根据 LB 节点和后端名称检查健康检查、后端服务和网络连通" not in
                                        annotations.get("description", "")):
                                    raise RuntimeError("LB alert did not contain the localized Chinese summary and action guidance")
                                break
                    except (OSError, ValueError, KeyError, IndexError, TypeError):
                        pass
                    if forward.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Prometheus did not scrape the real temporary HAProxy endpoint")
                    time.sleep(2)
                print("Real HAProxy exporter metrics were scraped; the backend-unavailable alert fired with a Chinese summary and action guidance.")
            if args.longhorn_collection:
                deadline = time.monotonic() + 150
                disk_alert_observed = False
                while True:
                    try:
                        url = "http://127.0.0.1:" + str(port) + "/api/v1/query?" + urlencode({"query": "atlas_longhorn_collection_test"})
                        with urlopen(url, timeout=3) as response:
                            samples = json.load(response)["data"]["result"]
                        if len(samples) == 1 and float(samples[0]["value"][1]) == 1:
                            with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/alerts", timeout=3) as response:
                                alerts = json.load(response)["data"]["alerts"]
                            disk_alert = next((item for item in alerts
                                if item.get("labels", {}).get("alertname") == "AtlasLonghornDiskUsageHigh"
                                and item.get("state") == "firing"), None)
                            if disk_alert:
                                labels, annotations = disk_alert.get("labels", {}), disk_alert.get("annotations", {})
                                if (labels.get("risk_id") != "R-STOR-04" or labels.get("node") != "worker-e2e" or
                                        labels.get("disk") != "disk-e2e" or annotations.get("summary") != "Longhorn 磁盘使用率过高"):
                                    raise RuntimeError("Longhorn disk alert did not preserve Chinese details and disk identity")
                                disk_alert_observed = True
                                break
                    except (OSError, ValueError, KeyError, IndexError, TypeError):
                        pass
                    if forward.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Prometheus did not discover and scrape the controlled Longhorn-like service")
                    time.sleep(2)
                if not disk_alert_observed:
                    raise RuntimeError("Prometheus did not evaluate the managed Longhorn disk capacity rule")
                print("Real Prometheus Operator selected the managed ServiceMonitor, scraped controlled Longhorn disk metrics and fired the Chinese capacity alert; NetworkPolicy enforcement not verified.")
            if args.robot:
                relay_metrics_server = (
                    "from http.server import BaseHTTPRequestHandler,HTTPServer\n"
                    "from pathlib import Path\n"
                    "Path('/tmp/relay-errors').write_text('0')\n"
                    "class H(BaseHTTPRequestHandler):\n"
                    " def do_GET(self):\n"
                    "  if self.path != '/metrics' or self.headers.get('Authorization') != 'Bearer test-only-relay-token':\n"
                    "   self.send_response(401); self.end_headers(); return\n"
                    "  failures=Path('/tmp/relay-errors').read_text().strip()\n"
                    "  body=('atlas_relay_deliveries_total{channel=\\\"wecom\\\",outcome=\\\"accepted\\\"} 4\\n'"
                    "+'atlas_relay_deliveries_total{channel=\\\"wecom\\\",outcome=\\\"rejected\\\"} '+failures+'\\n').encode()\n"
                    "  self.send_response(200); self.send_header('Content-Type','text/plain; version=0.0.4'); self.end_headers(); self.wfile.write(body)\n"
                    " def log_message(self,*args): pass\n"
                    "HTTPServer(('0.0.0.0',18081),H).serve_forever()\n")
                command(kubectl + ["create", "-f", "-"], data={"apiVersion": "v1", "kind": "Pod",
                    "metadata": {"name": "atlas-relay-metrics-test", "namespace": "monitoring",
                                 "labels": {"atlas-kubekit.io/relay-metrics": "enabled"}},
                    "spec": {"containers": [{"name": "relay", "image": "python:3.13-slim",
                        "command": ["python", "-u", "-c", relay_metrics_server],
                        "ports": [{"name": "atlas-metrics", "containerPort": 18081}]}]}})
                command(kubectl + ["wait", "--for=condition=Ready", "pod/atlas-relay-metrics-test", "--timeout=90s"])
                relay_args = SimpleNamespace(monitoring_namespace="monitoring", monitoring_release="stack",
                    monitoring_prometheus_name=prom_name, monitoring_relay_auth_secret="atlas-relay-auth",
                    monitoring_relay_auth_key="token", lang="zh_CN")
                relay_values = monitoring_relay_metrics.from_arguments(relay_args, configuration.template(), environment)
                relay_observation = monitoring_relay_metrics.observe(relay_values, environment)
                relay_plan = plans.make("monitoring-relay-metrics", client.cluster_identity(), relay_observation, relay_values)
                relay_result = execution.execute(relay_plan.record(), relay_plan.id, ClusterCoordinator(client),
                    lambda values: monitoring_relay_metrics.observe(values, environment),
                    lambda values, checkpoint: monitoring_relay_metrics.apply(values, environment, checkpoint, relay_plan.id),
                    monitoring_relay_metrics.parameters)
                if relay_result["result"]["evaluation"] != "unverified":
                    raise RuntimeError("relay metrics publication incorrectly claimed evaluation")
                deadline = time.monotonic() + 120
                while True:
                    try:
                        query = 'atlas_relay_deliveries_total{channel="wecom",outcome="accepted"}'
                        url = "http://127.0.0.1:" + str(port) + "/api/v1/query?" + urlencode({"query": query})
                        with urlopen(url, timeout=3) as response:
                            samples = json.load(response)["data"]["result"]
                        if len(samples) == 1 and float(samples[0]["value"][1]) == 4:
                            with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/rules", timeout=3) as response:
                                groups = json.load(response)["data"]["groups"]
                            active_rule = any(rule.get("name") == "AtlasNotificationDeliveryFailure" and
                                rule.get("health") == "ok" for group in groups for rule in group["rules"])
                            if active_rule:
                                break
                    except (OSError, ValueError, KeyError, IndexError, TypeError):
                        pass
                    if forward.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Prometheus did not discover and scrape the authenticated relay metrics target")
                    time.sleep(2)
                print("Real Prometheus Operator selected the managed relay PodMonitor and scraped a bearer-authenticated test relay; delivery-failure rule loaded. Provider delivery remains separately verified.")
                command(kubectl + ["exec", "atlas-relay-metrics-test", "--", "python", "-c",
                    "from pathlib import Path; Path('/tmp/relay-errors').write_text('1')"])
                deadline = time.monotonic() + 120
                firing_alert = None
                while True:
                    try:
                        with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/alerts", timeout=3) as response:
                            alerts = json.load(response)["data"]["alerts"]
                        firing_alert = next((item for item in alerts if
                            item.get("labels", {}).get("alertname") == "AtlasNotificationDeliveryFailure" and
                            item.get("state") == "firing"), None)
                        if firing_alert:
                            break
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                    if forward.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("relay delivery-failure alert did not fire from the scraped failure counter")
                    time.sleep(2)
                if "通知中继投递失败" not in firing_alert.get("annotations", {}).get("summary", ""):
                    raise RuntimeError("relay delivery-failure alert did not include its localized summary")
                command(kubectl + ["exec", "atlas-relay-metrics-test", "--", "python", "-c",
                    "from pathlib import Path; Path('/tmp/relay-errors').write_text('0')"])
                # The production rule uses a five-minute increase window; allow
                # the last failure sample to age out before expecting recovery.
                deadline = time.monotonic() + 420
                while True:
                    try:
                        with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/alerts", timeout=3) as response:
                            alerts = json.load(response)["data"]["alerts"]
                        if not any(item.get("labels", {}).get("alertname") == "AtlasNotificationDeliveryFailure"
                                   for item in alerts):
                            break
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
                    if forward.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("relay delivery-failure alert did not resolve after the counter reset")
                    time.sleep(2)
                print("Prometheus fired and resolved the Chinese relay-delivery alert from bearer-authenticated test metrics; no real provider message was sent.")
            if args.workload:
                def wait_available(expected, timeout=120):
                    deadline = time.monotonic() + timeout
                    expression = 'kube_deployment_status_replicas_available{namespace="monitoring",deployment="atlas-workload-e2e"}'
                    while True:
                        try:
                            url = "http://127.0.0.1:" + str(port) + "/api/v1/query?" + urlencode({"query": expression})
                            with urlopen(url, timeout=3) as response:
                                result = json.load(response)["data"]["result"]
                            if len(result) == 1 and float(result[0]["value"][1]) == expected:
                                return
                        except (OSError, ValueError, KeyError, IndexError, TypeError):
                            pass
                        if forward.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("kube-state-metrics did not report expected available replicas")
                        time.sleep(2)
                def wait_workload(expected, rollout_expected, timeout=120):
                    deadline = time.monotonic() + timeout
                    while True:
                        try:
                            with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/rules", timeout=3) as response:
                                data = json.load(response)
                            rules = [rule for group in data["data"]["groups"] for rule in group["rules"]]
                            availability = any(rule.get("name") == "AtlasDeploymentAvailabilityOrTelemetryMissing" and
                                               rule.get("state") == expected and rule.get("health") == "ok"
                                               for rule in rules)
                            rollout = any(rule.get("name") == "AtlasDeploymentRolloutStalled" and
                                          rule.get("state") == rollout_expected and rule.get("health") == "ok"
                                          for rule in rules)
                            if availability and rollout:
                                return
                        except (OSError, ValueError, KeyError):
                            pass
                        if forward.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("workload availability or rollout alert did not reach expected state")
                        time.sleep(2)
                wait_available(1)
                wait_workload("inactive", "inactive")
                command(kubectl + ["set", "image", "deployment/atlas-workload-e2e", "app=python:atlas-e2e-missing"])
                wait_available(0)
                wait_workload("firing", "firing")
                command(kubectl + ["set", "image", "deployment/atlas-workload-e2e", "app=python:3.13-slim"])
                command(kubectl + ["rollout", "status", "deployment/atlas-workload-e2e", "--timeout=90s"])
                wait_available(1)
                wait_workload("inactive", "inactive")
                print("Real kube-state-metrics Deployment availability and progress-deadline alerts: healthy -> stalled -> recovered passed.")
            if args.controllers:
                for kind, capability, desired_metric, available_metric, alert_name in (
                    ("statefulset", monitoring_statefulsets, "kube_statefulset_replicas", "kube_statefulset_status_replicas_available", "AtlasStatefulSetAvailabilityOrTelemetryMissing"),
                    ("daemonset", monitoring_daemonsets, "kube_daemonset_status_desired_number_scheduled", "kube_daemonset_status_number_available", "AtlasDaemonSetAvailabilityOrTelemetryMissing"),
                ):
                    resource = "atlas-" + kind + "-e2e"
                    spec = {"selector": {"matchLabels": {"app": resource}},
                            "template": {"metadata": {"labels": {"app": resource}},
                                "spec": {"tolerations": [{"operator": "Exists"}],
                                         "containers": [{"name": "app", "image": "python:3.13-slim",
                                                         "command": ["sleep", "3600"]}]}}}
                    if kind == "statefulset":
                        spec.update({"replicas": 1, "serviceName": resource})
                    resource_kind = "StatefulSet" if kind == "statefulset" else "DaemonSet"
                    command(kubectl + ["create", "-f", "-"], data={"apiVersion": "apps/v1", "kind": resource_kind,
                        "metadata": {"name": resource, "namespace": "monitoring"}, "spec": spec})
                    command(kubectl + ["rollout", "status", kind + "/" + resource, "--timeout=90s"])
                    controller_args = SimpleNamespace(lang="en", monitoring_namespace="monitoring", monitoring_release="stack",
                        monitoring_prometheus_name=prom_name, monitoring_hold_seconds=1, monitoring_max_age=30,
                        monitoring_severity="warning", monitoring_controller=["monitoring/" + resource])
                    params = capability.from_arguments(controller_args, configuration.template(), environment)
                    observation = capability.observe(params, environment)
                    plan = plans.make("monitoring-" + kind + "-rules", client.cluster_identity(), observation, params)
                    execution.execute(plan.record(), plan.id, ClusterCoordinator(client),
                        lambda values: capability.observe(values, environment),
                        lambda values, checkpoint: capability.apply(values, environment, checkpoint, plan.id),
                        capability.parameters)
                    def wait_controller(expected_metric, expected_alert):
                        deadline = time.monotonic() + 120
                        selector = '{namespace="monitoring",' + kind + '="' + resource + '"}'
                        expression = available_metric + selector
                        desired_expression = desired_metric + selector
                        last_observation = "no Prometheus response"
                        while True:
                            try:
                                url = "http://127.0.0.1:" + str(port) + "/api/v1/query?" + urlencode({"query": expression})
                                with urlopen(url, timeout=3) as response:
                                    result = json.load(response)["data"]["result"]
                                with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/query?" +
                                             urlencode({"query": desired_expression}), timeout=3) as response:
                                    desired_result = json.load(response)["data"]["result"]
                                with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/rules", timeout=3) as response:
                                    groups = json.load(response)["data"]["groups"]
                                matched_rule = next((rule for group in groups for rule in group["rules"]
                                                     if rule.get("name") == alert_name), None)
                                matched = (matched_rule is not None and matched_rule.get("state") == expected_alert and
                                           matched_rule.get("health") == "ok")
                                values = (len(result) == 1 and len(desired_result) == 1 and
                                          float(result[0]["value"][1]) >= 0 and
                                          float(desired_result[0]["value"][1]) > 0)
                                actual, desired = ((float(result[0]["value"][1]),
                                                    float(desired_result[0]["value"][1])) if values else (None, None))
                                metric_matches = (values and
                                                  (actual == desired if expected_metric == "healthy" else actual < desired))
                                last_observation = "available=" + json.dumps(result) + "; desired=" + json.dumps(desired_result) + "; rule=" + json.dumps(
                                    None if matched_rule is None else {key: matched_rule.get(key) for key in
                                        ("name", "state", "health", "lastError")}, ensure_ascii=False)
                                if metric_matches and matched:
                                    return
                            except (OSError, ValueError, KeyError, IndexError, TypeError):
                                pass
                            if forward.poll() is not None or time.monotonic() >= deadline:
                                raise RuntimeError(kind + " did not reach expected metric=" + str(expected_metric) +
                                                   " and alert state=" + expected_alert + "; last observation: " +
                                                   last_observation)
                            time.sleep(2)
                    wait_controller("healthy", "inactive")
                    command(kubectl + ["set", "image", kind + "/" + resource, "app=python:atlas-e2e-missing"])
                    wait_controller("unhealthy", "firing")
                    command(kubectl + ["set", "image", kind + "/" + resource, "app=python:3.13-slim"])
                    # A StatefulSet can retain the failed Pod after reverting an
                    # update; delete only this test-owned Pod to unblock rollout.
                    if kind == "statefulset":
                        command(kubectl + ["delete", "pod", resource + "-0", "--wait=true", "--timeout=60s"])
                    command(kubectl + ["rollout", "status", kind + "/" + resource, "--timeout=90s"])
                    wait_controller("healthy", "inactive")
                    print("Real kube-state-metrics " + kind + ": healthy -> unavailable -> recovered passed.")
            if args.storage:
                def metric(name):
                    expression = name + '{namespace="monitoring",persistentvolumeclaim="atlas-pvc-e2e"}'
                    url = "http://127.0.0.1:" + str(port) + "/api/v1/query?" + urlencode({"query": expression})
                    with urlopen(url, timeout=3) as response:
                        results = json.load(response)["data"]["result"]
                    if len(results) != 1:
                        raise ValueError("expected a single PVC metric source")
                    return float(results[0]["value"][1])
                deadline = time.monotonic() + 150
                while True:
                    try:
                        available = metric("kubelet_volume_stats_available_bytes")
                        capacity = metric("kubelet_volume_stats_capacity_bytes")
                        if capacity > 0 and 0 <= available <= capacity:
                            break
                    except (OSError, ValueError, KeyError, IndexError, TypeError):
                        pass
                    if time.monotonic() >= deadline:
                        storage_args = SimpleNamespace(lang="en", monitoring_namespace="monitoring", monitoring_release="stack",
                            monitoring_prometheus_name=prom_name, monitoring_hold_seconds=1, monitoring_max_age=120,
                            monitoring_severity="warning", monitoring_pvc=["monitoring/atlas-pvc-e2e"], monitoring_free_percent=15)
                        params = monitoring_storage.from_arguments(storage_args, configuration.template(), environment)
                        observation = monitoring_storage.observe(params, environment)
                        plan = plans.make("monitoring-storage-rules", client.cluster_identity(), observation, params)
                        execution.execute(plan.record(), plan.id, ClusterCoordinator(client),
                            lambda values: monitoring_storage.observe(values, environment),
                            lambda values, checkpoint: monitoring_storage.apply(values, environment, checkpoint, plan.id),
                            monitoring_storage.parameters)
                        alert_deadline = time.monotonic() + 60
                        alert_diagnostic = {"state": "not-observed", "query": "not-run"}
                        while time.monotonic() < alert_deadline:
                            try:
                                with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/rules", timeout=3) as response:
                                    groups = json.load(response)["data"]["groups"]
                                matching = [rule for group in groups for rule in group["rules"]
                                            if rule.get("name") == "AtlasPVCFreeSpaceOrTelemetryMissing"]
                                query = "max(" + monitoring_storage._manifest(params)["spec"]["groups"][0]["rules"][0]["expr"] + ")"
                                query_url = "http://127.0.0.1:" + str(port) + "/api/v1/query?" + urlencode({"query": query})
                                with urlopen(query_url, timeout=3) as response:
                                    query_result = json.load(response)
                                alert_diagnostic = {
                                    "rules": [{"state": rule.get("state"), "health": rule.get("health"),
                                               "lastError": rule.get("lastError")}
                                              for rule in matching],
                                    "queryStatus": query_result.get("status"),
                                    "queryResult": query_result.get("data", {}).get("result"),
                                }
                                if any(rule.get("state") == "firing" and rule.get("health") == "ok"
                                       for rule in matching):
                                    print("Real Prometheus fired for missing PVC telemetry; capacity threshold is unverified.")
                                    raise RuntimeError("real kubelet PVC metrics unavailable; low-space test not performed")
                            except (OSError, ValueError, KeyError):
                                pass
                            time.sleep(2)
                        raise RuntimeError("real kubelet PVC metrics unavailable and missing-data alert was not verified: " +
                                           json.dumps(alert_diagnostic, ensure_ascii=False, sort_keys=True))
                    time.sleep(2)
                percent = available * 100 / capacity
                low, high = math.floor(percent) - 1, math.ceil(percent) + 1
                if not 1 <= low < high <= 99:
                    raise RuntimeError("PVC free-space percentage cannot be safely bracketed for this test")
                def publish_storage(threshold):
                    storage_args = SimpleNamespace(lang="en", monitoring_namespace="monitoring", monitoring_release="stack",
                        monitoring_prometheus_name=prom_name, monitoring_hold_seconds=1, monitoring_max_age=120,
                        monitoring_severity="warning", monitoring_pvc=["monitoring/atlas-pvc-e2e"], monitoring_free_percent=threshold)
                    params = monitoring_storage.from_arguments(storage_args, configuration.template(), environment)
                    observation = monitoring_storage.observe(params, environment)
                    plan = plans.make("monitoring-storage-rules", client.cluster_identity(), observation, params)
                    execution.execute(plan.record(), plan.id, ClusterCoordinator(client),
                        lambda values: monitoring_storage.observe(values, environment),
                        lambda values, checkpoint: monitoring_storage.apply(values, environment, checkpoint, plan.id),
                        monitoring_storage.parameters)
                def wait_storage(expected):
                    deadline = time.monotonic() + 180
                    while True:
                        try:
                            with urlopen("http://127.0.0.1:" + str(port) + "/api/v1/rules", timeout=3) as response:
                                groups = json.load(response)["data"]["groups"]
                            if any(rule.get("name") == "AtlasPVCFreeSpaceOrTelemetryMissing" and
                                   rule.get("state") == expected and rule.get("health") == "ok"
                                   for group in groups for rule in group["rules"]):
                                return
                        except (OSError, ValueError, KeyError):
                            pass
                        if forward.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("PVC alert did not reach " + expected)
                        time.sleep(2)
                publish_storage(high)
                wait_storage("firing")
                publish_storage(low)
                wait_storage("inactive")
                print("Real kubelet PVC metrics: threshold-driven firing and recovery passed (no disk fill).")
                if args.csi_source:
                    command(kubectl + ["exec", "atlas-pvc-e2e", "--", "python", "-c",
                                      "from pathlib import Path; Path('/data/atlas-e2e').write_text('ok')"])
                    default = ["kubectl", "--kubeconfig", kubeconfig, "-n", "default"]
                    command(default + ["scale", "statefulset/csi-hostpathplugin", "--replicas=0"])
                    command(default + ["wait", "--for=delete", "pod/csi-hostpathplugin-0", "--timeout=90s"])
                    command(default + ["scale", "statefulset/csi-hostpathplugin", "--replicas=1"])
                    command(default + ["rollout", "status", "statefulset/csi-hostpathplugin", "--timeout=120s"], timeout=130)
                    command(kubectl + ["delete", "pod", "atlas-pvc-e2e", "--wait=true", "--timeout=90s"])
                    command(kubectl + ["create", "-f", "-"], data=pod)
                    command(kubectl + ["wait", "--for=condition=Ready", "pod/atlas-pvc-e2e", "--timeout=120s"], timeout=130)
                    command(kubectl + ["exec", "atlas-pvc-e2e", "--", "python", "-c",
                                      "from pathlib import Path; assert Path('/data/atlas-e2e').read_text() == 'ok'"])
                    wait_storage("inactive")
                    print("Sample CSI plugin restart, PVC remount and test data persistence passed; no node failover tested.")
                    worker_node = name + "-worker"
                    owner = command(["docker", "inspect", "--format", '{{index .Config.Labels "io.x-k8s.kind.cluster"}}', worker_node]).strip()
                    if owner != name:
                        raise RuntimeError("worker ownership check failed")
                    command(["docker", "stop", "--time", "10", worker_node], timeout=30)
                    wait_storage("firing")
                    command(["docker", "start", worker_node], timeout=30)
                    command(["kubectl", "--kubeconfig", kubeconfig, "wait", "--for=condition=Ready",
                             "node/" + worker_node, "--timeout=180s"], timeout=200)
                    # Node/Pod Ready status can remain stale immediately after a
                    # container restart; require the live kubelet proxy as well.
                    deadline = time.monotonic() + 180
                    while True:
                        try:
                            result = command(["kubectl", "--kubeconfig", kubeconfig, "get", "--raw",
                                              "/api/v1/nodes/" + worker_node + "/proxy/healthz"], timeout=15).strip()
                            if result == "ok":
                                break
                        except RuntimeError:
                            pass
                        if time.monotonic() >= deadline:
                            raise RuntimeError("restarted worker kubelet did not become reachable")
                        time.sleep(3)
                    command(default + ["rollout", "status", "statefulset/csi-hostpathplugin", "--timeout=180s"], timeout=200)
                    command(kubectl + ["wait", "--for=condition=Ready", "pod/atlas-pvc-e2e", "--timeout=180s"], timeout=200)
                    deadline = time.monotonic() + 180
                    while True:
                        try:
                            command(kubectl + ["exec", "atlas-pvc-e2e", "--", "python", "-c",
                                "from pathlib import Path; assert Path('/data/atlas-e2e').read_text() == 'ok'"], timeout=20)
                            break
                        except RuntimeError:
                            if time.monotonic() >= deadline:
                                raise RuntimeError("PVC data could not be read after worker restart") from None
                            time.sleep(3)
                    wait_storage("inactive")
                    print("Test worker stop/restart: missing telemetry alert, node recovery and PVC data persistence passed.")
                if args.csi_attachment_failure:
                    from tests.e2e import monitoring_csi_attachment
                    monitoring_csi_attachment.run(kubectl, "http://127.0.0.1:" + str(port), name + "-worker")
        finally:
            if forward is not None:
                forward.terminate()
                try:
                    forward.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    forward.kill()
                    forward.wait()
            if name in command([args.kind, "get", "clusters"]).splitlines():
                command([args.kind, "delete", "cluster", "--name", name, "--kubeconfig", kubeconfig], timeout=120)
                if name in command([args.kind, "get", "clusters"]).splitlines():
                    raise RuntimeError("test cluster cleanup not verified")
            print("Temporary kind cluster cleanup verified.")


if __name__ == "__main__":
    main()
