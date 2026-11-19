# SPDX-License-Identifier: MIT
import unittest

from scripts.operations.arguments import parser_for
from scripts.operations.capabilities import monitoring_node_network as capability
from scripts.operations.models import OperationError
from scripts.operations.monitoring import node_network_rules as rules


class NodeNetworkRulesTest(unittest.TestCase):
    def build(self, interfaces, **changes):
        values = {
            "namespace": "monitoring", "release": "stack", "interfaces": interfaces, "hold": 120,
            "age": 120, "severity": "warning",
            "thresholds": {key: default for key, _, default in capability.OPTIONS},
            "messages": {name: {"summary": name, "description": "Check the target."}
                         for name in rules.MESSAGES},
        }
        return rules.network_rules(**{**values, **changes})

    def test_registers_only_exact_interfaces_and_one_tcp_rule_per_node(self):
        first = capability._interface("node-exporter=[2001:db8::1]:9100|device=eth0|capacity_mbps=100")
        second = {**first, "device": "bond0", "capacityMbps": None}
        manifest = self.build([first, second])
        self.assertEqual(manifest, self.build([second, first]))
        generated = manifest["spec"]["groups"][0]["rules"]
        self.assertEqual(len(generated), 13)
        self.assertTrue(all(row["labels"]["risk_id"] == "R-NET-05" for row in generated))
        for row in (row for row in generated if row["alert"] == "AtlasNodeNetworkBandwidthHigh"):
            self.assertIn('instance="[2001:db8::1]:9100"', row["expr"])
            device = row["labels"]["device"]
            self.assertIn(f'device="{device}"', row["expr"])
            if device == "eth0":
                self.assertIn(" / 12500000", row["expr"])
                self.assertNotIn("node_network_speed_bytes", row["expr"])
            else:
                self.assertIn("node_network_speed_bytes", row["expr"])

    def test_rejects_unregistered_interfaces_and_unsafe_parameters(self):
        interface = capability._interface("node=cp1:9100|device=eth0")
        for targets in ([], [interface, interface], [{**interface, "device": "eth0\n"}],
                        [{**interface, "instance": "{{ $value }}"}], [{**interface, "capacityMbps": 0}]):
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                self.build(targets)
        for raw in ("node=cp1:9100", "node=cp1:9100|device=eth0|device=eth1",
                    "node=cp1:9100|device=eth0|capacity_mbps=100.5", "node=cp1:9100|device=eth0|region=A"):
            with self.subTest(raw=raw), self.assertRaises(OperationError):
                capability._interface(raw)

    def test_cli_handles_all_locales_and_basis_point_thresholds(self):
        for language in ("en", "zh_CN", "zh_TW"):
            parser = parser_for(language)
            with self.subTest(language=language):
                self.assertIn("--monitoring-network-drop-basis-points", parser.format_help())
                args = parser.parse_args(["--operation", capability.NAME, "--monitoring-network-drop-basis-points", "25"])
                self.assertEqual(args.monitoring_network_drop_basis_points, 25)

    def test_ruleset_names_allow_independent_cohorts_and_files_fit_operator_budget(self):
        interface = capability._interface("node-exporter=cp1:9100|device=eth0")
        manifest = self.build([interface], ruleset="rack-a")
        self.assertEqual(manifest["metadata"]["name"], "atlas-node-network-rack-a")
        group = manifest["spec"]["groups"][0]
        self.assertEqual(group["name"], "atlas.node.network.rack-a")
        self.assertTrue(all(rule["labels"]["network_ruleset"] == "rack-a" for rule in group["rules"]))
        with self.assertRaises(ValueError):
            self.build([interface], ruleset="rack/a")
        large_scope = [{**interface, "instance": f"node-{number}:9100"} for number in range(64)]
        with self.assertRaises(rules.RuleSizeError):
            self.build(large_scope)
