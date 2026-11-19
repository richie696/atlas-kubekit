import copy
import re
import unittest
from unittest.mock import Mock, patch

from scripts.addons import logging_monitoring as logging
from scripts.operations.models import OperationError, fingerprint
from scripts.operations.monitoring import publication


class LoggingMonitoringTests(unittest.TestCase):
    def test_monitors_select_logging_pods_and_keep_only_aggregate_metrics(self):
        for component in ("loki", "alloy"):
            with self.subTest(component=component):
                manifest = logging.pod_monitor(component)
                spec = manifest["spec"]
                self.assertEqual(spec["namespaceSelector"], {"matchNames": ["logging"]})
                labels = spec["selector"]["matchLabels"]
                self.assertEqual(labels["app.kubernetes.io/instance"], component)
                if component == "loki":
                    self.assertEqual(labels["app.kubernetes.io/component"], "single-binary")
                endpoint = spec["podMetricsEndpoints"][0]
                self.assertEqual(endpoint["port"], "http-metrics")
                self.assertEqual(endpoint["relabelings"][0]["replacement"], "atlas-logging-" + component)
                selector = endpoint["metricRelabelings"][0]["regex"]
                self.assertIsNone(re.fullmatch(selector, "loki_process_dropped_lines_total"))
                for metric in logging.COMPONENTS[component]:
                    self.assertIsNotNone(re.fullmatch(selector, metric))

    def test_rules_cover_only_installed_releases(self):
        rules = logging.logging_rules(["loki"], "zh_CN")["spec"]["groups"][0]["rules"]
        self.assertEqual([rule["alert"] for rule in rules],
                         ["AtlasLokiMetricsUnavailable", "AtlasLokiLogIngestionDiscarded"])
        self.assertIn("丢弃", rules[1]["annotations"]["summary"])
        self.assertTrue(all(rule["labels"]["risk_id"] == "R-OBS-05" for rule in rules))

    def test_monitor_version_is_retained_for_cas_and_rollback(self):
        client = Mock()
        wanted = logging.pod_monitor("alloy")
        current = copy.deepcopy(wanted)
        current["spec"]["podMetricsEndpoints"][0]["interval"] = "60s"
        current["metadata"]["resourceVersion"] = "123"
        current["metadata"]["annotations"] = {publication.SPEC_HASH: fingerprint(current["spec"])}
        client.get.return_value = current
        value, operation = logging._monitor_candidate(client, wanted)
        self.assertEqual(value["metadata"]["resourceVersion"], "123")
        self.assertEqual(value["spec"], wanted["spec"])
        self.assertEqual(operation, client.replace)

    def test_existing_matching_manifest_is_not_rewritten(self):
        client = Mock()
        wanted = logging.pod_monitor("loki")
        current = copy.deepcopy(wanted)
        current["metadata"]["annotations"] = {publication.SPEC_HASH: fingerprint(current["spec"])}
        client.get.return_value = current
        self.assertEqual(logging._monitor_candidate(client, wanted), (None, None))

    def test_foreign_monitor_preflight_runs_before_any_write(self):
        client = Mock()

        def get(kind, name, namespace=None, **_):
            if kind == "namespace":
                return {"metadata": {"name": name}}
            if kind == publication.PROMETHEUS:
                return {"metadata": {"namespace": namespace}, "spec": {"podMonitorSelector": {}}}
            if name == "atlas-logging-alloy":
                return {"metadata": {"name": name}, "spec": {}}
            return None

        client.get.side_effect = get
        with patch.object(logging, "ClusterCoordinator"), patch.object(publication, "capture"), \
                patch.object(publication, "publish") as publish, self.assertRaises(OperationError) as error:
            logging.reconcile(client, ["loki", "alloy"], "zh_CN")
        self.assertEqual(error.exception.key, "ops.change.foreign_resource")
        client.create.assert_not_called()
        client.replace.assert_not_called()
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
