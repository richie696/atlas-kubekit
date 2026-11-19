# SPDX-License-Identifier: MIT
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone
import json
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from scripts.operations.monitoring import prometheus


def vector(*samples, timestamp_values=False):
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"node": name, "instance": str(index)},
         "value": [1000, str(timestamp if timestamp_values else value)]}
        for index, (name, timestamp, value) in enumerate(samples)
    ]}}


def target(job, instance, health="up", timestamp=990, scrape_pool=None):
    last_scrape = datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"labels": {"job": job, "instance": instance}, "health": health,
            "scrapePool": scrape_pool or "serviceMonitor/default/" + job + "/0",
            "lastScrape": last_scrape, "scrapeUrl": "https://private.example/metrics",
            "lastError": "sensitive error details"}


class PrometheusQueryTest(unittest.TestCase):
    def test_risk_sample_parser_uses_source_age_and_rejects_mismatched_labels(self):
        labels = {"job": "node", "instance": "node:9100"}

        def payload(*samples):
            return {"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {**labels, "cpu": str(index)}, "value": [1000, str(timestamp)]}
                for index, timestamp in enumerate(samples)
            ]}}

        for samples, state in (((), "missing"), ((995,), "fresh"), ((800,), "stale"),
                               ((1011,), "stale"), ((980, 999), "ambiguous")):
            with self.subTest(samples=samples):
                value = prometheus.parse_metric_timestamp(payload(*samples), labels, 120, 1000)
                self.assertEqual(value["state"], state)
        for value, expected in ((payload(float("inf")), labels), (payload(995), {**labels, "job": "other"})):
            with self.assertRaises(ValueError):
                prometheus.parse_metric_timestamp(value, expected, 120, 1000)

    def test_missing_duplicate_stale_and_not_ready_are_distinct(self):
        samples = (("ready", 990, 1), ("failed", 990, 0),
                   ("stale", 700, 1), ("dup", 990, 1), ("dup", 991, 1))
        result = prometheus.parse_vector(vector(*samples), vector(*samples, timestamp_values=True),
                                         ["ready", "failed", "stale", "dup", "missing"], 120, 1000)
        states = {row["node"]: row["state"] for row in result["nodes"]}
        self.assertEqual(states, {"ready": "ready", "failed": "not-ready", "stale": "stale",
                                  "dup": "ambiguous", "missing": "missing"})
        self.assertEqual(result["state"], "unverified")

    def test_untrusted_endpoints_and_invalid_samples_are_rejected(self):
        for url in ("http://10.0.0.1:9090", "https://user:pass@example.com", "https://example.com/?token=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                prometheus.endpoint(url)
        with self.assertRaises(ValueError):
            prometheus.parse_vector(vector(("node", 1000, "nan")), vector(("node", 1000, 1), timestamp_values=True), ["node"], 120, 1000)

    def test_inconsistent_queries_are_unverified(self):
        with self.assertRaises(ValueError):
            prometheus.parse_vector(vector(("node", 1000, 1)), vector(), ["node"], 120, 1000)

    def test_actual_http_query_uses_fixed_expression_and_records_result(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.requested.append(self.path)
                is_timestamp = parse_qs(urlsplit(self.path).query)["query"][0] == prometheus.NODE_TIMESTAMP_QUERY
                payload = json.dumps(vector(("node", 990, 1), timestamp_values=is_timestamp)).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.requested = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = prometheus.collect("http://127.0.0.1:" + str(server.server_port), ["node"], 120, now=1000)
            self.assertEqual(result, {"state": "observed", "nodes": [{"node": "node", "state": "ready"}]})
            self.assertEqual([parse_qs(urlsplit(path).query)["query"][0] for path in server.requested],
                             [prometheus.NODE_READY_QUERY, prometheus.NODE_TIMESTAMP_QUERY])
            self.assertTrue(all(parse_qs(urlsplit(path).query)["time"] == ["1000"] for path in server.requested))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_target_inventory_distinguishes_missing_duplicate_down_stale_and_extra(self):
        expected = [{"scrapePool": "pool/a", "job": "a", "instance": "healthy:9100"},
                    {"scrapePool": "pool/b", "job": "b", "instance": "missing:9100"},
                    {"scrapePool": "pool/c", "job": "c", "instance": "duplicate:9100"},
                    {"scrapePool": "pool/d", "job": "d", "instance": "down:9100"},
                    {"scrapePool": "pool/e", "job": "e", "instance": "stale:9100"}]
        payload = {"status": "success", "data": {"activeTargets": [
            target("a", "healthy:9100", scrape_pool="pool/a"),
            target("c", "duplicate:9100", scrape_pool="pool/c"),
            target("c", "duplicate:9100", scrape_pool="pool/c"),
            target("d", "down:9100", "down", scrape_pool="pool/d"),
            target("e", "stale:9100", timestamp=700, scrape_pool="pool/e"),
            target("extra", "unexpected:9100", scrape_pool="pool/extra")]}}
        result = prometheus.parse_targets(payload, expected, 120, 1000)
        self.assertEqual(result["state"], "observed")
        self.assertFalse(result["healthy"])
        self.assertEqual({row["job"]: row["state"] for row in result["expected"]},
                         {"a": "healthy", "b": "missing", "c": "duplicate",
                          "d": "down", "e": "stale"})
        self.assertEqual(result["missingCount"], 1)
        self.assertEqual(result["issueCount"], 4)
        self.assertEqual(result["unexpectedCount"], 1)
        serialized = json.dumps(result)
        self.assertNotIn("sensitive error details", serialized)
        self.assertNotIn("private.example", serialized)

    def test_target_inventory_is_healthy_only_for_exact_fresh_set(self):
        result = prometheus.parse_targets(
            {"status": "success", "data": {"activeTargets": [target("node", "cp1:9100")]}},
            [{"scrapePool": "serviceMonitor/default/node/0", "job": "node", "instance": "cp1:9100"}],
            120, 1000)
        self.assertTrue(result["healthy"])
        self.assertEqual(result["discoveredCount"], 1)
        for url in ("http://10.0.0.1:9090", "https://u:p@example.com", "https://example.com/?secret=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                prometheus.targets_endpoint(url)
        with self.assertRaises(ValueError):
            prometheus.parse_targets({"status": "success", "data": {"activeTargets": []}},
                                     [{"scrapePool": "pool/node", "job": "node", "instance": "cp1:9100"}] * 2,
                                     120, 1000)
        nanoseconds = {"status": "success", "data": {"activeTargets": [
            {"labels": {"job": "node", "instance": "cp1:9100"}, "health": "up",
             "scrapePool": "serviceMonitor/default/node/0",
             "lastScrape": "2026-11-15T03:14:57.122168054Z"}]}}
        # Python 3.9 accepts only microseconds, while Prometheus emits up to
        # nanosecond precision in its RFC3339 target timestamps.
        from datetime import datetime
        timestamp = datetime.fromisoformat("2026-11-15T03:14:57.122168+00:00").timestamp()
        result = prometheus.parse_targets(nanoseconds,
            [{"scrapePool": "serviceMonitor/default/node/0", "job": "node", "instance": "cp1:9100"}],
            120, timestamp)
        self.assertTrue(result["healthy"])

    def test_same_job_and_instance_in_different_scrape_pools_are_distinct_targets(self):
        pool0 = "serviceMonitor/monitoring/kubelet/0"
        pool1 = "serviceMonitor/monitoring/kubelet/1"
        expected = [{"scrapePool": pool0, "job": "kubelet", "instance": "10.0.0.1:10250"},
                    {"scrapePool": pool1, "job": "kubelet", "instance": "10.0.0.1:10250"}]
        result = prometheus.parse_targets({"status": "success", "data": {"activeTargets": [
            target("kubelet", "10.0.0.1:10250", scrape_pool=pool0),
            target("kubelet", "10.0.0.1:10250", scrape_pool=pool1)]}}, expected, 120, 1000)
        self.assertTrue(result["healthy"])
        self.assertEqual(result["expectedCount"], 2)

    def test_scrape_source_labels_are_allowlisted_and_require_a_single_observation(self):
        raw = target("blackbox", "probe:9115")
        raw["labels"].update({"metrics_path": "/probe", "metrics_endpoint": "blackbox-probe",
                              "probe_target": "api_health", "target": "https://private.example",
                              "other": "private value"})
        raw["discoveredLabels"] = {"metrics_path": "/metrics"}
        expected = [{"scrapePool": raw["scrapePool"], "job": "blackbox", "instance": "probe:9115"}]

        def read(*targets):
            return prometheus.parse_targets({"status": "success", "data": {"activeTargets": list(targets)}},
                                            expected, 120, 1000)

        inventory = read(raw)
        self.assertEqual(inventory["expected"][0]["sourceLabels"],
                         {"metrics_path": "/probe", "metrics_endpoint": "blackbox-probe",
                          "probe_target": "api_health"})
        self.assertNotIn("private", str(inventory))
        self.assertNotIn("sourceLabels", read(raw, raw)["expected"][0])
        raw["labels"].update({"metrics_path": "/probe?key=value", "probe_target": "https://private.example"})
        self.assertEqual(read(raw)["expected"][0]["sourceLabels"], {"metrics_endpoint": "blackbox-probe"})

    def test_actual_targets_http_query_does_not_expose_target_urls_or_errors(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.requested.append(self.path)
                payload = {"status": "success", "data": {"activeTargets": [target("node", "cp1:9100")]}}
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.requested = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = prometheus.collect_targets("http://127.0.0.1:" + str(server.server_port),
                [{"scrapePool": "serviceMonitor/default/node/0", "job": "node", "instance": "cp1:9100"}],
                120, now=1000)
            self.assertTrue(result["healthy"])
            self.assertEqual(server.requested, ["/api/v1/targets?state=active"])
            self.assertNotIn("lastError", result)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
