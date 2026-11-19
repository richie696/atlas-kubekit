# SPDX-License-Identifier: MIT
import unittest

from scripts.operations.monitoring.scrape_rules import core_dns_rules


class CoreDNSRuleTest(unittest.TestCase):
    def render(self, targets=None, threshold=5, window=300, latency_ms=100, deployment=None,
               min_replicas=2, min_requests=100):
        return core_dns_rules("monitoring", "stack", targets or [{"job": "coredns", "instance": "dns-0:9153"}],
            10, 120, "warning", threshold, window, latency_ms, "CoreDNS 指标不可用", "检查指标目标。",
            "DNS 异常响应比例过高", "异常响应超过 5%。", "DNS P95 延迟过高", "P95 超过 100 毫秒。",
            deployment or {"namespace": "kube-system", "name": "coredns"}, min_replicas,
            "CoreDNS 副本不足", "至少需要 2 个可用副本。", latency_min_requests=min_requests)

    def test_builds_endpoint_and_error_ratio_rules_with_explicit_target(self):
        value = self.render()
        rules = value["spec"]["groups"][0]["rules"]
        self.assertEqual([rule["alert"] for rule in rules], [
            "AtlasCoreDNSTargetOrTelemetryMissing", "AtlasCoreDNSResponseErrorRateHigh",
            "AtlasCoreDNSP95LatencyHigh", "AtlasCoreDNSReplicasBelowMinimum"])
        self.assertEqual(rules[0]["labels"]["risk_id"], "R-NET-01")
        self.assertIn('coredns_build_info{job="coredns",instance="dns-0:9153"}', rules[0]["expr"])
        self.assertIn('rcode=~"SERVFAIL|REFUSED"', rules[1]["expr"])
        self.assertIn("[300s]", rules[1]["expr"])
        self.assertIn("> 5", rules[1]["expr"])
        self.assertIn("vector(0)", rules[1]["expr"])
        self.assertTrue(rules[1]["expr"].startswith("((100 * "))
        self.assertTrue(rules[1]["expr"].endswith(" > 0))"))
        self.assertEqual(rules[1]["annotations"]["observed_value"], '{{ printf "%.2f%%" $value }}')
        self.assertIn("coredns_dns_request_duration_seconds_bucket", rules[2]["expr"])
        self.assertIn("coredns_dns_request_duration_seconds_count", rules[2]["expr"])
        self.assertIn("histogram_quantile(0.95", rules[2]["expr"])
        self.assertIn("[300s]", rules[2]["expr"])
        self.assertIn("> 0.100", rules[2]["expr"])
        self.assertIn(">= 100", rules[2]["expr"])
        self.assertIn("P95 超过 100 毫秒", rules[2]["annotations"]["description"])
        self.assertEqual(rules[2]["annotations"]["observed_value"], '{{ printf "%.3f" $value }}s')
        self.assertIn('min by (namespace,deployment)', rules[3]["expr"])
        self.assertIn('namespace="kube-system",deployment="coredns"', rules[3]["expr"])
        self.assertIn("< 2", rules[3]["expr"])
        self.assertIn("absent(kube_deployment_status_replicas_available", rules[3]["expr"])
        self.assertEqual(rules[3]["labels"]["risk_id"], "R-NET-01")

    def test_rejects_invalid_threshold_window_and_targets(self):
        for threshold, window, targets, latency_ms in (
            (0, 300, None, 100), (101, 300, None, 100), (5, 4, None, 100),
            (5, True, None, 100), (5, 300, None, 0), (5, 300, None, 60001),
            (5, 300, [{"job": "coredns", "instance": "dns"}] * 2, 100),
            (5, 300, [{"job": "bad\njob", "instance": "dns"}], 100),
        ):
            with self.subTest(threshold=threshold, window=window, targets=targets, latency_ms=latency_ms), self.assertRaises(ValueError):
                self.render(targets=targets, threshold=threshold, window=window, latency_ms=latency_ms)
        for min_requests in (0, True, 10000001):
            with self.subTest(min_requests=min_requests), self.assertRaises(ValueError):
                self.render(min_requests=min_requests)
        for deployment, minimum in (({"namespace": "bad/ns", "name": "coredns"}, 2),
                                    ({"namespace": "kube-system", "name": "coredns"}, 0),
                                    ({"namespace": "kube-system", "name": "coredns"}, True)):
            with self.subTest(deployment=deployment, minimum=minimum), self.assertRaises(ValueError):
                self.render(deployment=deployment, min_replicas=minimum)


if __name__ == "__main__":
    unittest.main()
