# SPDX-License-Identifier: MIT
import unittest
from scripts.operations.monitoring.workload_rules import deployment_rules, controller_rules


class WorkloadRuleTest(unittest.TestCase):
    def render(self, workloads):
        return deployment_rules("monitoring", "stack", workloads, 10, 120, "warning", "Unavailable", "Check metrics.",
                                "Rollout stalled", "Check deployment progress.")

    def test_generates_explicit_targets_in_stable_order(self):
        spec = self.render([{"namespace": "z", "deployment": "app"}, {"namespace": "a", "deployment": "web"}])
        rules = spec["spec"]["groups"][0]["rules"]
        self.assertEqual([(r["labels"]["namespace"], r["alert"]) for r in rules], [
            ("a", "AtlasDeploymentAvailabilityOrTelemetryMissing"),
            ("a", "AtlasDeploymentRolloutStalled"),
            ("z", "AtlasDeploymentAvailabilityOrTelemetryMissing"),
            ("z", "AtlasDeploymentRolloutStalled"),
        ])
        self.assertIn('namespace="a",deployment="web"', rules[0]["expr"])
        self.assertEqual(rules[0]["labels"]["risk_id"], "R-WORK-01")
        rollout = next(rule for rule in rules if rule["alert"] == "AtlasDeploymentRolloutStalled")
        self.assertEqual(rollout["labels"]["risk_id"], "R-WORK-04")
        self.assertIn('reason="ProgressDeadlineExceeded"', rollout["expr"])

    def test_invalid_or_duplicate_targets_rejected(self):
        item = {"namespace": "prod", "deployment": "web"}
        for items in ([], [item, item], [{"namespace": "a/b", "deployment": "web"}],
                      [{"namespace": "prod", "deployment": '"} or vector(1)'}]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.render(items)

    def test_controller_kind_cannot_be_substituted(self):
        for kind in ("statefulset", "daemonset"):
            rule = controller_rules(kind, "monitoring", "stack", [{"namespace": "prod", kind: "app"}],
                                    10, 120, "warning", "Unavailable", "Check metrics.")
            self.assertEqual(rule["metadata"]["name"], "atlas-" + kind + "-availability")
            with self.assertRaises(ValueError):
                controller_rules(kind, "monitoring", "stack", [{"namespace": "prod", "deployment": "app"}],
                                 10, 120, "warning", "Unavailable", "Check metrics.")


if __name__ == "__main__":
    unittest.main()
