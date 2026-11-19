import copy
import unittest

from scripts.addons.keda_service_monitor import service_monitor


def fixture():
    prometheus = {
        "metadata": {"name": "kube-prometheus-stack-prometheus", "namespace": "monitoring"},
        "spec": {
            "serviceMonitorSelector": {"matchLabels": {"release": "kube-prometheus-stack"}},
            "serviceMonitorNamespaceSelector": {},
        },
    }
    service = {
        "metadata": {"name": "keda-operator", "namespace": "keda", "labels": {
            "app.kubernetes.io/name": "keda-operator", "app.kubernetes.io/instance": "keda",
        }},
        "spec": {"selector": {"app": "keda-operator"}, "ports": [
            {"name": "metrics", "port": 8080, "targetPort": 8080},
        ]},
    }
    monitoring_namespace = {"kubernetes.io/metadata.name": "monitoring"}
    return prometheus, service, monitoring_namespace


class KedaServiceMonitorTest(unittest.TestCase):
    def test_monitor_selects_keda_operator_metrics_and_is_selected_by_prometheus(self):
        prometheus, service, namespace_labels = fixture()

        monitor = service_monitor(prometheus, service, namespace_labels, "kube-prometheus-stack")

        self.assertEqual("monitoring", monitor["metadata"]["namespace"])
        self.assertEqual("kube-prometheus-stack", monitor["metadata"]["labels"]["release"])
        self.assertEqual({"matchNames": ["keda"]}, monitor["spec"]["namespaceSelector"])
        self.assertEqual("metrics", monitor["spec"]["endpoints"][0]["port"])

    def test_already_selected_chart_monitor_is_not_duplicated(self):
        prometheus, service, namespace_labels = fixture()
        existing = {
            "metadata": {"name": "keda-operator", "namespace": "keda",
                         "labels": {"release": "kube-prometheus-stack"}},
            "spec": {"namespaceSelector": {"matchNames": ["keda"]},
                     "selector": {"matchLabels": {"app.kubernetes.io/name": "keda-operator",
                                                    "app.kubernetes.io/instance": "keda"}},
                     "endpoints": [{"port": "metrics", "path": "/metrics"}]},
        }

        result = service_monitor(prometheus, service, namespace_labels, "kube-prometheus-stack", [existing])

        self.assertIsNone(result)

    def test_unselected_monitor_does_not_suppress_managed_monitor(self):
        prometheus, service, namespace_labels = fixture()
        existing = {
            "metadata": {"name": "keda-operator", "namespace": "keda",
                         "labels": {"release": "other"}},
            "spec": {"namespaceSelector": {"matchNames": ["keda"]},
                     "selector": {"matchLabels": {"app.kubernetes.io/name": "keda-operator",
                                                    "app.kubernetes.io/instance": "keda"}},
                     "endpoints": [{"port": "metrics", "path": "/metrics"}]},
        }

        result = service_monitor(prometheus, service, namespace_labels, "kube-prometheus-stack", [existing])

        self.assertIsNotNone(result)

    def test_prometheus_namespace_selector_must_include_monitor_namespace(self):
        prometheus, service, namespace_labels = fixture()
        prometheus["spec"]["serviceMonitorNamespaceSelector"] = {
            "matchLabels": {"kubernetes.io/metadata.name": "other"}}

        with self.assertRaisesRegex(ValueError, "do not select"):
            service_monitor(prometheus, service, namespace_labels, "kube-prometheus-stack")

    def test_null_monitor_selector_selects_nothing(self):
        prometheus, service, namespace_labels = fixture()
        prometheus["spec"]["serviceMonitorSelector"] = None

        with self.assertRaisesRegex(ValueError, "do not select"):
            service_monitor(prometheus, service, namespace_labels, "kube-prometheus-stack")

    def test_missing_metrics_port_fails_closed(self):
        prometheus, service, namespace_labels = fixture()
        service = copy.deepcopy(service)
        service["spec"]["ports"][0]["name"] = "http"

        with self.assertRaisesRegex(ValueError, "port is missing"):
            service_monitor(prometheus, service, namespace_labels, "kube-prometheus-stack")

    def test_duplicate_metrics_ports_fail_closed(self):
        prometheus, service, namespace_labels = fixture()
        service = copy.deepcopy(service)
        service["spec"]["ports"].append({"name": "metrics", "port": 8081})

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            service_monitor(prometheus, service, namespace_labels, "kube-prometheus-stack")


if __name__ == "__main__":
    unittest.main()
