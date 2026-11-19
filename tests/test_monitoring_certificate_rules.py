# SPDX-License-Identifier: MIT
import unittest

from scripts.operations.monitoring.certificate_rules import certificate_rules


class CertificateRuleTest(unittest.TestCase):
    def render(self, certificates=None, days=30):
        return certificate_rules("monitoring", "stack", certificates or [{"namespace": "apps", "name": "web-tls"}],
                                 days, 60, 120, "warning", "Certificate expiry", "Renew certificate.",
                                 "Certificate not ready", "Check issuer.", "Certificate metrics missing",
                                 "Check scrape target.")

    def test_generates_expiry_readiness_and_missing_metric_rules(self):
        rules = self.render()["spec"]["groups"][0]["rules"]
        self.assertEqual([rule["alert"] for rule in rules], [
            "AtlasCertificateExpiringSoon", "AtlasCertificateNotReady", "AtlasCertificateTelemetryMissing"])
        expiry, ready, missing = rules
        self.assertIn('certmanager_certificate_expiration_timestamp_seconds{exported_namespace="apps",name="web-tls"}', expiry["expr"])
        self.assertIn("<= 2592000", expiry["expr"])
        self.assertIn("> 0", expiry["expr"])
        self.assertIn("timestamp(", expiry["expr"])
        self.assertEqual(expiry["labels"]["risk_id"], "R-EDGE-04")
        self.assertIn('condition=~"False|Unknown"', ready["expr"])
        self.assertIn('exported_namespace="apps"', ready["expr"])
        self.assertIn("absent(", missing["expr"])

    def test_target_order_is_stable_and_duplicates_rejected(self):
        result = self.render([{"namespace": "z", "name": "b"}, {"namespace": "a", "name": "c"}])
        rules = result["spec"]["groups"][0]["rules"]
        self.assertEqual([(item["labels"]["namespace"], item["alert"]) for item in rules[:3]], [
            ("a", "AtlasCertificateExpiringSoon"), ("a", "AtlasCertificateNotReady"),
            ("a", "AtlasCertificateTelemetryMissing")])
        duplicate = {"namespace": "apps", "name": "web-tls"}
        with self.assertRaises(ValueError):
            self.render([duplicate, duplicate])

    def test_invalid_ranges_and_names_are_rejected(self):
        for days, targets in ((0, None), (366, None), (30, [{"namespace": "bad/ns", "name": "web"}]),
                              (30, [{"namespace": "apps", "name": "bad/name"}])):
            with self.subTest(days=days, targets=targets), self.assertRaises(ValueError):
                self.render(targets, days)


if __name__ == "__main__":
    unittest.main()
