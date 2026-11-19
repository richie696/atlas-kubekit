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


class CoreDNSChangeTest(unittest.TestCase):
    def test_plan_apply_and_status_publish_all_rules(self):
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-dns-rules",
                    "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                    "--monitoring-hold-seconds", "1", "--monitoring-target", "coredns=dns-0:9153",
                    "--monitoring-dns-error-percent", "7", "--monitoring-dns-window-seconds", "60",
                    "--monitoring-dns-min-requests", "80",
                    "--output-dir", temp])
                self.assertEqual(result, 0)
                self.assertIn("延迟阈值 100 毫秒，至少 80 次查询后计算 P95，统计窗口 60 秒", output.getvalue())
                path = next((Path(temp) / "plans").glob("*.json"))
                plan_id = json.loads(path.read_text())["id"]
                self.assertIn("CoreDNS", output.getvalue())
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-apply", "--plan-file", str(path),
                    "--confirm", plan_id, "--output-dir", temp]), 2)
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-status", "--plan-id", plan_id,
                    "--output-dir", temp]), 2)
            self.assertEqual(environment.client.rule["metadata"]["name"], "atlas-coredns-health")
            rules = environment.client.rule["spec"]["groups"][0]["rules"]
            self.assertEqual(len(rules), 4)
            self.assertEqual(rules[-1]["alert"], "AtlasCoreDNSReplicasBelowMinimum")
            self.assertEqual(rules[-1]["labels"]["minimum_replicas"], "2")
            latency_rule = next(rule for rule in rules if rule["alert"] == "AtlasCoreDNSP95LatencyHigh")
            self.assertIn(">= 80", latency_rule["expr"])
            self.assertIn("统计窗口 60 秒内至少 80 次查询", latency_rule["annotations"]["description"])
            self.assertIn("求值和通知送达仍未验证", output.getvalue())

    def test_duplicate_targets_fail_before_write(self):
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()):
                result = cli.main(["--lang", "en", "--change-plan", "--operation", "monitoring-dns-rules",
                    "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                    "--monitoring-target", "coredns=dns-0:9153", "--monitoring-target", "coredns=dns-0:9153",
                    "--output-dir", temp])
            self.assertNotEqual(result, 0)
            self.assertIsNone(environment.client.rule)


if __name__ == "__main__":
    unittest.main()
