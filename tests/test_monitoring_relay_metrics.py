# SPDX-License-Identifier: MIT
import copy
import unittest

from scripts.operations.models import OperationError, fingerprint
from scripts.operations.monitoring import relay_metrics as metrics


class RelayMetricsTest(unittest.TestCase):
    def setUp(self):
        self.resources = metrics.resources("monitoring", "stack", "stack", "relay-auth", "token",
                                           "通知发送失败", "请检查机器人目标和转发服务状态。")
        self.prometheus = {"metadata": {"name": "stack", "namespace": "monitoring", "uid": "p-uid",
                                         "resourceVersion": "1"},
                           "spec": {"podMonitorSelector": {"matchLabels": {"release": "stack"}},
                                    "podMonitorNamespaceSelector": {},
                                    "ruleSelector": {"matchLabels": {"release": "stack"}},
                                    "ruleNamespaceSelector": {}}}

    def test_manifest_scrapes_pod_with_bearer_secret_and_alerts_delivery_failures(self):
        monitor, rule = self.resources["podMonitor"], self.resources["rule"]
        self.assertEqual(monitor["spec"]["selector"]["matchLabels"], {metrics.POD_LABEL: "enabled"})
        endpoint = monitor["spec"]["podMetricsEndpoints"][0]
        self.assertEqual((endpoint["port"], endpoint["path"], endpoint["interval"]),
                         (metrics.METRICS_PORT_NAME, "/metrics", "30s"))
        self.assertEqual(endpoint["authorization"], {"type": "Bearer", "credentials":
                         {"name": "relay-auth", "key": "token"}})
        alert = rule["spec"]["groups"][0]["rules"][0]
        self.assertEqual(alert["alert"], metrics.ALERT_NAME)
        self.assertIn('outcome=~"unknown|retryable|rejected"', alert["expr"])
        self.assertEqual(alert["labels"]["risk_id"], "R-OBS-03")
        self.assertNotIn("actual-bearer-secret", repr(self.resources))

    def test_prometheus_selector_must_select_monitor_and_rule(self):
        class Client:
            def get(self, kind, name, namespace, optional=False):
                return copy.deepcopy(self.prometheus)
        client = Client()
        client.prometheus = self.prometheus
        identity = metrics.selected_prometheus(client, "stack", "monitoring",
                                               self.resources["podMonitor"], self.resources["rule"])
        self.assertEqual(identity, {"uid": "p-uid", "resourceVersion": "1"})
        for field, value in (("podMonitorSelector", {"matchLabels": {"release": "other"}}),
                             ("ruleSelector", {"matchExpressions": [{"key": "release", "operator": "Exists"}]}),
                             ("podMonitorNamespaceSelector", {"matchLabels": {"team": "ops"}})):
            client.prometheus["spec"][field] = value
            with self.subTest(field=field), self.assertRaises(OperationError):
                metrics.selected_prometheus(client, "stack", "monitoring",
                                            self.resources["podMonitor"], self.resources["rule"])
            client.prometheus["spec"] = copy.deepcopy(self.prometheus["spec"])

    def test_capture_rejects_foreign_or_untracked_resources(self):
        class Client:
            def __init__(self):
                self.resources = {}
            def get(self, kind, name, namespace, optional=False):
                if kind == metrics.PROMETHEUS:
                    return copy.deepcopy(self.prometheus)
                return copy.deepcopy(self.resources.get((kind, namespace, name)))
        client = Client()
        client.prometheus = self.prometheus
        before = metrics.capture(client, "monitoring", "stack", self.resources["podMonitor"],
                                 self.resources["rule"])
        self.assertEqual(before, {"prometheus": {"uid": "p-uid", "resourceVersion": "1"},
                                  "monitor": None, "rule": None})
        rule = copy.deepcopy(self.resources["rule"])
        rule["metadata"].update(uid="rule-uid", resourceVersion="2",
                                annotations={metrics.SPEC_HASH: fingerprint(rule["spec"])})
        client.resources[(metrics.RULE, "monitoring", metrics.RULE_NAME)] = rule
        captured = metrics.capture(client, "monitoring", "stack", self.resources["podMonitor"],
                                   self.resources["rule"])
        self.assertEqual(captured["rule"], {"uid": "rule-uid", "resourceVersion": "2",
                                             "specHash": fingerprint(rule["spec"])})
        rule["metadata"]["labels"][metrics.OWNER] = "another-tool"
        with self.assertRaises(OperationError):
            metrics.capture(client, "monitoring", "stack", self.resources["podMonitor"],
                            self.resources["rule"])

    def test_manifest_rejects_invalid_credentials_and_untrusted_rule_text(self):
        with self.assertRaises(ValueError):
            metrics.resources("monitoring", "stack", "stack", "relay-auth", "bad auth",
                              "summary", "description")
        with self.assertRaises(ValueError):
            metrics.resources("monitoring", "stack", "stack", "relay-auth", "token",
                              "{{ .ExternalURL }}", "description")


if __name__ == "__main__":
    unittest.main()
