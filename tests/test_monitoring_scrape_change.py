# SPDX-License-Identifier: MIT
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.operations import cli, configuration
from scripts.operations.capabilities import monitoring_scrapes
from scripts.operations.models import OperationError
from tests.test_monitoring_change_plan import Environment
from types import SimpleNamespace


class ScrapeChangeTest(unittest.TestCase):
    def test_cli_plan_apply_and_status_keep_unverified_boundary(self):
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-scrape-rules",
                                   "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                                   "--monitoring-hold-seconds", "10", "--monitoring-target", "api=cp1:6443",
                                   "--monitoring-target", "dns=dns:9153", "--output-dir", temp])
                self.assertEqual(result, 0)
                path = next((Path(temp) / "plans").glob("*.json"))
                plan_id = json.loads(path.read_text())["id"]
                self.assertIn("api=cp1:6443", output.getvalue())
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-apply", "--plan-file", str(path),
                                           "--confirm", plan_id, "--output-dir", temp]), 2)
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-status", "--plan-id", plan_id,
                                           "--output-dir", temp]), 2)
            self.assertEqual(environment.client.rule["metadata"]["name"], "atlas-scrape-targets")
            self.assertEqual(len(environment.client.rule["spec"]["groups"][0]["rules"]), 2)
            self.assertIn("求值和通知送达仍未验证", output.getvalue())

    def test_duplicate_targets_fail_before_write(self):
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()):
                result = cli.main(["--lang", "en", "--change-plan", "--operation", "monitoring-scrape-rules",
                                   "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                                   "--monitoring-hold-seconds", "10", "--monitoring-target", "api=cp1:6443",
                                   "--monitoring-target", "api=cp1:6443", "--output-dir", temp])
            self.assertNotEqual(result, 0)
            self.assertIsNone(environment.client.rule)

    def test_selector_conflict_and_post_plan_drift_do_not_write(self):
        environment = Environment()
        args = SimpleNamespace(lang="en", monitoring_namespace="monitoring", monitoring_release="stack",
                               monitoring_prometheus_name="prometheus-stack", monitoring_hold_seconds=10,
                               monitoring_max_age=120, monitoring_severity="warning",
                               monitoring_target=["api=cp1:6443"])
        environment.client.prom["spec"]["ruleSelector"] = {"matchLabels": {"release": "other"}}
        with self.assertRaises(OperationError):
            monitoring_scrapes.from_arguments(args, configuration.template(), environment)
        self.assertIsNone(environment.client.rule)
        environment.client.prom["spec"]["ruleSelector"] = {"matchLabels": {"release": "stack"}}
        params = monitoring_scrapes.from_arguments(args, configuration.template(), environment)
        environment.client.prom["spec"]["replicas"] = 2
        with self.assertRaises(OperationError):
            monitoring_scrapes.observe(params, environment)
        self.assertIsNone(environment.client.rule)


if __name__ == "__main__":
    unittest.main()
