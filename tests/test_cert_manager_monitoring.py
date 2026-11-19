# SPDX-License-Identifier: MIT
import unittest

from scripts.addons.cert_manager_monitoring import pod_monitor, selected_by


class CertManagerPodMonitorTest(unittest.TestCase):
    def setUp(self):
        self.monitor = pod_monitor("kube-prometheus-stack")
        self.prometheus = {
            "metadata": {"name": "kube-prometheus-stack-prometheus", "namespace": "monitoring"},
            "spec": {"podMonitorSelector": {"matchLabels": {"release": "kube-prometheus-stack"}},
                     "podMonitorNamespaceSelector": {}},
        }
        self.namespace = {"metadata": {"name": "cert-manager",
                                        "labels": {"kubernetes.io/metadata.name": "cert-manager"}}}

    def test_manifest_is_selected_and_targets_only_cert_manager_metrics_ports(self):
        self.assertTrue(selected_by(self.prometheus, self.namespace, self.monitor))
        self.assertEqual(self.monitor["kind"], "PodMonitor")
        self.assertEqual(self.monitor["metadata"]["labels"]["release"], "kube-prometheus-stack")
        self.assertEqual(self.monitor["spec"]["podMetricsEndpoints"], [{
            "port": "http-metrics", "path": "/metrics", "interval": "60s", "scrapeTimeout": "30s"}])
        self.assertEqual(self.monitor["spec"]["jobLabel"], "app.kubernetes.io/name")

    def test_custom_prometheus_selectors_must_match_monitor_and_namespace(self):
        self.prometheus["spec"]["podMonitorSelector"] = {"matchLabels": {"release": "other"}}
        self.assertFalse(selected_by(self.prometheus, self.namespace, self.monitor))
        self.prometheus["spec"]["podMonitorSelector"] = {"matchLabels": {"release": "kube-prometheus-stack"}}
        self.prometheus["spec"]["podMonitorNamespaceSelector"] = {
            "matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}
        self.assertFalse(selected_by(self.prometheus, self.namespace, self.monitor))

    def test_unknown_selector_semantics_are_rejected(self):
        self.prometheus["spec"]["podMonitorSelector"] = None
        self.assertFalse(selected_by(self.prometheus, self.namespace, self.monitor))

    def test_invalid_release_name_is_rejected(self):
        for value in ("", "Upper", "bad/name", "x" * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pod_monitor(value)


if __name__ == "__main__":
    unittest.main()
