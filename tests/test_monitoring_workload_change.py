# SPDX-License-Identifier: MIT
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.operations import cli, configuration
from scripts.operations.capabilities import monitoring_workload_health
from tests.test_monitoring_change_plan import Environment


class WorkloadChangeTest(unittest.TestCase):
    def test_preview_apply_and_status_remain_unverified(self):
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-workload-rules",
                    "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                    "--monitoring-hold-seconds", "10", "--monitoring-deployment", "prod/web", "--output-dir", temp])
                self.assertEqual(result, 0)
                plan = next((Path(temp) / "plans").glob("*.json"))
                plan_id = json.loads(plan.read_text())["id"]
                self.assertIn("prod/web", output.getvalue())
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-apply", "--plan-file", str(plan),
                                           "--confirm", plan_id, "--output-dir", temp]), 2)
                self.assertEqual(cli.main(["--lang", "zh_CN", "--change-status", "--plan-id", plan_id,
                                           "--output-dir", temp]), 2)
            self.assertEqual(environment.client.rule["metadata"]["name"], "atlas-deployment-availability")
            self.assertIn("求值和通知送达仍未验证", output.getvalue())

    def test_workload_health_rules_are_exposed_as_a_previewable_change_plan(self):
        environment = Environment()
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-workload-health-rules",
                    "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                    "--monitoring-hold-seconds", "10",
                    "--monitoring-restart-threshold", "4", "--monitoring-restart-window-seconds", "180",
                    "--monitoring-job-timeout-seconds", "7200", "--monitoring-cronjob-overdue-seconds", "1800",
                    "--output-dir", temp])
                self.assertEqual(result, 0)
            self.assertIn("工作负载", output.getvalue())
            plan = next((Path(temp) / "plans").glob("*.json"))
            parameters = json.loads(plan.read_text())["parameters"]
            rules = monitoring_workload_health._manifest(parameters)["spec"]["groups"][0]["rules"]
            self.assertEqual([rule["alert"] for rule in rules], [
                "AtlasPodPendingTooLong", "AtlasPodContainerStartFailure", "AtlasPodContainerRestartBurst",
                "AtlasPodOOMKilled", "AtlasJobFailure", "AtlasJobRunningTooLong", "AtlasCronJobScheduleOverdue",
                "AtlasHPAReplicaLimitOrScalingLimited", "AtlasHPAMetricsUnavailable", "AtlasKEDAScalerErrors",
                "AtlasKEDAScaledObjectErrors", "AtlasKEDAEmptyUpstreamResponses", "AtlasKEDAScalerMetricsSlow",
                "AtlasKEDAOperatorTargetMissing"])
            self.assertIn("increase(kube_pod_container_status_restarts_total[180s]) >= 4",
                          next(rule["expr"] for rule in rules if rule["alert"] == "AtlasPodContainerRestartBurst"))
            self.assertIn("> 7200", next(rule["expr"] for rule in rules if rule["alert"] == "AtlasJobRunningTooLong"))
            self.assertIn("> 1800", next(rule["expr"] for rule in rules if rule["alert"] == "AtlasCronJobScheduleOverdue"))
            self.assertIn("> 5", next(rule["expr"] for rule in rules if rule["alert"] == "AtlasKEDAScalerMetricsSlow"))
            target_rule = next(rule for rule in rules if rule["alert"] == "AtlasKEDAOperatorTargetMissing")
            self.assertEqual(target_rule["expr"], "vector(0)")
            with patch("scripts.operations.commands.changes.environment", return_value=environment), \
                 patch("scripts.operations.commands.changes.configuration_cluster.read", return_value=configuration.template()), \
                 redirect_stdout(io.StringIO()):
                enabled = cli.main(["--lang", "zh_CN", "--change-plan", "--operation", "monitoring-workload-health-rules",
                    "--monitoring-prometheus-name", "prometheus-stack", "--monitoring-release", "stack",
                    "--monitoring-hold-seconds", "10", "--monitoring-keda-namespace", "keda",
                    "--output-dir", temp])
            self.assertEqual(enabled, 0)
            latest_plan = sorted((Path(temp) / "plans").glob("*.json"), key=lambda path: path.stat().st_mtime)[-1]
            enabled_rules = monitoring_workload_health._manifest(json.loads(latest_plan.read_text())["parameters"])["spec"]["groups"][0]["rules"]
            enabled_target = next(rule for rule in enabled_rules if rule["alert"] == "AtlasKEDAOperatorTargetMissing")
            self.assertIn('namespace="keda",service="keda-operator"}[120s]', enabled_target["expr"])
            self.assertIn("absent_over_time(", enabled_target["expr"])


if __name__ == "__main__":
    unittest.main()
