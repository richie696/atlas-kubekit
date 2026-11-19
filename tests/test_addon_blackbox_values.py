# SPDX-License-Identifier: MIT
import unittest
from pathlib import Path

from scripts.addons.blackbox_values import values
from scripts.addons.sources import yaml_module


class BlackboxValuesTest(unittest.TestCase):
    def test_values_enable_daemonset_self_monitor_and_hardened_security(self):
        rendered = yaml_module().safe_load(values())
        self.assertEqual(rendered["kind"], "DaemonSet")
        self.assertFalse(rendered["automountServiceAccountToken"])
        self.assertFalse(rendered["serviceMonitor"]["selfMonitor"]["enabled"])
        self.assertFalse(rendered["serviceMonitor"]["enabled"])
        self.assertFalse(rendered["automountServiceAccountToken"])
        self.assertFalse(rendered["config"]["modules"]["http_2xx"]["http"]["follow_redirects"])
        self.assertEqual(set(rendered["config"]["modules"]), {"http_2xx"})
        self.assertTrue(rendered["securityContext"]["readOnlyRootFilesystem"])
        self.assertTrue(rendered["securityContext"]["runAsNonRoot"])
        self.assertEqual(rendered["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertEqual(len(rendered["tolerations"]), 2)

    def test_release_label_is_validated_before_yaml_rendering(self):
        for value in ("Bad", "x/y", "x" * 64, ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                values(value)

    def test_addon_installer_uses_rendered_defaults(self):
        script = (Path(__file__).resolve().parents[1] / "03-install-addons.sh").read_text()
        self.assertIn("blackbox_values.py\" --safe-defaults", script)
        self.assertIn("prometheus-community/prometheus-blackbox-exporter", script)
        self.assertIn("ui_printf 'phase03.menu.blackbox'; printf '\\n'", script)
        self.assertIn("ui_printf 'phase03.menu.notification_setup'; printf '\\n'", script)
        self.assertIn("ui_printf 'phase03.menu.monitoring_full_setup'; printf '\\n';;", script)

    def test_full_monitoring_setup_orders_dependencies_and_checks_readiness(self):
        script = (Path(__file__).resolve().parents[1] / "03-install-addons.sh").read_text()
        flow = script[script.index("monitoring_full_setup() {"):script.index("show_menu() {")]
        self.assertLess(flow.index("install_selected 4"), flow.index("install_selected 5"))
        self.assertLess(flow.index("install_selected 5"), flow.index("--require-complete"))
        self.assertIn("release_present kube-prometheus-stack monitoring", flow)
        self.assertIn("release=kube-prometheus-stack,app.kubernetes.io/name=prometheus", flow)
        self.assertIn("release=kube-prometheus-stack,app.kubernetes.io/name=alertmanager", flow)
        self.assertIn("rollout status", flow)
        self.assertIn("condition=Ready pod -l app.kubernetes.io/instance=loki", flow)
        self.assertIn("condition=Ready pod -l app.kubernetes.io/instance=alloy", flow)
        self.assertIn("    19)\n", script)


if __name__ == "__main__":
    unittest.main()
