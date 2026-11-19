# SPDX-License-Identifier: MIT
import unittest
from dataclasses import replace

from scripts.operations.monitoring.coverage import RiskObservation, assess, assess_all


class MonitoringCoverageTest(unittest.TestCase):
    def setUp(self):
        self.verified = RiskObservation("R-NODE-01", "required", True,
                                        "verified", "verified", "verified")

    def test_all_stages_required_for_covered(self):
        self.assertEqual(assess(self.verified).status, "covered")
        for field in ("data", "rule", "delivery"):
            with self.subTest(field=field):
                result = assess(replace(self.verified, **{field: "unknown"}))
                self.assertEqual(result.status, "unverified")
                self.assertIn(field + "-unverified", result.reasons)

    def test_missing_configuration_and_failed_evidence_are_not_covered(self):
        self.assertEqual(assess(replace(self.verified, configured=False)).status, "uncovered")
        self.assertEqual(assess(replace(self.verified, delivery="failed")).status, "uncovered")

    def test_not_applicable_needs_reason(self):
        excluded = replace(self.verified, applicability="not-applicable")
        with self.assertRaises(ValueError):
            assess(excluded)
        self.assertEqual(assess(replace(excluded, exclusion_reason="component not installed")).status,
                         "not-applicable")

    def test_invalid_and_duplicate_observations_fail(self):
        with self.assertRaises(ValueError):
            assess(replace(self.verified, risk_id="not-a-risk"))
        with self.assertRaises(ValueError):
            assess(replace(self.verified, configured=1))
        with self.assertRaises(ValueError):
            assess(replace(self.verified, data="healthy"))
        with self.assertRaises(ValueError):
            assess_all((self.verified, self.verified))


if __name__ == "__main__":
    unittest.main()
