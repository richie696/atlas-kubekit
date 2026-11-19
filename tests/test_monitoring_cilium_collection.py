# SPDX-License-Identifier: MIT
import copy
from types import SimpleNamespace
import unittest

from scripts.operations import configuration
from scripts.operations.capabilities import monitoring_cilium as capability
from scripts.operations.changes import plans
from scripts.operations.models import OperationError
from scripts.operations.monitoring import cilium_metrics as metrics
from scripts.operations.monitoring.publication import SPEC_HASH
from scripts.operations.models import fingerprint


class Client:
    def __init__(self):
        self.prometheus = {"metadata": {"namespace": "monitoring", "uid": "prom-1", "resourceVersion": "1"},
                           "spec": {"podMonitorSelector": {"matchLabels": {"release": "stack"}},
                                    "podMonitorNamespaceSelector": {},
                                    "ruleSelector": {"matchLabels": {"release": "stack"}},
                                    "ruleNamespaceSelector": {}}}
        self.ds = {"spec": {"template": {"metadata": {"annotations": {
            "prometheus.io/scrape": "true", "prometheus.io/port": "9962"}},
            "spec": {"containers": [{"name": "cilium-agent", "ports": [{"containerPort": 9962}]}]}}}}
        self.deployment = {"spec": {"template": {"metadata": {"annotations": {
            "prometheus.io/scrape": "true", "prometheus.io/port": "9963"}},
            "spec": {"containers": [{"name": "cilium-operator", "ports": [{"containerPort": 9963}]}]}}}}
        self.objects, self.writes, self.sequence = {}, [], 0

    def get(self, kind, name, namespace, optional=False):
        if kind == metrics.PROMETHEUS:
            return copy.deepcopy(self.prometheus)
        if kind == "daemonset":
            return copy.deepcopy(self.ds)
        if kind == "deployment":
            return copy.deepcopy(self.deployment)
        return copy.deepcopy(self.objects.get((kind, namespace, name)))

    def create(self, value):
        return self._write(value, "create")

    def replace(self, value):
        return self._write(value, "replace")

    def _write(self, value, action):
        self.sequence += 1
        item = copy.deepcopy(value)
        kind = metrics.POD_MONITOR if item["kind"] == "PodMonitor" else metrics.RULE
        key = (kind, item["metadata"]["namespace"], item["metadata"]["name"])
        previous = self.objects.get(key)
        item["metadata"].update(uid=previous["metadata"]["uid"] if previous else f"uid-{self.sequence}",
                                resourceVersion=str(self.sequence))
        self.objects[key] = item
        self.writes.append((action, key, item))
        return copy.deepcopy(item)


class Environment:
    def __init__(self, client):
        self.client = client

    def verify(self, value):
        return configuration.normalize(value), {}


class CiliumMonitoringCollectionTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.environment = Environment(self.client)
        self.args = SimpleNamespace(lang="zh_CN", monitoring_namespace="monitoring",
            monitoring_release="stack", monitoring_prometheus_name="stack",
            monitoring_hold_seconds=120, monitoring_notification_language="zh_CN",
            monitoring_cilium_ipam_percent=80, monitoring_cilium_bpf_pressure_percent=80)
        self.values = capability.from_arguments(self.args, configuration.template(), self.environment)

    def test_change_plan_writes_owned_monitors_and_rules(self):
        observed = capability.observe(self.values, self.environment)
        plan = plans.make("monitoring-cilium-collection", "test-cluster", observed, self.values)
        checkpoints = []
        result = capability.apply(plan.parameters, self.environment, checkpoints.append, plan.id)
        self.assertEqual(result["evaluation"], "unverified")
        self.assertEqual(checkpoints, ["monitoring-cilium-agent", "monitoring-cilium-operator", "monitoring-cilium-rule"])
        agent = self.client.objects[(metrics.POD_MONITOR, "monitoring", metrics.AGENT_MONITOR)]
        operator = self.client.objects[(metrics.POD_MONITOR, "monitoring", metrics.OPERATOR_MONITOR)]
        rule = self.client.objects[(metrics.RULE, "monitoring", metrics.RULE_NAME)]
        self.assertEqual(agent["spec"]["podMetricsEndpoints"][0]["portNumber"], 9962)
        self.assertEqual(operator["spec"]["podMetricsEndpoints"][0]["portNumber"], 9963)
        self.assertEqual(rule["metadata"]["annotations"][SPEC_HASH], fingerprint(rule["spec"]))
        alerts = {item["alert"]: item for group in rule["spec"]["groups"] for item in group["rules"]}
        self.assertEqual(set(alerts), {
            "AtlasCiliumAgentOrTelemetryMissing", "AtlasCiliumOperatorTelemetryUnavailable",
            "AtlasCiliumIPAMTelemetryMissing", "AtlasCiliumNodeIPAMCapacityLow",
            "AtlasCiliumBPFMapPressureHigh", "AtlasCiliumNodeConnectivityDegraded"})
        self.assertIn(">= 80", alerts["AtlasCiliumNodeIPAMCapacityLow"]["expr"])
        self.assertIn(">= 80", alerts["AtlasCiliumBPFMapPressureHigh"]["expr"])
        self.assertIn("{{ $labels.node }}", alerts["AtlasCiliumBPFMapPressureHigh"]["annotations"]["description"])
        self.assertEqual(len(self.client.writes), 3)

    def test_rejects_disabled_metrics_and_incompatible_prometheus_selector(self):
        self.client.ds["spec"]["template"]["metadata"]["annotations"]["prometheus.io/scrape"] = "false"
        with self.assertRaises(OperationError):
            capability.from_arguments(self.args, configuration.template(), self.environment)
        self.client.ds["spec"]["template"]["metadata"]["annotations"]["prometheus.io/scrape"] = "true"
        self.client.prometheus["spec"]["podMonitorSelector"] = {"matchLabels": {"release": "other"}}
        with self.assertRaises(OperationError):
            capability.from_arguments(self.args, configuration.template(), self.environment)
        self.assertEqual(self.client.writes, [])

    def test_rejects_out_of_range_ipam_and_bpf_thresholds_without_writes(self):
        for field in ("monitoring_cilium_ipam_percent", "monitoring_cilium_bpf_pressure_percent"):
            for threshold in (0, 100, True):
                with self.subTest(field=field, threshold=threshold):
                    setattr(self.args, field, threshold)
                    with self.assertRaises(OperationError):
                        capability.from_arguments(self.args, configuration.template(), self.environment)
                    self.assertEqual(self.client.writes, [])
                    setattr(self.args, field, 80)


if __name__ == "__main__":
    unittest.main()
