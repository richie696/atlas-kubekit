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


class StorageChangeTest(unittest.TestCase):
    def test_preview_apply_and_status_remain_unverified(self):
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-storage-rules",
                    "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                    "--monitoring-hold-seconds", "10", "--monitoring-free-percent", "15",
                    "--monitoring-pvc", "prod/data", "--output-dir", temp])
                self.assertEqual(result, 0)
                plan = next((Path(temp) / "plans").glob("*.json"))
                plan_id = json.loads(plan.read_text())["id"]
                self.assertIn("prod/data", output.getvalue())
                self.assertIn("15%", output.getvalue())
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-apply", "--plan-file", str(plan),
                                           "--confirm", plan_id, "--output-dir", temp]), 2)
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-status", "--plan-id", plan_id,
                                           "--output-dir", temp]), 2)
            self.assertEqual(environment.client.rule["metadata"]["name"], "atlas-pvc-capacity")
            self.assertIn("求值和通知送达仍未验证", output.getvalue())


if __name__ == "__main__":
    unittest.main()
