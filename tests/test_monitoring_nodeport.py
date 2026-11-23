# SPDX-License-Identifier: MIT
import unittest

from scripts.addons.monitoring_nodeport import ensure_gateway_ready, nodeport


class MonitoringNodePortTests(unittest.TestCase):
    def fixture(self):
        services = {"items": [{"metadata": {"labels": {
            "gateway.envoyproxy.io/owning-gateway-namespace": "monitoring",
            "gateway.envoyproxy.io/owning-gateway-name": "atlas-monitoring-public"}},
            "spec": {"type": "NodePort", "ports": [
                {"port": 443, "protocol": "TCP", "nodePort": 32443}]}}]}
        nodes = {"items": [
            {"status": {"conditions": [{"type": "Ready", "status": "True"}],
                         "addresses": [{"type": "InternalIP", "address": "10.20.1.34"}]}},
            {"status": {"conditions": [{"type": "Ready", "status": "False"}],
                         "addresses": [{"type": "InternalIP", "address": "10.20.1.35"}]}},
            {"status": {"conditions": [{"type": "Ready", "status": "True"}],
                         "addresses": [{"type": "InternalIP", "address": "10.20.1.36"}]}},
        ]}
        return services, nodes

    def test_selects_only_owned_https_nodeport_and_ready_nodes(self):
        services, nodes = self.fixture()
        self.assertEqual(nodeport(services, nodes), {
            "nodePort": 32443,
            "backends": ["10.20.1.34:32443", "10.20.1.36:32443"]})

    def test_rejects_missing_duplicate_or_non_nodeport_service(self):
        services, nodes = self.fixture()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            nodeport({"items": []}, nodes)
        services["items"][0]["spec"]["type"] = "LoadBalancer"
        with self.assertRaisesRegex(ValueError, "not NodePort"):
            nodeport(services, nodes)

    def test_rejects_ready_node_with_ambiguous_internal_addresses(self):
        services, nodes = self.fixture()
        nodes["items"][0]["status"]["addresses"].append(
            {"type": "InternalIP", "address": "10.20.1.37"})
        with self.assertRaisesRegex(ValueError, "exactly one"):
            nodeport(services, nodes)

    def test_gateway_must_be_programmed_and_have_ready_endpoints(self):
        gateway = {"status": {"conditions": [{"type": "Programmed", "status": "True"}]}}
        slices = {"items": [{"endpoints": [{"addresses": ["10.245.1.8"],
                                                "conditions": {"ready": True}}]}]}
        ensure_gateway_ready(slices, gateway)
        with self.assertRaisesRegex(ValueError, "not programmed"):
            ensure_gateway_ready(slices, {"status": {"conditions": []}})
        with self.assertRaisesRegex(ValueError, "no Ready"):
            ensure_gateway_ready({"items": []}, gateway)


if __name__ == "__main__":
    unittest.main()
