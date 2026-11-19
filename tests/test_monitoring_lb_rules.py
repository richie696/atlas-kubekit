# SPDX-License-Identifier: MIT
import unittest

from scripts.operations.monitoring import lb_rules


MESSAGE_KEYS = ("target", "backend", "capacity", "connection_errors", "queue", "keepalived",
                "vip_missing", "vip_no_owner", "vip_multiple", "vip_drift")


class LoadBalancerRulesTest(unittest.TestCase):
    def render(self, nodes=None, threshold=80, drift=4, vips=None):
        nodes = nodes or [{"name": "lb1", "address": "10.20.1.12"},
                          {"name": "lb2", "address": "10.20.1.19"}]
        texts = {key: key for key in MESSAGE_KEYS}
        details = {key: "Check LB service, route and telemetry." for key in MESSAGE_KEYS}
        return lb_rules.load_balancer_rules("monitoring", "stack", nodes, vips or ["10.20.1.9"],
                                            60, 120, "warning", threshold, 5, drift, texts, details)

    def test_renders_per_node_target_and_backend_capacity_rules(self):
        value = self.render()
        rules = value["spec"]["groups"][0]["rules"]
        self.assertEqual(len(rules), 17)
        by_name = {(rule["alert"], rule["labels"].get("node")): rule for rule in rules}
        target = by_name[("AtlasLBTargetOrTelemetryMissing", "lb1")]
        self.assertIn('instance="10.20.1.12:8405"', target["expr"])
        self.assertIn('lb="lb1"', target["expr"])
        self.assertIn('metrics_endpoint="haproxy"', target["expr"])
        backend = by_name[("AtlasLBBackendUnavailableOrMetricsMissing", "lb1")]["expr"]
        self.assertIn("haproxy_backend_active_servers", backend)
        self.assertIn("haproxy_backend_backup_servers", backend)
        capacity = by_name[("AtlasLBFrontendCapacityHigh", "lb1")]
        self.assertIn("0.8", capacity["expr"])
        self.assertEqual(capacity["labels"]["risk_id"], "R-LB-03")
        self.assertEqual(capacity["annotations"]["observed_value"], "{{ $value }}")
        errors = by_name[("AtlasLBBackendConnectionErrors", "lb1")]["expr"]
        self.assertIn("increase(haproxy_backend_connection_errors_total", errors)
        self.assertIn('proxy!="atlas-vip-metrics"', errors)
        self.assertIn("> 5", errors)
        self.assertIn("[5m]", errors)
        queue = by_name[("AtlasLBBackendQueueNonZero", "lb1")]
        self.assertIn("haproxy_backend_current_queue", queue["expr"])
        self.assertEqual(queue["labels"]["risk_id"], "R-LB-03")
        inactive = by_name[("AtlasLBKeepalivedInactive", "lb1")]["expr"]
        self.assertIn("atlas_keepalived_active", inactive)
        self.assertTrue(inactive.endswith(") > 0"))

    def test_vip_rules_cover_no_owner_multiple_owners_missing_and_drift(self):
        rules = self.render(vips=["10.20.1.9", "10.20.1.10"], drift=7)["spec"]["groups"][0]["rules"]
        by_alert = {}
        for rule in rules:
            by_alert.setdefault(rule["alert"], []).append(rule)
        for alert in ("AtlasLBVipNoOwner", "AtlasLBVipMultipleOwners", "AtlasLBVipOwnershipDrift"):
            self.assertEqual({rule["labels"]["vip"] for rule in by_alert[alert]}, {"10.20.1.9", "10.20.1.10"})
        self.assertIn("== 0", by_alert["AtlasLBVipNoOwner"][0]["expr"])
        self.assertIn("> 1", by_alert["AtlasLBVipMultipleOwners"][0]["expr"])
        self.assertIn("changes(", by_alert["AtlasLBVipOwnershipDrift"][0]["expr"])
        self.assertIn(">= 7", by_alert["AtlasLBVipOwnershipDrift"][0]["expr"])
        self.assertIn("absent_over_time(", by_alert["AtlasLBVipTelemetryMissing"][0]["expr"])

    def test_capacity_threshold_is_configurable_and_range_is_enforced(self):
        value = self.render(threshold=92)
        capacity = next(rule for rule in value["spec"]["groups"][0]["rules"]
                        if rule["alert"] == "AtlasLBFrontendCapacityHigh")
        self.assertIn("0.92", capacity["expr"])
        for threshold in (0, 100, True):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                self.render(threshold=threshold)
        for drift in (1, 101, True):
            with self.subTest(drift=drift), self.assertRaises(ValueError):
                self.render(drift=drift)

    def test_duplicate_nodes_vips_invalid_target_and_message_are_rejected(self):
        duplicate = [{"name": "lb1", "address": "10.0.0.1"}, {"name": "lb1", "address": "10.0.0.2"}]
        with self.assertRaises(ValueError):
            self.render(nodes=duplicate)
        with self.assertRaises(ValueError):
            self.render(vips=["10.0.0.9", "10.0.0.9"])
        with self.assertRaises(ValueError):
            self.render(vips=["127.0.0.1"])
        texts = {key: key for key in MESSAGE_KEYS}
        texts["target"] = "{{ unsafe }}"
        details = {key: "Check the service and telemetry." for key in MESSAGE_KEYS}
        with self.assertRaises(ValueError):
            lb_rules.load_balancer_rules("monitoring", "stack", [{"name": "lb1", "address": "10.0.0.1"}],
                                         ["10.0.0.9"], 60, 120, "warning", 80, 5, 4, texts, details)


if __name__ == "__main__":
    unittest.main()
