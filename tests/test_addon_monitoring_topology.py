# SPDX-License-Identifier: MIT
import re
import unittest
from pathlib import Path

from scripts.addons.monitoring_topology import (
    agent_values, alloy_remote_write_config, center_values, verify_rendered_mode,
)
from scripts.addons.sources import yaml_module


class MonitoringTopologyTest(unittest.TestCase):
    def test_guided_agent_prevalidates_both_push_pipelines_before_marker(self):
        script = (Path(__file__).resolve().parents[1] / "03-install-addons.sh").read_text()
        flow = script[script.index("monitoring_topology_setup() {"):script.index("show_menu() {")]
        validation = flow.index("validate_monitoring_topology_marker agent")
        setup = flow.index("monitoring_full_setup agent")
        self.assertLess(flow.index("monitoring_topology.py"), validation)
        self.assertLess(flow.index("logging_values.py"), validation)
        self.assertLess(validation, setup)
        self.assertNotIn("ensure_monitoring_topology_marker agent", flow)
        self.assertIn('ask \'phase03.monitoring.remote_loki_url\'', flow)
        self.assertIn('ask \'phase03.monitoring.remote_write_url\'', flow)
        self.assertIn("monitoring_full_setup agent", flow)

    def test_center_mode_installs_inside_the_dedicated_monitoring_cluster(self):
        script = (Path(__file__).resolve().parents[1] / "03-install-addons.sh").read_text()
        flow = script[script.index("monitoring_topology_setup() {"):script.index("show_menu() {")]
        center = flow[flow.index("    center)\n"):flow.index("    agent)\n")]
        setup = script[script.index("monitoring_full_setup() {"):script.index("\nmonitoring_topology_setup() {")]
        self.assertIn("phase03.monitoring.confirm_center_topology", center)
        self.assertIn("monitoring_full_setup center", center)
        self.assertLess(setup.index('if [ "$mode" = center ]'), setup.index("install_selected 4"))
        self.assertIn('ATLAS_CLUSTER_PURPOSE="$(read_cluster_purpose)"', script)
        self.assertIn('[ "$ATLAS_CLUSTER_PURPOSE" = monitoring ] && mode=center', center)

    def test_topology_marker_is_written_only_after_successful_component_setup(self):
        script = (Path(__file__).resolve().parents[1] / "03-install-addons.sh").read_text()
        setup = script[script.index("monitoring_full_setup() {"):script.index("\nmonitoring_topology_setup() {")]
        agent_marker = setup.index("ensure_monitoring_topology_marker agent")
        logs_ready = setup.index(
            'kubectl -n logging wait --for=condition=Ready pod -l app.kubernetes.io/instance=alloy'
        )
        center_marker = setup.index("ensure_monitoring_topology_marker center")
        self.assertLess(logs_ready, agent_marker)
        self.assertLess(logs_ready, center_marker)
        guided = script[script.index("monitoring_topology_setup() {"):script.index("show_menu() {")]
        self.assertNotIn("ensure_monitoring_topology_marker center", guided)

    def test_topology_marker_read_is_fail_closed_and_precedes_creation(self):
        script = (Path(__file__).resolve().parents[1] / "03-install-addons.sh").read_text()
        marker = script[script.index("read_monitoring_topology_marker() {"):script.index("\nmonitoring_topology_mode() {")]
        self.assertIn("--ignore-not-found=true", marker)
        self.assertIn("|| return 1", marker)
        validate = script[script.index("validate_monitoring_topology_marker() {"):script.index("\nmonitoring_topology_mode() {")]
        self.assertIn('marker="$(read_monitoring_topology_marker)" \\\n    || die', validate)
        ensure = script[script.index("ensure_monitoring_topology_marker() {"):script.index("\nmonitoring_topology_mode() {")]
        self.assertLess(ensure.index("validate_monitoring_topology_marker"), ensure.index("kubectl create namespace"))
        self.assertLess(ensure.index("kubectl create namespace"), ensure.index("kind: ConfigMap"))
        mode_reader = script[script.index("monitoring_topology_mode() {"):script.index("\nlonghorn_replica_guidance() {")]
        self.assertIn("read_monitoring_topology_marker", mode_reader)
        self.assertIn("topology_marker_read_failed", mode_reader)
        self.assertNotIn("|| true", mode_reader)
        readers = re.findall(
            r'(?m)^\s*\w+="\$\(monitoring_topology_mode\)"\s*\\\n\s*\|\| die .+$', script
        )
        self.assertEqual(len(readers), 4)

    def test_chart_topology_render_check_precedes_release_mutation(self):
        script = (Path(__file__).resolve().parents[1] / "03-install-addons.sh").read_text()
        install = script[script.index("install_downloaded_chart() {"):script.index("\nrelease_present() {")]
        render = install.index('helm template "$release" "$chart"')
        topology = install.index("--verify-rendered-manifest")
        upgrade = install.index('helm upgrade --install "$release"')
        self.assertLess(render, topology)
        self.assertLess(topology, upgrade)
        self.assertIn('"$directory/preflight.yaml"', install[topology:upgrade])

    def test_center_enables_remote_write_receiver_and_keeps_central_rules(self):
        values = center_values("zh_CN")
        self.assertTrue(values["prometheus"]["prometheusSpec"]["enableRemoteWriteReceiver"])
        self.assertIn("additionalPrometheusRulesMap", values)

    def test_agent_forwards_with_identity_tls_and_secret_reference_only(self):
        values = agent_values("prod-east", "https://metrics.example.com/api/v1/write",
                              "central-write-token", "central-ca")
        self.assertTrue(values["prometheus"]["agentMode"])
        spec = values["prometheus"]["prometheusSpec"]
        self.assertEqual(spec["externalLabels"], {"cluster": "prod-east"})
        self.assertEqual(spec["remoteWrite"][0]["url"], "https://metrics.example.com/api/v1/write")
        self.assertEqual(spec["remoteWrite"][0]["authorization"]["credentials"],
                         {"name": "central-write-token", "key": "token"})
        self.assertEqual(spec["remoteWrite"][0]["tlsConfig"]["ca"]["secret"],
                         {"name": "central-ca", "key": "ca.crt"})
        self.assertNotIn("additionalPrometheusRulesMap", values)
        self.assertNotIn("storageSpec", spec)
        self.assertIn("extraHostVolumeMounts", values["prometheus-node-exporter"])
        self.assertFalse(values["kubeProxy"]["serviceMonitor"]["enabled"])
        self.assertFalse(values["alertmanager"]["enabled"])
        self.assertFalse(values["grafana"]["enabled"])
        self.assertFalse(values["defaultRules"]["create"])
        self.assertNotIn("password", yaml_module().safe_dump(values))

    def test_chart_preflight_requires_true_agent_resource_without_rules(self):
        agent = """apiVersion: monitoring.coreos.com/v1alpha1
kind: PrometheusAgent
spec:
  externalLabels: {cluster: prod-east}
  remoteWrite: [{url: https://metrics.example.com/api/v1/write}]
"""
        self.assertEqual(verify_rendered_mode("agent", agent),
                         {"mode": "agent", "prometheusAgent": 1})
        for wrong in (
            "apiVersion: monitoring.coreos.com/v1\nkind: Prometheus\nspec: {}\n",
            agent + "---\napiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\nspec: {}\n",
            agent + "---\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: center-grafana\n  labels:\n    app.kubernetes.io/name: grafana\nspec: {}\n",
        ):
            with self.subTest(manifest=wrong), self.assertRaises(ValueError):
                verify_rendered_mode("agent", wrong)

    def test_chart_preflight_parses_helm_crd_yaml_equals_enum(self):
        manifest = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
