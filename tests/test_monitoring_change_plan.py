# SPDX-License-Identifier: MIT
import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.operations import cli, configuration
from scripts.operations.capabilities import monitoring_rules
from scripts.operations.changes import execution, plans
from scripts.operations.changes.coordinator import ClusterCoordinator
from scripts.operations.models import OperationError
from tests.test_monitoring_publication import Client


class Cluster(Client):
    def __init__(self):
        super().__init__()
        self.maps = {}
        self.serial = 0

    def cluster_identity(self):
        return "cluster-12345678"

    def get(self, kind, name=None, namespace=None, optional=False):
        if kind == "configmap":
            return copy.deepcopy(self.maps.get((namespace, name)))
        return super().get(kind, name, namespace, optional)

    def create(self, value):
        if value.get("kind") != "ConfigMap":
            return super().create(value)
        key = (value["metadata"]["namespace"], value["metadata"]["name"])
        if key in self.maps:
            return None
        self.serial += 1
        value = copy.deepcopy(value)
        value["metadata"]["resourceVersion"] = str(self.serial)
        self.maps[key] = value
        return copy.deepcopy(value)

    def replace(self, value):
        if value.get("kind") != "ConfigMap":
            return super().replace(value)
        key = (value["metadata"]["namespace"], value["metadata"]["name"])
        previous = self.maps.get(key)
        if previous is None or previous["metadata"]["resourceVersion"] != value["metadata"]["resourceVersion"]:
            return None
        self.serial += 1
        value = copy.deepcopy(value)
        value["metadata"]["resourceVersion"] = str(self.serial)
        self.maps[key] = value
        return copy.deepcopy(value)


class Environment:
    def __init__(self):
        self.client = Cluster()

    def verify(self, config):
        return configuration.normalize(config), {"cp1": {}}


class MonitoringChangeTest(unittest.TestCase):
    def setUp(self):
        self.environment = Environment()
        self.args = SimpleNamespace(lang="en", monitoring_namespace="monitoring", monitoring_release="stack",
                                    monitoring_prometheus_name="prometheus-stack", monitoring_hold_seconds=10,
                                    monitoring_max_age=120, monitoring_severity="warning",
                                    monitoring_notification_language="zh_CN", monitoring_node_cpu_percent=90,
                                    monitoring_node_memory_percent=90, monitoring_node_filesystem_percent=85,
                                    monitoring_node_inode_percent=85)
        self.params = monitoring_rules.from_arguments(self.args, configuration.template(), self.environment)

    def test_alert_annotations_follow_notification_language_not_cli_language(self):
        self.assertEqual(self.params["summary"], "节点未就绪或监控数据不可用")
        self.assertEqual(self.params["description"], "请检查节点和指标来源；需要新鲜的就绪样本才能判定恢复。")

    def plan(self):
        observation = monitoring_rules.observe(self.params, self.environment)
        return plans.make("monitoring-node-rules", self.environment.client.cluster_identity(),
                          observation, self.params)

    def execute(self, plan):
        return execution.execute(plan.record(), plan.id, ClusterCoordinator(self.environment.client),
                                 lambda values: monitoring_rules.observe(values, self.environment),
                                 lambda values, checkpoint: monitoring_rules.apply(values, self.environment, checkpoint, plan.id),
                                 monitoring_rules.parameters)

    def test_plan_apply_and_idempotent_replay_preserve_unverified_evaluation(self):
        plan = self.plan()
        result = self.execute(plan)
        self.assertEqual(result["result"]["evaluation"], "unverified")
        self.assertEqual(self.environment.client.rule["metadata"]["labels"]["release"], "stack")
        revision = self.environment.client.rule["metadata"]["resourceVersion"]
        self.assertEqual(self.execute(plan)["state"], "already-succeeded")
        self.assertEqual(self.environment.client.rule["metadata"]["resourceVersion"], revision)

    def test_drift_is_rejected_before_writing(self):
        plan = self.plan()
        self.environment.client.prom["spec"]["replicas"] = 2
        with self.assertRaises(OperationError):
            self.execute(plan)
        self.assertIsNone(self.environment.client.rule)

    def test_cli_plan_apply_and_status_all_preserve_evaluation_boundary(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=self.environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-node-rules",
                                   "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                                   "--monitoring-hold-seconds", "10", "--output-dir", temp])
                self.assertEqual(result, 0)
                plan_file = next((Path(temp) / "plans").glob("*.json"))
                plan_id = json.loads(plan_file.read_text())["id"]
                result = cli.main(["--lang", "zh_CN", "--change-apply", "--plan-file", str(plan_file),
                                   "--confirm", plan_id, "--output-dir", temp])
                self.assertEqual(result, 2)
                result = cli.main(["--lang", "zh_CN", "--change-status", "--plan-id", plan_id, "--output-dir", temp])
                self.assertEqual(result, 2)
            self.assertIn("求值和通知送达仍未验证", output.getvalue())


if __name__ == "__main__":
    unittest.main()
