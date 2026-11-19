# SPDX-License-Identifier: MIT
import unittest

from scripts.operations.monitoring.rules import node_readiness_rule


class NodeReadinessRuleTest(unittest.TestCase):
    def arguments(self):
        return dict(namespace="monitoring", release="monitoring-stack", nodes=["w1", "cp1"],
                    hold_seconds=10, max_age_seconds=120, severity="warning",
                    summary="Node readiness or monitoring data unavailable",
                    description="Inspect the node and the metric source before declaring recovery.")

    def test_render_is_stable_and_contains_missing_and_stale_guards(self):
        args = self.arguments()
        value = node_readiness_rule(**args)
        self.assertEqual(args["nodes"], ["w1", "cp1"])
        self.assertEqual(value["metadata"]["labels"]["release"], "monitoring-stack")
        rules = value["spec"]["groups"][0]["rules"]
        self.assertEqual([rule["labels"]["node"] for rule in rules], ["cp1", "w1"])
        for rule in rules:
            self.assertIn("absent(", rule["expr"])
            self.assertIn("timestamp(", rule["expr"])
            self.assertIn("count(", rule["expr"])
            self.assertEqual(rule["for"], "10s")
            self.assertEqual(rule["labels"]["risk_id"], "R-NODE-01")
        resources = value["spec"]["groups"][1]["rules"]
        self.assertEqual(len(resources), 16)
        self.assertEqual({rule["alert"] for rule in resources}, {
            "AtlasNodeCPUUsageHigh", "AtlasNodeCPUTelemetryMissing",
            "AtlasNodeMemoryUsageHigh", "AtlasNodeMemoryTelemetryMissing",
            "AtlasNodeFilesystemUsageHigh", "AtlasNodeFilesystemTelemetryMissing",
            "AtlasNodeInodeUsageHigh", "AtlasNodeInodeTelemetryMissing"})
        cpu = next(rule for rule in resources if rule["alert"] == "AtlasNodeCPUUsageHigh")
        self.assertIn("> 90", cpu["expr"])
        self.assertIn('node_uname_info{nodename="cp1"}', cpu["expr"])
        self.assertIn("time() -", cpu["expr"])
        self.assertEqual(cpu["labels"]["risk_id"], "R-NODE-02")
        self.assertEqual(cpu["annotations"]["observed_value"], "{{ $value }}%")
        cpu_missing = next(rule for rule in resources
                           if rule["alert"] == "AtlasNodeCPUTelemetryMissing")
        self.assertIn("timestamp(", cpu_missing["expr"])
        self.assertIn("> time() - 120", cpu_missing["expr"])
        self.assertIn("<= time() + 10", cpu_missing["expr"])

    def test_resource_thresholds_are_bounded_and_configurable(self):
        value = node_readiness_rule(**self.arguments(), cpu_percent=80, memory_percent=75,
                                    filesystem_percent=70, inode_percent=65)
        resources = value["spec"]["groups"][1]["rules"]
        expected = {"AtlasNodeCPUUsageHigh": "> 80", "AtlasNodeMemoryUsageHigh": "> 75",
                    "AtlasNodeFilesystemUsageHigh": "> 70", "AtlasNodeInodeUsageHigh": "> 65"}
        for name, fragment in expected.items():
            self.assertIn(fragment, next(rule for rule in resources if rule["alert"] == name)["expr"])
        for changes in ({"cpu_percent": 0}, {"memory_percent": 100},
                        {"filesystem_percent": True}, {"inode_percent": "80"},
                        {"resource_messages": []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                node_readiness_rule(**self.arguments(), **changes)

    def test_rejects_injection_duplicates_and_bad_durations(self):
        for changes in ({"nodes": ['node"} or vector(1)']}, {"nodes": ["cp1", "cp1"]},
                        {"nodes": []}, {"hold_seconds": True}, {"max_age_seconds": 0},
                        {"release": "bad/release"}, {"summary": '{{ query "secret" }}'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                node_readiness_rule(**(self.arguments() | changes))


if __name__ == "__main__":
    unittest.main()