spec:
  validation:
    openAPIV3Schema:
      properties:
        value:
          enum: [=]
---
apiVersion: monitoring.coreos.com/v1alpha1
kind: PrometheusAgent
spec:
  externalLabels: {cluster: prod-east}
  remoteWrite: [{url: https://metrics.example.com/api/v1/write}]
"""
        self.assertEqual(verify_rendered_mode("agent", manifest),
                         {"mode": "agent", "prometheusAgent": 1})

    def test_chart_preflight_requires_center_receiver(self):
        center = """apiVersion: monitoring.coreos.com/v1
kind: Prometheus
spec:
  enableRemoteWriteReceiver: true
"""
        self.assertEqual(verify_rendered_mode("center", center), {"mode": "center", "prometheus": 1})
        with self.assertRaises(ValueError):
            verify_rendered_mode("center", center.replace("true", "false"))

    def test_agent_requires_unique_safe_identity_https_and_secret_refs(self):
        for args in (("", "https://metrics.example.com/write", "secret"),
                     ("prod_east", "https://metrics.example.com/write", "secret"),
                     ("prod-east", "https://metrics.example.com/write", "secret"),
                     ("prod-east", "http://metrics.example.com/write", "secret"),
                     ("prod-east", "https://user:pass@metrics.example.com/write", "secret"),
                     ("prod-east", "https://metrics.example.com/write", "bad/name")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                agent_values(*args)

    def test_alloy_uses_authenticated_https_endpoint(self):
        output = alloy_remote_write_config("https://logs.example.com")
        self.assertIn("https://logs.example.com/loki/api/v1/push", output)
        self.assertIn('sys.env("ATLAS_LOKI_TOKEN")', output)
        self.assertNotIn("http://", output)
        with self.assertRaises(ValueError):
            alloy_remote_write_config("http://logs.example.com")


if __name__ == "__main__":
    unittest.main()
