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


class EtcdChangeTest(unittest.TestCase):
    def test_plan_apply_and_status_are_unverified(self):
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-etcd-rules",
                    "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                    "--monitoring-hold-seconds", "10", "--monitoring-target", "etcd=cp1:2381", "--output-dir", temp])
                self.assertEqual(result, 0)
                path = next((Path(temp) / "plans").glob("*.json"))
                plan_id = json.loads(path.read_text())["id"]
                self.assertIn("etcd=cp1:2381", output.getvalue())
                self.assertIn("多数派按该清单计算", output.getvalue())
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-apply", "--plan-file", str(path),
                                           "--confirm", plan_id, "--output-dir", temp]), 2)
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-status", "--plan-id", plan_id,
                                           "--output-dir", temp]), 2)
            rule = environment.client.rule
            self.assertEqual(rule["metadata"]["name"], "atlas-etcd-leader")
            rules = rule["spec"]["groups"][0]["rules"]
            self.assertIn("etcd_server_has_leader", rules[0]["expr"])
            quorum = next(item for item in rules if item["alert"] == "AtlasEtcdQuorumOrTelemetryMissing")
            self.assertIn('instance="cp1:2381"', quorum["expr"])
            self.assertIn("< 1", quorum["expr"])
            self.assertEqual(quorum["labels"]["registered_members"], "1")
            self.assertEqual(quorum["labels"]["quorum_required"], "1")

    def test_majority_is_derived_from_one_registered_three_member_cluster(self):
        from scripts.operations.monitoring.scrape_rules import etcd_leader_rules
        target_list = [{"job": "etcd", "instance": "cp1:2381"},
                       {"job": "etcd", "instance": "cp2:2381"},
                       {"job": "etcd", "instance": "cp3:2381"}]
        value = etcd_leader_rules("monitoring", "stack", target_list, 30, 120, "critical", "summary", "description")
        quorum = next(item for item in value["spec"]["groups"][0]["rules"]
                      if item["alert"] == "AtlasEtcdQuorumOrTelemetryMissing")
        self.assertEqual(quorum["labels"]["registered_members"], "3")
        self.assertEqual(quorum["labels"]["quorum_required"], "2")
        self.assertIn('instance="cp1:2381"', quorum["expr"])
        self.assertIn('instance="cp2:2381"', quorum["expr"])
        self.assertIn('instance="cp3:2381"', quorum["expr"])
        self.assertIn("< 2", quorum["expr"])
        self.assertEqual(len(value["spec"]["groups"][0]["rules"]), 4)


if __name__ == "__main__":
    unittest.main()
