#!/usr/bin/env python3
"""Exercise rule publication against an isolated, temporary kind API server.

The test installs minimal synthetic CRDs, not Prometheus Operator, and cannot
establish rule selection, evaluation or notification delivery.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.adapters.kubernetes import Kubernetes
from scripts.operations import configuration
from scripts.operations.capabilities import monitoring_scrapes
from scripts.operations.changes import execution, plans
from scripts.operations.changes.coordinator import ClusterCoordinator
from scripts.operations.models import OperationError
from scripts.operations.monitoring import publication, scrape_rules


def command(args, *, data=None, timeout=120):
    result = subprocess.run(args, input=json.dumps(data).encode() if data else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError(args[0] + " failed: " + result.stderr.decode(errors="replace")[-3000:])
    return result.stdout.decode()


def crd(plural, singular, kind):
    return {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
            "metadata": {"name": plural + ".monitoring.coreos.com"},
            "spec": {"group": "monitoring.coreos.com", "scope": "Namespaced",
                     "names": {"plural": plural, "singular": singular, "kind": kind},
                     "versions": [{"name": "v1", "served": True, "storage": True,
                                   "schema": {"openAPIV3Schema": {"type": "object", "properties": {
                                       "spec": {"type": "object", "x-kubernetes-preserve-unknown-fields": True}}}}}]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, help="Absolute path to verified kind binary")
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != "atlas-monitor-e2e" or not Path(args.kind).is_absolute():
        parser.error("A verified absolute kind path and confirmation atlas-monitor-e2e are required")
    name = "atlas-monitor-" + uuid.uuid4().hex[:12]
    existing = command([args.kind, "get", "clusters"]).splitlines()
    if name in existing:
        raise RuntimeError("test cluster name already exists")
    temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="atlas-kind-test-", dir=temp_root) as directory:
        kubeconfig = str(Path(directory) / "kubeconfig")
        try:
            command([args.kind, "create", "cluster", "--name", name, "--image", "kindest/node:v1.36.1",
                     "--kubeconfig", kubeconfig, "--wait", "120s"], timeout=180)
            kubectl = ["kubectl", "--kubeconfig", kubeconfig]
            command(kubectl + ["create", "-f", "-"], data=crd("prometheuses", "prometheus", "Prometheus"))
            command(kubectl + ["create", "-f", "-"], data=crd("prometheusrules", "prometheusrule", "PrometheusRule"))
            command(kubectl + ["wait", "--for=condition=Established", "--timeout=60s",
                               "crd/prometheuses.monitoring.coreos.com", "crd/prometheusrules.monitoring.coreos.com"])
            command(kubectl + ["create", "namespace", "monitoring"])
            command(kubectl + ["create", "-f", "-"], data={"apiVersion": "monitoring.coreos.com/v1",
                    "kind": "Prometheus", "metadata": {"name": "stack", "namespace": "monitoring"},
                    "spec": {"ruleSelector": {"matchLabels": {"release": "stack"}}}})
            client = Kubernetes(kubeconfig)
            rule = scrape_rules.target_rules("monitoring", "stack", [{"job": "api", "instance": "cp1:6443"}],
                                              10, 120, "warning", "Target unavailable", "Check the scrape target.")
            expected = publication.capture(client, rule, "stack")
            result = publication.publish(client, rule, "stack", expected)
            if result.get("evaluation") != "unverified":
                raise RuntimeError("publication incorrectly claimed evaluation")
            previous = publication.capture(client, rule, "stack")
            command(kubectl + ["-n", "monitoring", "annotate", "prometheusrule", "atlas-scrape-targets", "test=drift"])
            try:
                publication.publish(client, rule, "stack", previous)
            except OperationError:
                pass
            else:
                raise RuntimeError("resource-version drift was not rejected")
            class Environment:
                def __init__(self, client):
                    self.client = client
                def verify(self, config):
                    # Synthetic lab node inventory; the host/SSH verification is tested separately.
                    return configuration.normalize(config), {}
            environment = Environment(client)
            operation_args = SimpleNamespace(lang="en", monitoring_namespace="monitoring", monitoring_release="stack",
                                   monitoring_prometheus_name="stack", monitoring_hold_seconds=10,
                                   monitoring_max_age=120, monitoring_severity="warning",
                                   monitoring_target=["api=cp1:6443"])
            params = monitoring_scrapes.from_arguments(operation_args, configuration.template(), environment)
            observation = monitoring_scrapes.observe(params, environment)
            plan = plans.make("monitoring-scrape-rules", client.cluster_identity(), observation, params)
            result = execution.execute(plan.record(), plan.id, ClusterCoordinator(client),
                lambda values: monitoring_scrapes.observe(values, environment),
                lambda values, checkpoint: monitoring_scrapes.apply(values, environment, checkpoint, plan.id),
                monitoring_scrapes.parameters)
            if result["result"]["evaluation"] != "unverified":
                raise RuntimeError("change plan incorrectly claimed evaluation")
            print("Real Kubernetes API: change plan, lock, rule write and drift rejection passed; Operator and host checks not tested.")
        finally:
            clusters = command([args.kind, "get", "clusters"]).splitlines()
            if name in clusters:
                command([args.kind, "delete", "cluster", "--name", name, "--kubeconfig", kubeconfig], timeout=120)
                if name in command([args.kind, "get", "clusters"]).splitlines():
                    raise RuntimeError("test cluster cleanup not verified")
            print("Temporary kind cluster cleanup verified.")


if __name__ == "__main__":
    main()
