# SPDX-License-Identifier: MIT
import unittest
from scripts.operations.monitoring.storage_rules import pvc_capacity_rules


class StorageRuleTest(unittest.TestCase):
    def render(self, claims, percent=15):
        return pvc_capacity_rules("monitoring", "stack", claims, percent, 10, 120, "warning", "PVC alert", "Check storage and metrics.")

    def test_valid_targets_and_threshold(self):
        rule = self.render([{"namespace": "prod", "claim": "data"}])["spec"]["groups"][0]["rules"][0]
        self.assertEqual(rule["labels"]["risk_id"], "R-STOR-01")
        self.assertIn("< 0.15", rule["expr"])

    def test_long_claim_uses_bounded_alert_label_and_keeps_full_identity(self):
        claim = "prometheus-kube-prometheus-stack-prometheus-db-prometheus-kube-prometheus-stack-prometheus-0"
        rule = self.render([{"namespace": "monitoring", "claim": claim}])["spec"]["groups"][0]["rules"][0]
        self.assertNotIn("persistentvolumeclaim", rule["labels"])
        self.assertLessEqual(len(rule["labels"]["persistentvolumeclaim_id"]), 63)
        self.assertEqual(rule["annotations"]["persistentvolumeclaim"], claim)
        self.assertIn('persistentvolumeclaim="' + claim + '"', rule["expr"])

    def test_invalid_targets_and_threshold_rejected(self):
        claim = {"namespace": "prod", "claim": "data"}
        for values, percent in (([], 15), ([claim, claim], 15), ([claim], 0), ([claim], True),
                                ([{"namespace": "prod", "claim": 'bad"'}], 15)):
            with self.subTest(values=values, percent=percent), self.assertRaises(ValueError):
                self.render(values, percent)


if __name__ == "__main__":
    unittest.main()
