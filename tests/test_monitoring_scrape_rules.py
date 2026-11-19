# SPDX-License-Identifier: MIT
import unittest
from scripts.operations.monitoring.scrape_rules import target_rules, control_plane_rules
from scripts.operations.capabilities.monitoring_scrapes import _parse_target
from scripts.operations.models import OperationError


class ScrapeRuleTest(unittest.TestCase):
    def render(self, targets):
        return target_rules("monitoring", "stack", targets, 30, 120, "warning", "Scrape unavailable", "Check target and network.")

    def test_escaped_selector_and_stable_order(self):
        rules = self.render([{"job": 'z"job', "instance": "a:80"}, {"job": "a", "instance": "b"}])["spec"]["groups"][0]["rules"]
        self.assertEqual(rules[0]["labels"]["job"], "a")
        self.assertIn('job="z\\"job"', rules[1]["expr"])
        self.assertEqual(rules[1]["labels"]["risk_id"], "R-OBS-01")

    def test_duplicates_controls_and_bad_policies_rejected(self):
        entry = {"job": "a", "instance": "b"}
        for targets in ([], [entry, entry], [{"job": "a\nb", "instance": "b"}],
                        [{"job": "{{ $value }}", "instance": "b"}], [{"job": "a"}]):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                self.render(targets)

    def test_additional_exact_labels_disambiguate_targets_sharing_job_and_instance(self):
        targets = [
            {"job": "kubelet", "instance": "10.20.1.28:10250", "labels": {"metrics_path": "/metrics"}},
            {"job": "kubelet", "instance": "10.20.1.28:10250", "labels": {"metrics_path": "/metrics/cadvisor"}},
        ]
        rules = self.render(targets)["spec"]["groups"][0]["rules"]
        self.assertEqual(len(rules), 2)
        expressions = [rule["expr"] for rule in rules]
        self.assertTrue(any('metrics_path="/metrics"' in expression for expression in expressions))
        self.assertTrue(any('metrics_path="/metrics/cadvisor"' in expression for expression in expressions))
        self.assertNotEqual(rules[0]["labels"]["target_id"], rules[1]["labels"]["target_id"])

    def test_invalid_or_duplicate_selector_labels_are_rejected(self):
        for target in (
            {"job": "kubelet", "instance": "node:10250", "labels": {"bad-name": "x"}},
            {"job": "kubelet", "instance": "node:10250", "labels": {"job": "other"}},
            {"job": "kubelet", "instance": "node:10250", "labels": {"metrics_path": "{{ $value }}"}},
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.render([target])

    def test_target_argument_accepts_optional_exact_matchers(self):
        self.assertEqual(_parse_target("kubelet=10.20.1.28:10250|metrics_path=/metrics"), {
            "job": "kubelet", "instance": "10.20.1.28:10250", "labels": {"metrics_path": "/metrics"}})
        with self.assertRaises(OperationError):
            _parse_target("kubelet=10.20.1.28:10250|metrics_path=/metrics|metrics_path=/probe")


class ControlPlaneRuleTest(unittest.TestCase):
    def test_api_target_rule_is_explicit_and_missing_data_fails_closed(self):
        result = control_plane_rules("monitoring", "stack", [{"job": "apiserver", "instance": "cp1:6443"}],
                                     30, 120, "warning", "控制面不可用", "检查认证采集和目标状态。")
        rule = result["spec"]["groups"][0]["rules"][0]
        self.assertEqual(rule["labels"]["risk_id"], "R-CP-01")
        self.assertEqual(rule["alert"], "AtlasControlPlaneTargetOrTelemetryMissing")
        self.assertIn('up{job="apiserver",instance="cp1:6443"}', rule["expr"])


if __name__ == "__main__":
    unittest.main()
