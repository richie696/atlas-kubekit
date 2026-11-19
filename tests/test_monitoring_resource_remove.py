# SPDX-License-Identifier: MIT
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from scripts.operations import configuration
from scripts.operations.capabilities import monitoring_resource_remove as remove
from scripts.operations.models import OperationError, fingerprint


def owned_rule(resource_version="9"):
    spec = {"groups": [{"name": "atlas.test", "rules": [{"alert": "AtlasTest", "expr": "vector(1)"}]}]}
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": "atlas-test", "namespace": "monitoring", "uid": "uid-test",
                         "resourceVersion": resource_version,
                         "labels": {"atlas-kubekit.io/managed-by": "monitoring"},
                         "annotations": {"atlas-kubekit.io/spec-sha256": fingerprint(spec)}},
            "spec": spec}


class MonitoringResourceRemoveTest(unittest.TestCase):
    def setUp(self):
        self.configuration = configuration.template()
        self.client = Mock()
        self.environment = SimpleNamespace(client=self.client, verify=Mock(return_value=(self.configuration, None)))
        self.args = SimpleNamespace(monitoring_resource_kind="prometheusrule",
                                    monitoring_resource_namespace="monitoring",
                                    monitoring_resource_name="atlas-test")

    def test_plan_observes_only_matching_atlas_owned_spec(self):
        fixture = owned_rule()
        self.client.get.return_value = fixture
        values = remove.from_arguments(self.args, self.configuration, self.environment)
        self.assertEqual(values["expected"]["uid"], "uid-test")
        self.assertEqual(values["expected"]["resourceVersion"], "9")
        self.assertEqual(remove.observe(values, self.environment)["target"], values["expected"])

    def test_unowned_resource_is_rejected_before_plan_creation(self):
        fixture = owned_rule()
        fixture["metadata"]["labels"].clear()
        self.client.get.return_value = fixture
        with self.assertRaises(OperationError) as raised:
            remove.from_arguments(self.args, self.configuration, self.environment)
        self.assertEqual(raised.exception.key, "ops.change.foreign_resource")

    def test_spec_hash_mismatch_is_rejected(self):
        fixture = owned_rule()
        fixture["spec"]["groups"][0]["rules"][0]["expr"] = "vector(0)"
        self.client.get.return_value = fixture
        with self.assertRaises(OperationError) as raised:
            remove.from_arguments(self.args, self.configuration, self.environment)
        self.assertEqual(raised.exception.key, "ops.change.foreign_resource")

    def test_resource_version_drift_stops_apply_before_delete(self):
        fixture = owned_rule("10")
        values = remove.parameters({"configuration": self.configuration, "kind": "prometheusrule",
                                    "namespace": "monitoring", "name": "atlas-test",
                                    "expected": {"uid": "uid-test", "resourceVersion": "9",
                                                  "specHash": fingerprint(fixture["spec"])}})
        self.client.get.return_value = fixture
        with self.assertRaises(OperationError) as raised:
            remove.observe(values, self.environment)
        self.assertEqual(raised.exception.key, "ops.change.stale")
        self.client.delete_monitoring_resource.assert_not_called()

    def test_apply_deletes_with_expected_identity_and_verifies_absence(self):
        fixture = owned_rule()
        values = remove.parameters({"configuration": self.configuration, "kind": "prometheusrule",
                                    "namespace": "monitoring", "name": "atlas-test",
                                    "expected": {"uid": "uid-test", "resourceVersion": "9",
                                                  "specHash": fingerprint(fixture["spec"])}})
        self.client.delete_monitoring_resource.return_value = {"kind": "Status", "status": "Success"}
        self.client.get.return_value = None
        result = remove.apply(values, self.environment, Mock(), "run-id")
        self.client.delete_monitoring_resource.assert_called_once_with(
            "prometheusrule", "monitoring", "atlas-test", "uid-test", "9")
        self.assertEqual(result["state"], "resource-removed")

    def test_invalid_kind_and_cluster_scope_are_rejected(self):
        with self.assertRaises(OperationError):
            remove.parameters({"configuration": self.configuration, "kind": "secret", "namespace": "monitoring",
                               "name": "credential", "expected": {"uid": "u", "resourceVersion": "1",
                                                                       "specHash": "0" * 64}})
        with self.assertRaises(OperationError):
            remove.parameters({"configuration": self.configuration, "kind": "prometheusrule", "namespace": "*",
                               "name": "atlas-test", "expected": {"uid": "uid-test", "resourceVersion": "1",
                                                                        "specHash": "0" * 64}})


if __name__ == "__main__":
    unittest.main()
