# SPDX-License-Identifier: MIT
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations import cli, configuration
from scripts.operations.monitoring.longhorn_rules import volume_robustness_rules
from tests.test_monitoring_change_plan import Environment


class LonghornMonitoringTest(unittest.TestCase):
    def test_invalid_pvc_and_duplicates_are_rejected(self):
        claim = {"namespace": "prod", "claim": "data"}
        for targets in ([], [claim, claim], [{"namespace": "prod", "claim": 'bad"'}]):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                volume_robustness_rules("monitoring", "stack", targets, 10, 120, "warning", "s", "d")

    def test_long_pvc_name_is_preserved_in_metric_selector_and_annotation(self):
        claim = "prometheus-kube-prometheus-stack-prometheus-db-prometheus-kube-prometheus-stack-prometheus-0"
        rule = volume_robustness_rules("monitoring", "stack", [{"namespace": "monitoring", "claim": claim}],
                                       10, 120, "warning", "s", "d")["spec"]["groups"][0]["rules"][0]
        self.assertIn('pvc="' + claim + '"', rule["expr"])
        self.assertEqual(rule["annotations"]["persistentvolumeclaim"], claim)
        self.assertLessEqual(len(rule["labels"]["persistentvolumeclaim_id"]), 63)

    def test_plan_apply_is_explicitly_unverified(self):
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-longhorn-rules",
                    "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                    "--monitoring-hold-seconds", "10", "--monitoring-pvc", "prod/data", "--output-dir", temp])
                self.assertEqual(result, 0)
                plan = next((Path(temp) / "plans").glob("*.json"))
                plan_id = json.loads(plan.read_text())["id"]
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-apply", "--plan-file", str(plan),
                                           "--confirm", plan_id, "--output-dir", temp]), 2)
            self.assertEqual(environment.client.rule["metadata"]["name"], "atlas-longhorn-robustness")
            self.assertIn("prod/data", output.getvalue())
            self.assertIn("求值和通知送达仍未验证", output.getvalue())


if __name__ == "__main__":
    unittest.main()
