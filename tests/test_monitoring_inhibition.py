# SPDX-License-Identifier: MIT
import copy
import unittest

from scripts.operations import configuration
from scripts.operations.capabilities import monitoring_inhibition
from scripts.operations.models import OperationError
from scripts.operations.monitoring import inhibition, publication


class Client:
    def __init__(self):
        self.manager = {"metadata": {"uid": "manager", "resourceVersion": "4"}, "spec": {
            "version": "v0.35.0", "alertmanagerConfigSelector": {"matchLabels": {
                "release": "stack", publication.OWNER: "monitoring"}},
            "alertmanagerConfigMatcherStrategy": {"type": "OnNamespaceExceptForAlertmanagerNamespace"}}}
        self.current = None
        self.calls = []

    def get(self, kind, name, namespace, optional=False):
        return copy.deepcopy(self.manager if kind == monitoring_inhibition.ALERTMANAGER else self.current)

    def create(self, value):
        self.current = copy.deepcopy(value)
        self.current["metadata"].update(uid="config", resourceVersion="1")
        return copy.deepcopy(self.current)

    def replace(self, value):
        self.current = copy.deepcopy(value)
        self.current["metadata"]["resourceVersion"] = "2"
        return copy.deepcopy(self.current)

    def call(self, args, value=None, **_kwargs):
        self.calls.append((list(args), copy.deepcopy(value)))
        return {"kind": value["kind"]}


class Environment:
    def __init__(self):
        self.client = Client()

    def verify(self, value):
        return configuration.normalize(value), {}


def values(client, enabled=True):
    policy = inhibition.normalize(enabled, "critical", "warning", None)
    raw = {"namespace": "monitoring", "release": "stack", "managerName": "stack-alertmanager",
           "version": "0.35.0", "policy": policy}
    desired = inhibition.manifest(raw["namespace"], raw["release"], policy)
    existing = client.get(monitoring_inhibition.CONFIG, desired["metadata"]["name"], "monitoring", optional=True)
    raw["expected"] = {"manager": {"uid": "manager", "specHash": publication.fingerprint(client.manager["spec"])},
                        "config": publication._identity(existing)}
    return {"configuration": configuration.template(), **raw}


class MonitoringInhibitionTest(unittest.TestCase):
    def test_only_critical_can_suppress_warning_or_info_with_identity_labels(self):
        for args in (("warning", "info", ["cluster", "alertname"]),
                     ("critical", "critical", ["cluster", "alertname"]),
                     ("critical", "warning", ["cluster", "namespace"]),
                     ("critical", "warning", ["namespace", "alertname"])):
            with self.subTest(args=args), self.assertRaises(ValueError):
                inhibition.normalize(True, *args)
        self.assertFalse(inhibition.normalize(False, "critical", "warning", None)["enabled"])

    def test_manifest_has_nonrouting_guard_and_explicit_severity_labels(self):
        policy = inhibition.normalize(True, "critical", "warning", ["cluster", "namespace", "alertname"])
        rendered = inhibition.manifest("monitoring", "stack", policy)
        self.assertEqual(rendered["spec"]["inhibitRules"], [{
            "sourceMatch": [{"name": "severity", "value": "critical", "matchType": "="}],
            "targetMatch": [{"name": "severity", "value": "warning", "matchType": "="}],
            "equal": ["cluster", "namespace", "alertname"],
        }])
        self.assertEqual(rendered["spec"]["route"]["matchers"], [
            {"name": "atlas_kubekit_policy", "value": "never-match", "matchType": "="}])
        disabled = inhibition.manifest("monitoring", "stack", inhibition.normalize(False, "critical", "warning", None))
        self.assertEqual(disabled["spec"]["inhibitRules"], [])

    def test_plan_publish_is_owned_and_revalidated(self):
        environment = Environment()
        raw = values(environment.client)
        plan = monitoring_inhibition.parameters(raw)
        observation = monitoring_inhibition.observe(plan, environment)
        self.assertEqual(observation["desiredSpecHash"],
                         publication.fingerprint(inhibition.manifest("monitoring", "stack", plan["policy"])["spec"]))
        result = monitoring_inhibition.apply(plan, environment, lambda _step: None, "a" * 64)
        self.assertEqual(result["state"], "resource-written")
        self.assertEqual(environment.client.current["metadata"]["name"], "atlas-inhibition-policy")
        self.assertEqual(len(environment.client.current["spec"]["inhibitRules"]), 1)
        self.assertEqual(environment.client.calls[0][0], ["apply", "--dry-run=server", "-f", "-"])
        with self.assertRaises(OperationError):
            monitoring_inhibition.apply(plan, environment, lambda _step: None, "b" * 64)


if __name__ == "__main__":
    unittest.main()
