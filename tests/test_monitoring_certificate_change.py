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


class CertificateChangeTest(unittest.TestCase):
    def test_plan_apply_and_status_publish_cert_rules(self):
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-certificate-rules",
                    "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                    "--monitoring-hold-seconds", "1", "--monitoring-certificate", "apps/web-tls",
                    "--monitoring-certificate-expiry-days", "21", "--output-dir", temp])
                self.assertEqual(result, 0)
                self.assertIn("提前 21 天预警", output.getvalue())
                path = next((Path(temp) / "plans").glob("*.json"))
                plan_id = json.loads(path.read_text())["id"]
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-apply", "--plan-file", str(path),
                    "--confirm", plan_id, "--output-dir", temp]), 2)
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-status", "--plan-id", plan_id,
                    "--output-dir", temp]), 2)
            self.assertEqual(environment.client.rule["metadata"]["name"], "atlas-certificate-expiry")
            rules = environment.client.rule["spec"]["groups"][0]["rules"]
            self.assertEqual(len(rules), 3)
            self.assertEqual(rules[0]["labels"]["risk_id"], "R-EDGE-04")
            self.assertIn("求值和通知送达仍未验证", output.getvalue())

    def test_duplicate_certificate_fails_before_write(self):
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()):
                result = cli.main(["--lang", "en", "--change-plan", "--operation", "monitoring-certificate-rules",
                    "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                    "--monitoring-certificate", "apps/web-tls", "--monitoring-certificate", "apps/web-tls",
                    "--output-dir", temp])
            self.assertNotEqual(result, 0)
            self.assertIsNone(environment.client.rule)


if __name__ == "__main__":
    unittest.main()
