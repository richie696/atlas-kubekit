# SPDX-License-Identifier: MIT
import unittest

from scripts.addons.monitoring_values import safe_monitoring_values
from scripts.addons.keda_values import monitoring_values as keda_monitoring_values
from scripts.addons.sources import yaml_module
from scripts.addons.helm_release import initial_failure_needs_cleanup
from pathlib import Path


class AddonMonitoringValuesTest(unittest.TestCase):
    def test_failed_helm_first_install_is_recoverable_only_without_deployed_history(self):
        self.assertTrue(initial_failure_needs_cleanup(
            '[{"revision":1,"status":"failed"}]'))
        self.assertTrue(initial_failure_needs_cleanup(
            '[{"revision":1,"status":"uninstalling"}]'))
        self.assertFalse(initial_failure_needs_cleanup(
            '[{"revision":1,"status":"deployed"}]'))
        self.assertFalse(initial_failure_needs_cleanup(
            '[{"revision":1,"status":"deployed"},{"revision":2,"status":"failed"}]'))
        self.assertFalse(initial_failure_needs_cleanup(
            '[{"revision":2,"status":"failed"}]'))
        with self.assertRaises(ValueError):
            initial_failure_needs_cleanup('not-json')

    def test_failed_first_install_cleanup_requires_confirmation_and_skips_hooks(self):
        script = (Path(__file__).resolve().parents[1] / "03-install-addons.sh").read_text()
        start = script.index('install_downloaded_chart()')
        end = script.index('release_present()', start)
        block = script[start:end]
        self.assertIn('helm history "$release" -n "$namespace" -o json', block)
        self.assertIn('initial-failure-needs-cleanup', block)
        self.assertIn('yes_no \'phase03.ui.confirm_failed_first_install_cleanup\' no', block)
        self.assertIn('helm uninstall "$release" -n "$namespace" --no-hooks --wait', block)
        self.assertIn('EXISTING=0', block)

    def test_monitoring_defaults_avoid_single_writer_upgrade_deadlock(self):
        values = yaml_module().safe_load(safe_monitoring_values())
        self.assertEqual(values["grafana"]["deploymentStrategy"], {"type": "Recreate"})
        self.assertEqual(values["defaultRules"]["rules"]["kubeProxy"], False)
        self.assertEqual(values["kubeProxy"]["serviceMonitor"]["enabled"], False)
        rules = values["alertmanager"]["config"]["inhibit_rules"]
        self.assertEqual(len(rules), 4)
        self.assertEqual(rules[0]["equal"], ["cluster", "namespace", "alertname"])
        self.assertEqual(rules[1]["equal"], ["cluster", "namespace", "alertname"])
        self.assertEqual(rules[2]["equal"], ["cluster", "namespace"])

    def test_monitoring_defaults_include_localized_alertmanager_failure_rule(self):
        for locale, expected in (("en", "Alertmanager is unavailable"),
                                 ("zh_CN", "Alertmanager 不可用"),
                                 ("zh_TW", "Alertmanager 不可用")):
            with self.subTest(locale=locale):
                values = yaml_module().safe_load(safe_monitoring_values(locale))
                rule = values["additionalPrometheusRulesMap"]["atlas-self-monitoring"]["groups"][0]["rules"][0]
                self.assertEqual(rule["alert"], "AtlasAlertmanagerUnavailableOrTelemetryMissing")
                self.assertEqual(rule["for"], "1m")
                self.assertEqual(rule["labels"], {"severity": "critical", "risk_id": "R-OBS-02"})
                self.assertIn("absent(up", rule["expr"])
                self.assertIn("sum(up", rule["expr"])
                self.assertIn(expected, rule["annotations"]["summary"])
                self.assertIn("独立观察者" if locale == "zh_CN" else "獨立觀察者"
                              if locale == "zh_TW" else "independent observer",
                              rule["annotations"]["description"])

    def test_monitoring_defaults_include_localized_csi_attachment_alerts(self):
        for locale, expected in (("en", "CSI volume attachment"),
                                 ("zh_CN", "CSI 卷"), ("zh_TW", "CSI 磁碟區")):
            with self.subTest(locale=locale):
                values = yaml_module().safe_load(safe_monitoring_values(locale))
                rules = values["additionalPrometheusRulesMap"]["atlas-csi-attachments"]["groups"][0]["rules"]
                self.assertEqual({rule["alert"] for rule in rules}, {
                    "AtlasCSIVolumeAttachmentNotReady", "AtlasCSIVolumeAttachmentTelemetryMissing"})
                for rule in rules:
                    self.assertEqual(rule["for"], "5m")
                    self.assertEqual(rule["labels"], {"severity": "warning", "risk_id": "R-STOR-02"})
                    self.assertIn("kube_volumeattachment_info", rule["expr"])
                    self.assertIn("kube_volumeattachment_status_attached", rule["expr"])
                    self.assertIn(expected, rule["annotations"]["summary"])

    def test_monitoring_defaults_include_scoped_node_systemd_collection_and_alerts(self):
        for locale, expected in (("en", "Kubelet is inactive"),
                                 ("zh_CN", "kubelet 服务未运行"),
                                 ("zh_TW", "kubelet 服務未執行")):
            with self.subTest(locale=locale):
                values = yaml_module().safe_load(safe_monitoring_values(locale))
                exporter = values["prometheus-node-exporter"]
                self.assertEqual(exporter["extraArgs"], ["--collector.textfile.directory=/host-textfile"])
                self.assertEqual(exporter["extraHostVolumeMounts"], [{
                    "name": "atlas-systemd-textfile",
                    "hostPath": "/var/lib/atlas-kubekit/node-exporter-textfile",
                    "mountPath": "/host-textfile",
                    "readOnly": True,
                    "type": "DirectoryOrCreate",
                }])
                rules = values["additionalPrometheusRulesMap"]["atlas-node-systemd"]["groups"][0]["rules"]
                self.assertEqual(len(rules), 4)
                kubelet = next(rule for rule in rules if rule["alert"] == "AtlasKubeletServiceUnavailable")
                self.assertEqual(kubelet["for"], "2m")
                self.assertEqual(kubelet["labels"], {"severity": "critical", "risk_id": "R-NODE-06"})
                self.assertIn(expected, kubelet["annotations"]["summary"])
                self.assertIn("group_left(nodename)", kubelet["expr"])
                missing = next(rule for rule in rules if rule["alert"] == "AtlasKubeletSystemdTelemetryMissing")
                self.assertIn("unless on(job,instance)", missing["expr"])
                self.assertIn("timestamp(", missing["expr"])
                for rule in rules:
                    self.assertIn(
                        'node_textfile_mtime_seconds{file="/host-textfile/atlas_systemd_units.prom"}',
                        rule["expr"])

    def test_installer_applies_safe_defaults_before_optional_user_values(self):
        script = (Path(__file__).resolve().parents[1] / "03-install-addons.sh").read_text()
        start = script.index('if [ "${ATLAS_MONITORING_MODE:-}" = center ]; then')
        start = script.index('python3 "$SCRIPT_DIR/scripts/addons/monitoring_topology.py" --mode center', start)
        end = script.index('install_chart prometheus-community https://prometheus-community.github.io/helm-charts', start)
        block = script[start:end]
        self.assertLess(block.index('--mode center'), block.index('optional_values_arg'))
        self.assertIn('--lang "$ATLAS_LANG"', block)
        self.assertIn('EXTRA_VALUES+=(-f "$TMP_DIR/monitoring-safe-defaults.yaml")', script)

    def test_keda_operator_metrics_and_selected_servicemonitor_values(self):
        values = yaml_module().safe_load(keda_monitoring_values("kube-prometheus-stack"))
        operator = values["prometheus"]["operator"]
        self.assertTrue(operator["enabled"])
        self.assertTrue(operator["serviceMonitor"]["enabled"])
        self.assertEqual(operator["serviceMonitor"]["additionalLabels"], {"release": "kube-prometheus-stack"})
        without_prometheus = yaml_module().safe_load(keda_monitoring_values())
        self.assertTrue(without_prometheus["prometheus"]["operator"]["enabled"])
        self.assertFalse(without_prometheus["prometheus"]["operator"]["serviceMonitor"]["enabled"])
        with self.assertRaises(ValueError):
            keda_monitoring_values("Invalid_Release")

    def test_keda_installer_passes_monitoring_defaults_and_recommends_verification(self):
        script = (Path(__file__).resolve().parents[1] / "03-install-addons.sh").read_text()
        start = script.index('prepare_release keda keda')
        end = script.index('install_chart kedacore https://kedacore.github.io/charts', start)
        block = script[start:end]
        self.assertIn('keda_values.py" --prometheus-release kube-prometheus-stack', block)
        self.assertIn('EXTRA_VALUES=(-f "$TMP_DIR/keda-monitoring.yaml")', block)

    def test_addon_install_order_reconciles_keda_after_monitoring_is_installed(self):
        script = (Path(__file__).resolve().parents[1] / "03-install-addons.sh").read_text()
        self.assertIn('if [ "$choice" = 3 ] || [ "$choice" = 4 ] || [ "$choice" = 14 ]; then', script)
        self.assertIn("keda_monitoring_reconcile", script)
        self.assertIn('scripts/addons/keda_service_monitor.py', script)


if __name__ == "__main__":
    unittest.main()
