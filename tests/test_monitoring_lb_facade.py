# SPDX-License-Identifier: MIT
import unittest
from unittest.mock import patch

from scripts.operations.capabilities import monitoring_lb
from scripts.operations.models import OperationError


class LoadBalancerMonitoringFacadeTest(unittest.TestCase):
    def setUp(self):
        self.values = {
            "access": {"configuration": {"cluster": {"statsAllowedCIDR": ""}, "nodes": []},
                       "cidr": "10.0.0.0/8"},
            "collection": {"configuration": {"cluster": {"statsAllowedCIDR": "10.0.0.0/8"},
                                             "nodes": []},
                           "namespace": "monitoring", "release": "stack", "prometheusName": "prom",
                           "nodes": [{"name": "lb1", "address": "10.0.0.1"}], "expected": {}},
            "rules": {"configuration": {"cluster": {"statsAllowedCIDR": "10.0.0.0/8"},
                                         "nodes": []},
                      "namespace": "monitoring", "release": "stack", "prometheusName": "prom",
                      "nodes": [{"name": "lb1", "address": "10.0.0.1"}], "expected": {}},
            "vipMetrics": {"configuration": {"cluster": {"statsAllowedCIDR": ""}, "nodes": []},
                           "expected": {"configurationRevision": "rev", "nodes": {}}},
        }

    def test_parameters_require_one_shared_target(self):
        with patch.object(monitoring_lb.lb_stats, "parameters", side_effect=lambda value: value), \
             patch.object(monitoring_lb.collection, "parameters", side_effect=lambda value: value), \
             patch.object(monitoring_lb.rules, "parameters", side_effect=lambda value: value), \
             patch.object(monitoring_lb.vip_metrics, "parameters", side_effect=lambda value: value), \
             patch.object(monitoring_lb, "normalize", side_effect=lambda value: value), \
             patch.object(monitoring_lb, "_configured", return_value=self.values["collection"]["configuration"]):
            self.assertEqual(monitoring_lb.parameters(self.values), self.values)
            changed = {**self.values, "rules": {**self.values["rules"], "release": "other"}}
            with self.assertRaises(OperationError):
                monitoring_lb.parameters(changed)
            changed_configuration = {**self.values, "rules": {
                **self.values["rules"],
                "configuration": {"cluster": {"statsAllowedCIDR": "192.0.2.0/24"}, "nodes": []},
            }}
            with self.assertRaises(OperationError):
                monitoring_lb.parameters(changed_configuration)

    def test_apply_publishes_scraping_before_rules_and_reports_unverified_state(self):
        calls = []
        checkpoints = []
        with patch.object(monitoring_lb, "parameters", side_effect=lambda value: value), \
             patch.object(monitoring_lb.lb_stats, "apply", side_effect=lambda *args: calls.append("access") or {"state": "written"}), \
             patch.object(monitoring_lb.vip_metrics, "apply", side_effect=lambda *args: calls.append("vip") or {"state": "written"}), \
             patch.object(monitoring_lb.collection, "apply", side_effect=lambda *args: calls.append("scrape") or {"state": "written"}), \
             patch.object(monitoring_lb.rules, "apply", side_effect=lambda *args: calls.append("rules") or {"state": "written"}):
            result = monitoring_lb.apply(self.values, object(), checkpoints.append, "plan")
        self.assertEqual(calls, ["access", "vip", "scrape", "rules"])
        self.assertEqual(checkpoints, ["monitoring-lb-access-start", "monitoring-lb-access-complete",
                                       "monitoring-lb-vip-exporter-start", "monitoring-lb-vip-exporter-complete",
                                       "monitoring-lb-scrape-start", "monitoring-lb-scrape-complete",
                                       "monitoring-lb-rules-complete"])
        self.assertIn("independent checks", result["verification"])

    def test_continuation_accepts_only_unchanged_or_already_desired_resources(self):
        old_prom = {"uid": "prom", "specHash": "p"}
        scrape = self.values["collection"]
        scrape["prometheusName"] = "prom"
        scrape["expected"] = {"prometheus": old_prom, "resources": {
            "service": None, "endpoints": None, "servicemonitor": None}}
        alerting = self.values["rules"]
        alerting["prometheusName"] = "prom"
        alerting["expected"] = {"prometheus": old_prom, "rule": None}
        manifests = (
            {"kind": "Service", "spec": {"wanted": True}},
            {"kind": "Endpoints", "subsets": {"wanted": True}},
            {"kind": "ServiceMonitor", "spec": {"wanted": True}},
        )
        current_scrape = {"prometheus": old_prom, "resources": {
            "service": {"uid": "svc", "resourceVersion": "2", "specHash": "desired"},
            "endpoints": None, "servicemonitor": None}}
        current_rules = {"prometheus": old_prom, "rule": None}
        context = type("Context", (), {"client": object()})()
        with patch.object(monitoring_lb, "parameters", side_effect=lambda value: value), \
             patch.object(monitoring_lb.collection, "_manifests", return_value=manifests), \
             patch.object(monitoring_lb.lb_collection, "capture", return_value=current_scrape), \
             patch.object(monitoring_lb.rules, "_manifest", return_value={"spec": {"rules": []}}), \
             patch.object(monitoring_lb.publication, "capture", return_value=current_rules), \
             patch.object(monitoring_lb.vip_metrics, "_snapshot", return_value={"configurationRevision": "rev2", "nodes": {}}), \
             patch.object(monitoring_lb, "fingerprint", side_effect=lambda value: "desired" if value.get("wanted") else "rule-desired"):
            continued = monitoring_lb.continue_parameters(self.values, context)
        self.assertEqual(continued["collection"]["expected"], current_scrape)
        self.assertEqual(continued["rules"]["expected"], current_rules)

    def test_continuation_refuses_unrelated_resource_drift(self):
        old_prom = {"uid": "prom", "specHash": "p"}
        scrape = self.values["collection"]
        scrape["prometheusName"] = "prom"
        scrape["expected"] = {"prometheus": old_prom, "resources": {
            "service": None, "endpoints": None, "servicemonitor": None}}
        self.values["rules"]["prometheusName"] = "prom"
        self.values["rules"]["expected"] = {"prometheus": old_prom, "rule": None}
        current_scrape = {"prometheus": old_prom, "resources": {
            "service": {"uid": "svc", "resourceVersion": "2", "specHash": "foreign-change"},
            "endpoints": None, "servicemonitor": None}}
        context = type("Context", (), {"client": object()})()
        manifests = ({"kind": "Service", "spec": {"wanted": True}},
                     {"kind": "Endpoints", "subsets": {}},
                     {"kind": "ServiceMonitor", "spec": {}})
        with patch.object(monitoring_lb, "parameters", side_effect=lambda value: value), \
             patch.object(monitoring_lb.collection, "_manifests", return_value=manifests), \
             patch.object(monitoring_lb.lb_collection, "capture", return_value=current_scrape):
            with self.assertRaises(OperationError):
                monitoring_lb.continue_parameters(self.values, context)


if __name__ == "__main__":
    unittest.main()
