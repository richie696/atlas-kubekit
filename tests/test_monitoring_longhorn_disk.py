# SPDX-License-Identifier: MIT
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations import cli, configuration
from scripts.operations.monitoring.longhorn_disk_rules import disk_capacity_rules
from tests.test_monitoring_change_plan import Environment


class LonghornDiskMonitoringTest(unittest.TestCase):
    def test_disk_alerts_are_owned_localized_and_warn_about_scheduler_semantics(self):
        rule = disk_capacity_rules("monitoring", "stack", 70, 10, 120, "warning",
            {"capacity": "capacity", "not_ready": "not-ready", "missing": "missing", "partial": "partial"},
            {"capacity": "capacity description", "not_ready": "disk description", "missing": "missing description",
             "partial": "partial description"})
        self.assertEqual(rule["metadata"]["name"], "atlas-longhorn-disk-capacity")
        self.assertEqual(rule["spec"]["groups"][0]["name"], "atlas.storage.longhorn.disk")
        rules = rule["spec"]["groups"][0]["rules"]
        self.assertEqual([item["alert"] for item in rules], [
            "AtlasLonghornDiskUsageHigh", "AtlasLonghornDiskNotReady", "AtlasLonghornDiskMetricsMissing",
            "AtlasLonghornDiskTelemetryPartial"])
        self.assertTrue(all(item["labels"]["risk_id"] == "R-STOR-04" for item in rules))
        self.assertIn("max by (node, disk)", rules[0]["expr"])
        self.assertIn("timestamp(longhorn_disk_usage_bytes", rules[0]["expr"])
        self.assertIn("< 120", rules[0]["expr"])
        self.assertIn("unless on (node, disk)", rules[3]["expr"])
        self.assertIn("observed_value", rules[0]["annotations"])
        self.assertNotIn("schedulable", rules[0]["annotations"]["description"])

    def test_invalid_threshold_and_messages_are_rejected(self):
        args = ["monitoring", "stack", 70, 10, 120, "warning",
                {"capacity": "capacity", "not_ready": "not-ready", "missing": "missing"},
                {"capacity": "capacity description", "not_ready": "disk description", "missing": "missing description"}]
        for index, value in ((2, 100), (2, 0), (6, {"capacity": "bad {{ template", "not_ready": "a", "missing": "b"})):
            invalid = list(args)
            invalid[index] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                disk_capacity_rules(*invalid)

    def test_plan_apply_is_explicitly_unverified(self):
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-longhorn-disk-rules",
                    "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                    "--monitoring-hold-seconds", "10", "--monitoring-max-age", "120",
                    "--monitoring-longhorn-disk-used-percent", "70", "--output-dir", temp])
                self.assertEqual(result, 0)
                plan = next((Path(temp) / "plans").glob("*.json"))
                plan_id = json.loads(plan.read_text())["id"]
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-apply", "--plan-file", str(plan),
                                           "--confirm", plan_id, "--output-dir", temp]), 2)
            self.assertEqual(environment.client.rule["metadata"]["name"], "atlas-longhorn-disk-capacity")
            self.assertIn("70%", output.getvalue())
            self.assertIn("求值和通知送达仍未验证", output.getvalue())


if __name__ == "__main__":
    unittest.main()
