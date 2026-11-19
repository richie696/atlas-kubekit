# SPDX-License-Identifier: MIT
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.operations import cli, configuration
from tests.test_monitoring_change_plan import Environment


class ControllerChangeTest(unittest.TestCase):
    def test_each_kind_publishes_only_its_own_rule_and_remains_unverified(self):
        for kind in ("statefulset", "daemonset"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
                environment = Environment()
                with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                     patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                     redirect_stdout(io.StringIO()) as output:
                    result = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-" + kind + "-rules",
                        "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                        "--monitoring-hold-seconds", "10", "--monitoring-controller", "prod/app", "--output-dir", temp])
                    self.assertEqual(result, 0)
                    path = next((Path(temp) / "plans").glob("*.json"))
                    plan_id = json.loads(path.read_text())["id"]
                    self.assertIn("prod/app", output.getvalue())
                    self.assertEqual(cli.main(["--lang", "zh_CN", "--change-apply", "--plan-file", str(path),
                                               "--confirm", plan_id, "--output-dir", temp]), 2)
                    self.assertEqual(cli.main(["--lang", "zh_CN", "--change-status", "--plan-id", plan_id,
                                               "--output-dir", temp]), 2)
                self.assertEqual(environment.client.rule["metadata"]["name"], "atlas-" + kind + "-availability")


if __name__ == "__main__":
    unittest.main()
