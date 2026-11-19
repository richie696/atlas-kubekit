# SPDX-License-Identifier: MIT
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from scripts.operations.capabilities import monitoring_systemd, monitoring_systemd_remove
from scripts.operations.capabilities.registry import CAPABILITIES
from scripts.operations.configuration import revision, template
from scripts.operations.changes.plans import CAPABILITIES as PLAN_CAPABILITIES
from scripts.operations.arguments import parser_for
from scripts.operations.models import OperationError
from scripts.operations.host_actions import systemd_metrics


class MonitoringSystemdCollectionTest(unittest.TestCase):
    def test_operation_is_reachable_from_change_plan_and_registered_host_actions(self):
        self.assertIs(CAPABILITIES["monitoring-systemd-collection"], monitoring_systemd)
        self.assertIn("monitoring-systemd-collection", PLAN_CAPABILITIES)
        args = parser_for("zh_CN").parse_args([
            "--change-plan", "--operation", "monitoring-systemd-collection",
        ])
        self.assertEqual(args.operation, "monitoring-systemd-collection")
        from scripts.operations.host_actions.entry import ACTIONS
        self.assertIn("systemd-metrics-observe", ACTIONS)
        self.assertIn("systemd-metrics-apply", ACTIONS)

    def setUp(self):
        self.configuration = template()
        self.configuration["nodes"].append({
            "name": "w1", "address": "192.0.2.13", "interface": "enp6s18",
            "dataDevice": "none", "identityFingerprint": "",
        })
        self.rows = {
            "cp1": {"collectorInstalled": False, "timerActive": False, "timerEnabled": False},
            "w1": {"collectorInstalled": True, "timerActive": True, "timerEnabled": True},
        }
        self.environment = Mock()
        self.environment.verify.return_value = (self.configuration, {})
        self.environment.host.side_effect = lambda config, name: self._host(name, self.rows[name])

    @staticmethod
    def _host(name, facts):
        identity = {"identityState": "ok", "hostname": name, "identityFingerprint": "a" * 64}
        host = Mock()
        host.call.return_value = {"identity": identity, "metrics": facts}
        return host

    def test_plan_parameters_cover_only_kubernetes_nodes_and_preview_existing_state(self):
        value = {"configuration": self.configuration,
                 "expected": {"configurationRevision": revision(self.configuration), "nodes": self.rows}}
        normalized = monitoring_systemd.parameters(value)
        self.assertEqual(sorted(normalized["expected"]["nodes"]), ["cp1", "w1"])
        preview = monitoring_systemd.preview(normalized)
        self.assertIn(("ops.monitoring.systemd_preview", (2, "cp1, w1", 1)), preview)
        with self.assertRaises(OperationError):
            monitoring_systemd.parameters({**value, "expected": {**value["expected"], "nodes": {"lb1": {}}}})

    def test_apply_installs_each_node_in_order_and_records_resumable_checkpoints(self):
        expected = {"configurationRevision": revision(self.configuration), "nodes": self.rows}
        value = monitoring_systemd.parameters({"configuration": self.configuration, "expected": expected})
        completed = []

        def host(name):
            result = self._host(name, {"collectorInstalled": True, "timerActive": True, "timerEnabled": True})
            result.call.return_value = {"identity": {"identityState": "ok", "hostname": name,
                                                     "identityFingerprint": "a" * 64},
                                       "metrics": {"collectorInstalled": True, "timerActive": True,
                                                   "timerEnabled": True}}
            return result

        self.environment.host.side_effect = lambda config, name: host(name)
        result = monitoring_systemd.apply(value, self.environment, completed.append, "b" * 64)
        self.assertEqual(result["state"], "installed")
        self.assertEqual(list(result["nodes"]), ["cp1", "w1"])
        self.assertEqual(completed, ["systemd-metrics-cp1-start", "systemd-metrics-cp1-complete",
                                     "systemd-metrics-w1-start", "systemd-metrics-w1-complete"])

    def test_observe_rejects_drift_and_continue_refreshes_observation(self):
        value = monitoring_systemd.parameters({
            "configuration": self.configuration,
            "expected": {"configurationRevision": revision(self.configuration), "nodes": self.rows},
        })
        self.environment.host.side_effect = lambda config, name: self._host(
            name, {"collectorInstalled": True, "timerActive": True, "timerEnabled": True})
        with self.assertRaises(OperationError):
            monitoring_systemd.observe(value, self.environment)
        continued = monitoring_systemd.continue_parameters(value, self.environment)
        self.assertTrue(all(row["collectorInstalled"] for row in continued["expected"]["nodes"].values()))


class MonitoringSystemdRemoveTest(unittest.TestCase):
    def setUp(self):
        self.config = template()
        self.identity = {"hostname": "cp1", "identityFingerprint": "a" * 64, "identityState": "ok"}
        self.files = {"collector": "b" * 64, "service": "c" * 64, "timer": "d" * 64}
        self.host = Mock()
        self.environment = Mock()
        self.environment.verify.return_value = (self.config, {})
        self.environment.host.return_value = self.host
        self.host.call.return_value = {"identity": self.identity, "files": self.files}

    def plan(self):
        args = parser_for("zh_CN").parse_args(
            ["--change-plan", "--operation", "monitoring-systemd-remove", "--node", "cp1"])
        self.assertIs(CAPABILITIES[args.operation], monitoring_systemd_remove)
        self.assertIn(args.operation, PLAN_CAPABILITIES)
        return monitoring_systemd_remove.from_arguments(args, self.config, self.environment)

    def test_only_registered_kubernetes_node_can_be_selected(self):
        with self.assertRaises(OperationError):
            monitoring_systemd_remove.from_arguments(Mock(node="lb1"), self.config, self.environment)
        self.environment.host.assert_not_called()

    def test_drift_refuses_apply_and_continuation_preserves_identity(self):
        values = self.plan()
        self.host.call.return_value = {"identity": self.identity, "files": {**self.files, "timer": None}}
        with self.assertRaises(OperationError):
            monitoring_systemd_remove.observe(values, self.environment)
        refreshed = monitoring_systemd_remove.continue_parameters(values, self.environment)
        self.assertIsNone(refreshed["expected"]["files"]["timer"])
        self.host.call.return_value["identity"] = {**self.identity, "identityFingerprint": "e" * 64}
        with self.assertRaises(OperationError):
            monitoring_systemd_remove.continue_parameters(values, self.environment)

    def test_apply_forwards_pinned_files_and_identity_and_records_completion(self):
        values = self.plan()
        self.host.call.return_value = {"identity": self.identity, "files": dict.fromkeys(self.files)}
        checks = []
        result = monitoring_systemd_remove.apply(values, self.environment, checks.append, "f" * 64)
        self.assertEqual(result, {"state": "removed", "node": "cp1"})
        self.assertEqual(checks, ["systemd-metrics-cp1-remove-start", "systemd-metrics-cp1-remove-complete"])
        self.host.call.assert_called_with("systemd-metrics-remove", {
            "expected": self.files, "identity": {"hostname": "cp1", "identityFingerprint": "a" * 64},
        }, "f" * 64, timeout=180)

    def test_host_action_checks_identity_before_running_uninstall(self):
        with patch.object(systemd_metrics, "identity_facts", return_value=self.identity), \
                patch.object(systemd_metrics, "_run_helper") as helper:
            with self.assertRaises(OperationError):
                systemd_metrics.remove({"expected": self.files,
                                        "identity": {"hostname": "cp1", "identityFingerprint": "f" * 64}}, "run")
        helper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
