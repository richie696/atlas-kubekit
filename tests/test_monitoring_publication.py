# SPDX-License-Identifier: MIT
import copy
import unittest

from scripts.operations.models import OperationError
from scripts.operations.monitoring import publication, rules


class Client:
    def __init__(self):
        self.prom = {"metadata": {"uid": "p1", "resourceVersion": "10"},
                     "spec": {"ruleSelector": {"matchLabels": {"release": "stack"}},
                              "ruleNamespaceSelector": {}}}
        self.rule = None
        self.version = 0
        self.reject_write = False

    def get(self, kind, name, namespace=None, optional=False):
        self.assert_namespace = namespace
        return copy.deepcopy(self.prom if kind == publication.PROMETHEUS else self.rule)

    def create(self, value):
        if self.rule is not None or self.reject_write:
            return None
        self.version += 1
        self.rule = copy.deepcopy(value)
        self.rule["metadata"].update(uid="rule-uid", resourceVersion=str(self.version))
        return copy.deepcopy(self.rule)

    def replace(self, value):
        if (self.reject_write or self.rule is None or
                self.rule["metadata"]["resourceVersion"] != value["metadata"]["resourceVersion"]):
            return None
        self.version += 1
        self.rule = copy.deepcopy(value)
        self.rule["metadata"]["resourceVersion"] = str(self.version)
        return copy.deepcopy(self.rule)


class MonitoringPublicationTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.rule = rules.node_readiness_rule("monitoring", "stack", ["cp1"], 10, 120,
                                              "warning", "Node state unavailable", "Check source and node.")

    def test_create_then_update_owned_object_uses_cas_and_preserves_metadata(self):
        before = publication.capture(self.client, self.rule, "prometheus-stack")
        result = publication.publish(self.client, self.rule, "prometheus-stack", before)
        self.assertEqual(result["evaluation"], "unverified")
        self.assertEqual(self.client.assert_namespace, "monitoring")
        self.client.rule["metadata"]["annotations"]["external-note"] = "keep"
        before = publication.capture(self.client, self.rule, "prometheus-stack")
        updated = rules.node_readiness_rule("monitoring", "stack", ["cp1", "w1"], 10, 120,
                                           "warning", "Node state unavailable", "Check source and node.")
        result = publication.publish(self.client, updated, "prometheus-stack", before)
        self.assertEqual(result["state"], "resource-written")
        self.assertEqual(self.client.rule["metadata"]["annotations"]["external-note"], "keep")
        self.assertEqual(len(self.client.rule["spec"]["groups"][0]["rules"]), 2)

    def test_foreign_or_manually_changed_rule_is_never_overwritten(self):
        self.client.rule = {"metadata": {"uid": "foreign", "resourceVersion": "1"}, "spec": {}}
        with self.assertRaises(OperationError):
            publication.capture(self.client, self.rule, "prometheus-stack")
        self.client.rule = None
        before = publication.capture(self.client, self.rule, "prometheus-stack")
        publication.publish(self.client, self.rule, "prometheus-stack", before)
        self.client.rule["spec"]["groups"] = []
        with self.assertRaises(OperationError):
            publication.capture(self.client, self.rule, "prometheus-stack")

    def test_stale_state_and_write_conflict_are_rejected(self):
        before = publication.capture(self.client, self.rule, "prometheus-stack")
        self.client.prom["spec"]["replicas"] = 2
        with self.assertRaises(OperationError):
            publication.publish(self.client, self.rule, "prometheus-stack", before)
        self.assertIsNone(self.client.rule)
        before = publication.capture(self.client, self.rule, "prometheus-stack")
        self.client.reject_write = True
        with self.assertRaises(OperationError):
            publication.publish(self.client, self.rule, "prometheus-stack", before)
        self.assertIsNone(self.client.rule)

    def test_selector_and_namespace_constraints_are_checked(self):
        for selector in (None, {"matchLabels": {"release": "other"}},
                         {"matchExpressions": [{"key": "release", "operator": "Exists"}]}):
            self.client.prom["spec"]["ruleSelector"] = selector
            with self.subTest(selector=selector), self.assertRaises(OperationError):
                publication.capture(self.client, self.rule, "prometheus-stack")
        self.client.prom["spec"]["ruleSelector"] = {}
        self.client.prom["spec"]["ruleNamespaceSelector"] = {"matchLabels": {"team": "ops"}}
        with self.assertRaises(OperationError):
            publication.capture(self.client, self.rule, "prometheus-stack")

    def test_prometheus_drift_after_write_is_reported_without_claiming_success(self):
        class DriftingClient(Client):
            def create(self, value):
                result = super().create(value)
                self.prom["spec"]["replicas"] = 2
                return result
        client = DriftingClient()
        before = publication.capture(client, self.rule, "prometheus-stack")
        with self.assertRaises(OperationError):
            publication.publish(client, self.rule, "prometheus-stack", before)

    def test_prometheus_status_updates_do_not_invalidate_plan(self):
        class UpdatingStatusClient(Client):
            def create(self, value):
                result = super().create(value)
                self.prom["metadata"]["resourceVersion"] = "11"
                self.prom["status"] = {"availableReplicas": 1}
                self.rule["metadata"]["resourceVersion"] = "12"
                self.rule["status"] = {"observedGeneration": 1}
                return result
        client = UpdatingStatusClient()
        before = publication.capture(client, self.rule, "prometheus-stack")
        result = publication.publish(client, self.rule, "prometheus-stack", before)
        self.assertEqual(result["evaluation"], "unverified")
        self.assertIsNotNone(client.rule)

    def test_controller_metadata_update_does_not_stale_rule_plan(self):
        before = publication.capture(self.client, self.rule, "prometheus-stack")
        publication.publish(self.client, self.rule, "prometheus-stack", before)
        before = publication.capture(self.client, self.rule, "prometheus-stack")
        self.client.rule["metadata"].setdefault("annotations", {})["prometheus-operator-validated"] = "true"
        self.client.rule["metadata"]["resourceVersion"] = "operator-update"
        result = publication.publish(self.client, self.rule, "prometheus-stack", before)
        self.assertEqual(result["state"], "resource-written")
        self.assertEqual(self.client.rule["metadata"]["annotations"]["prometheus-operator-validated"], "true")

    def test_identical_desired_rule_is_not_replaced(self):
        expected = publication.capture(self.client, self.rule, "prometheus-stack")
        publication.publish(self.client, self.rule, "prometheus-stack", expected)
        expected = publication.capture(self.client, self.rule, "prometheus-stack")
        resource_version = self.client.rule["metadata"]["resourceVersion"]
        publication.publish(self.client, self.rule, "prometheus-stack", expected)
        self.assertEqual(self.client.rule["metadata"]["resourceVersion"], resource_version)

    def test_replace_conflict_retries_when_only_controller_metadata_changed(self):
        class ConflictingClient(Client):
            conflicts = 0
            def replace(self, value):
                if self.conflicts == 0:
                    self.conflicts += 1
                    self.rule["metadata"]["annotations"]["prometheus-operator-validated"] = "true"
                    self.rule["metadata"]["resourceVersion"] = "controller-update"
                    return None
                return super().replace(value)
        client = ConflictingClient()
        publication.publish(client, self.rule, "prometheus-stack",
                            publication.capture(client, self.rule, "prometheus-stack"))
        before = publication.capture(client, self.rule, "prometheus-stack")
        updated = rules.node_readiness_rule("monitoring", "stack", ["cp1", "w1"], 10, 120,
                                             "warning", "Node state unavailable", "Check source and node.")
        result = publication.publish(client, updated, "prometheus-stack", before)
        self.assertEqual(result["state"], "resource-written")
        self.assertEqual(client.conflicts, 1)
        self.assertEqual(client.rule["metadata"]["annotations"]["prometheus-operator-validated"], "true")

    def test_changed_selector_label_is_not_silently_repaired(self):
        before = publication.capture(self.client, self.rule, "prometheus-stack")
        publication.publish(self.client, self.rule, "prometheus-stack", before)
        self.client.rule["metadata"]["labels"]["release"] = "other"
        with self.assertRaises(OperationError):
            publication.capture(self.client, self.rule, "prometheus-stack")


if __name__ == "__main__":
    unittest.main()
