# SPDX-License-Identifier: MIT
import copy
from types import SimpleNamespace
import unittest

from scripts.operations import configuration
from scripts.operations.capabilities import monitoring_relay_metrics as capability
from scripts.operations.models import OperationError
from scripts.operations.monitoring import relay_metrics as metrics
from scripts.operations.monitoring.publication import SPEC_HASH
from scripts.operations.models import fingerprint
from scripts.operations.changes import plans


class Client:
    def __init__(self):
        self.prometheus = {"metadata": {"name": "stack", "namespace": "monitoring", "uid": "prom-1",
                                         "resourceVersion": "1"},
                           "spec": {"podMonitorSelector": {"matchLabels": {"release": "stack"}},
                                    "podMonitorNamespaceSelector": {},
                                    "ruleSelector": {"matchLabels": {"release": "stack"}},
                                    "ruleNamespaceSelector": {}}}
        self.objects = {}
        self.writes = []
        self.sequence = 0

    def get(self, kind, name, namespace, optional=False):
        if kind == metrics.PROMETHEUS:
            return copy.deepcopy(self.prometheus)
        return copy.deepcopy(self.objects.get((kind, namespace, name)))

    def create(self, value):
        return self._write(value, "create")

    def replace(self, value):
        return self._write(value, "replace")

    def _write(self, value, operation):
        self.sequence += 1
        item = copy.deepcopy(value)
        previous = self.objects.get((metrics.POD_MONITOR if item["kind"] == "PodMonitor" else metrics.RULE,
                                     item["metadata"]["namespace"], item["metadata"]["name"]))
        item["metadata"].update(uid=previous["metadata"]["uid"] if previous else "uid-" + str(self.sequence),
                                resourceVersion=str(self.sequence))
        kind = metrics.POD_MONITOR if item["kind"] == "PodMonitor" else metrics.RULE
        self.objects[(kind, item["metadata"]["namespace"], item["metadata"]["name"])] = item
        self.writes.append((operation, kind, copy.deepcopy(item)))
        return copy.deepcopy(item)


class Environment:
    def __init__(self, client):
        self.client = client

    def verify(self, value):
        return configuration.normalize(value), {}


class RelayMetricsChangeTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.environment = Environment(self.client)
        self.args = SimpleNamespace(lang="zh_CN", monitoring_namespace="monitoring",
            monitoring_release="stack", monitoring_prometheus_name="stack",
            monitoring_relay_auth_secret="relay-auth", monitoring_relay_auth_key="token")
        self.values = capability.from_arguments(self.args, configuration.template(), self.environment)

    def test_plan_publishes_selected_monitor_and_rule_without_claiming_evaluation(self):
        observation = capability.observe(self.values, self.environment)
        plan = plans.make("monitoring-relay-metrics", "test-cluster", observation, self.values)
        steps = []
        result = capability.apply(plan.parameters, self.environment, steps.append, plan.id)
        self.assertEqual(result["evaluation"], "unverified")
        self.assertEqual(steps, ["monitoring-relay-metrics", "monitoring-relay-pod-monitor",
                                 "monitoring-relay-delivery-rule"])
        monitor = self.client.objects[(metrics.POD_MONITOR, "monitoring", metrics.MONITOR_NAME)]
        rule = self.client.objects[(metrics.RULE, "monitoring", metrics.RULE_NAME)]
        self.assertEqual(monitor["spec"]["podMetricsEndpoints"][0]["authorization"]["credentials"],
                         {"name": "relay-auth", "key": "token"})
        self.assertEqual(rule["metadata"]["annotations"][SPEC_HASH], fingerprint(rule["spec"]))
        self.assertEqual(len(self.client.writes), 2)

    def test_mismatched_selector_and_plan_drift_fail_before_writing(self):
        self.client.prometheus["spec"]["podMonitorSelector"] = {"matchLabels": {"release": "other"}}
        with self.assertRaises(OperationError):
            capability.from_arguments(self.args, configuration.template(), self.environment)
        self.assertEqual(self.client.writes, [])
        self.client.prometheus["spec"]["podMonitorSelector"] = {"matchLabels": {"release": "stack"}}
        self.client.prometheus["metadata"]["resourceVersion"] = "2"
        with self.assertRaises(OperationError):
            capability.apply(self.values, self.environment, lambda _: None, "plan")
        self.assertEqual(self.client.writes, [])

    def test_existing_unowned_resource_is_never_overwritten(self):
        self.client.objects[(metrics.POD_MONITOR, "monitoring", metrics.MONITOR_NAME)] = {
            "metadata": {"name": metrics.MONITOR_NAME, "namespace": "monitoring", "uid": "foreign",
                         "resourceVersion": "1", "labels": {"release": "other"}}, "spec": {}}
        with self.assertRaises(OperationError):
            capability.from_arguments(self.args, configuration.template(), self.environment)
        self.assertEqual(self.client.writes, [])


if __name__ == "__main__":
    unittest.main()
