# SPDX-License-Identifier: MIT
import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations import cli, configuration
from scripts.operations.models import OperationError
from scripts.operations.monitoring import longhorn_collection as collection, publication
from tests.test_monitoring_change_plan import Cluster


class Client:
    def __init__(self):
        self.prom = {"metadata": {"uid": "prom-1", "resourceVersion": "1"},
                     "spec": {"serviceMonitorSelector": {"matchLabels": {"release": "stack"}}}}
        self.service = {"metadata": {"name": "longhorn-backend", "uid": "service-1", "resourceVersion": "1",
                                     "labels": {"app": "longhorn-manager"}},
                        "spec": {"selector": {"app": "longhorn-manager"},
                                 "ports": [{"name": "manager", "port": 9500, "targetPort": "manager"}]}}
        self.objects = {}
        self.fail_kind = None
        self.extra_services = []
        self.policies = [{"metadata": {"name": "existing-isolation"},
                          "spec": {"podSelector": {"matchLabels": {"app": "longhorn-manager"}},
                                   "policyTypes": ["Ingress"]}}]
        self.version = 1

    def get(self, kind, name=None, namespace=None, optional=False):
        if kind == publication.PROMETHEUS:
            return copy.deepcopy(self.prom)
        if kind == "service":
            return {"items": copy.deepcopy([self.service, *self.extra_services])}
        if kind == collection.POLICY and name is None:
            return {"items": copy.deepcopy(self.policies)}
        return copy.deepcopy(self.objects.get((kind, namespace, name)))

    def create(self, value):
        kind = collection.POLICY if value["kind"] == "NetworkPolicy" else collection.MONITOR
        key = (kind, value["metadata"]["namespace"], value["metadata"]["name"])
        if self.fail_kind == kind or key in self.objects:
            return None
        self.version += 1
        value = copy.deepcopy(value)
        value["metadata"].update(uid="uid-" + str(self.version), resourceVersion=str(self.version))
        self.objects[key] = value
        return copy.deepcopy(value)

    def replace(self, value):
        kind = collection.POLICY if value["kind"] == "NetworkPolicy" else collection.MONITOR
        key = (kind, value["metadata"]["namespace"], value["metadata"]["name"])
        if self.fail_kind == kind or key not in self.objects or self.objects[key]["metadata"]["resourceVersion"] != value["metadata"]["resourceVersion"]:
            return None
        self.version += 1
        value = copy.deepcopy(value)
        value["metadata"]["resourceVersion"] = str(self.version)
        self.objects[key] = value
        return copy.deepcopy(value)


class LonghornCollectionTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.monitor, self.policy = collection.manifests("monitoring", "stack", "prometheus-stack", "longhorn-system")

    def capture(self):
        return collection.capture(self.client, self.monitor, self.policy, "prometheus-stack")

    def test_collection_scopes_both_namespace_and_pod_and_does_not_claim_success(self):
        expected = self.capture()
        steps = []
        result = collection.publish(self.client, self.monitor, self.policy, "prometheus-stack", expected, steps.append)
        self.assertEqual(result["evaluation"], "unverified")
        self.assertEqual(steps, ["longhorn-network-policy", "longhorn-service-monitor"])
        source = self.policy["spec"]["ingress"][0]["from"][0]
        self.assertEqual(source["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"], "monitoring")
        self.assertEqual(source["podSelector"]["matchLabels"]["prometheus"], "prometheus-stack")
        self.assertEqual(self.policy["spec"]["ingress"][0]["ports"], [{"protocol": "TCP", "port": 9500}])
        self.assertEqual(self.monitor["spec"]["endpoints"], [{"port": "manager", "path": "/metrics"}])

    def test_selector_service_and_foreign_object_refuse_to_write(self):
        for edit in (
            lambda: self.client.prom["spec"].update(serviceMonitorSelector=None),
            lambda: self.client.prom["spec"].update(serviceMonitorNamespaceSelector={"matchLabels": {"x": "y"}}),
            lambda: self.client.prom["spec"].update(ignoreNamespaceSelectors=True),
            lambda: self.client.service["spec"]["ports"][0].update(port=80),
            lambda: self.client.extra_services.append(copy.deepcopy(self.client.service)),
            lambda: self.client.objects.update({(collection.POLICY, "longhorn-system", "atlas-longhorn-prometheus"):
                                                {"metadata": {"uid": "foreign", "resourceVersion": "1"}, "spec": {}}}),
        ):
            self.client = Client()
            edit()
            with self.assertRaises(OperationError):
                self.capture()

    def test_new_policy_requires_existing_manager_ingress_isolation(self):
        self.client.policies = []
        with self.assertRaises(OperationError):
            self.capture()
        self.assertEqual(self.client.objects, {})
        self.client.policies = [{"metadata": {"name": "other"}, "spec": {
            "podSelector": {"matchLabels": {"app": "other"}}, "policyTypes": ["Ingress"]}}]
        with self.assertRaises(OperationError):
            self.capture()

    def test_drift_and_partial_write_are_not_reported_as_success(self):
        expected = self.capture()
        self.client.service["spec"]["selector"]["other"] = "changed"
        with self.assertRaises(OperationError):
            collection.publish(self.client, self.monitor, self.policy, "prometheus-stack", expected, lambda step: None)
        self.assertEqual(self.client.objects, {})
        self.client = Client()
        expected = self.capture()
        self.client.fail_kind = collection.MONITOR
        with self.assertRaises(OperationError):
            collection.publish(self.client, self.monitor, self.policy, "prometheus-stack", expected, lambda step: None)
        self.assertIn((collection.POLICY, "longhorn-system", "atlas-longhorn-prometheus"), self.client.objects)
        self.assertNotIn((collection.MONITOR, "monitoring", "atlas-longhorn-metrics"), self.client.objects)

    def test_cli_plan_apply_and_status_report_collection_unverified(self):
        class FullClient(Client, Cluster):
            def __init__(self):
                Cluster.__init__(self)
                Client.__init__(self)
            def get(self, kind, name=None, namespace=None, optional=False):
                if kind == "configmap":
                    return Cluster.get(self, kind, name, namespace, optional)
                return Client.get(self, kind, name, namespace, optional)
            def create(self, value):
                return Cluster.create(self, value) if value.get("kind") == "ConfigMap" else Client.create(self, value)
            def replace(self, value):
                return Cluster.replace(self, value) if value.get("kind") == "ConfigMap" else Client.replace(self, value)
        class Environment:
            def __init__(self):
                self.client = FullClient()
            def verify(self, config):
                return configuration.normalize(config), {}
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as directory, \
             patch("scripts.operations.commands.changes.environment", return_value=environment), \
             patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
             redirect_stdout(io.StringIO()) as output:
            code = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-longhorn-collection",
                             "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                             "--output-dir", directory])
            self.assertEqual(code, 0)
            plan = next((Path(directory) / "plans").glob("*.json"))
            plan_id = json.loads(plan.read_text())["id"]
            self.assertEqual(cli.main(["--lang", "zh_CN", "--change-apply", "--plan-file", str(plan),
                                       "--confirm", plan_id, "--output-dir", directory]), 2)
            self.assertEqual(cli.main(["--lang", "zh_CN", "--change-status", "--plan-id", plan_id,
                                       "--output-dir", directory]), 2)
            self.assertIn("实际指标仍未验证", output.getvalue())


if __name__ == "__main__":
    unittest.main()
