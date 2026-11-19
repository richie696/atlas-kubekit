# SPDX-License-Identifier: MIT
import unittest
import json
from urllib.parse import parse_qs, urlsplit

from scripts.operations.monitoring import log_diagnostics, loki
from scripts.operations.monitoring.channels import alertmanager


class LogDiagnosticsTest(unittest.TestCase):
    def test_selector_maps_alert_labels_to_the_actual_alloy_instance_label(self):
        value = log_diagnostics.selector({
            "namespace": "payments", "pod": "api-0", "container": "server",
            "user_id": "must-not-enter-query", "query": "{evil=\"x\"}",
        })
        self.assertEqual(value, '{instance="payments/api-0:server"}')

    def test_selector_rejects_alert_without_a_pod_and_namespace_identity(self):
        self.assertIsNone(log_diagnostics.selector({"namespace": "payments", "severity": "critical"}))
        self.assertEqual(log_diagnostics.selector({"node": "worker-1"}), '{node="worker-1"}')
        self.assertIsNone(log_diagnostics.selector({"namespace": "payments", "deployment": "api"}))

    def test_selector_prefers_exported_namespace_and_matches_all_containers_for_a_pod(self):
        value = log_diagnostics.selector({"exported_namespace": "kube-system", "pod": "api-0"})
        self.assertEqual(value, '{instance=~"^kube\\\\-system/api\\\\-0:.+$"}')

    def test_selector_escapes_untrusted_label_values(self):
        value = log_diagnostics.selector({"namespace": "payments", "pod": 'api"\\-0', "container": "server"})
        self.assertEqual(value, '{instance="payments/api\\\"\\\\-0:server"}')
        self.assertIsNone(log_diagnostics.selector({"namespace": "payments", "pod": "api\n-0"}))

    def test_selector_scopes_remote_logs_by_cluster_identity(self):
        self.assertEqual(
            log_diagnostics.selector({"cluster": "prod-east", "namespace": "payments",
                                      "pod": "api-0", "container": "server"}),
            '{cluster="prod-east",instance="payments/api-0:server"}',
        )
        self.assertIsNone(log_diagnostics.selector({"cluster": "prod\n-east", "node": "worker-1"}))

    def test_render_redacts_secrets_and_bounds_lines_and_bytes(self):
        text, truncated = log_diagnostics.render_lines([
            "panic: database unavailable",
            "Authorization: Bearer abc.def.secret",
            "password=private-value",
            "line-5",
        ], "zh_CN", max_lines=3, max_bytes=1024)
        self.assertIn("Bearer [已脱敏]", text)
        self.assertIn("password=[已脱敏]", text)
        self.assertNotIn("abc.def.secret", text)
        self.assertNotIn("private-value", text)
        self.assertLessEqual(len(text.encode("utf-8")), 1024)
        self.assertTrue(truncated)
        self.assertIn("panic: database unavailable", log_diagnostics.sanitize_line("panic: database unavailable"))

    def test_render_has_localized_empty_and_failure_states(self):
        empty, _ = log_diagnostics.render_lines([], "zh_CN")
        failed = log_diagnostics.status_text("query_failed", "zh_CN")
        self.assertIn("未找到", empty)
        self.assertIn("查询失败", failed)

    def test_query_url_is_limited_to_in_cluster_loki_and_bounds_results(self):
        url = log_diagnostics.query_url("http://loki-gateway.logging.svc.cluster.local", '{pod="api-0"}',
                                        100, 200, 12)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["limit"], ["12"])
        self.assertEqual(query["direction"], ["backward"])
        for endpoint in ("https://example.com", "http://127.0.0.1", "http://user@loki.logging.svc"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                log_diagnostics.query_url(endpoint, '{pod="api-0"}', 100, 200)

    def test_grafana_link_is_absent_by_default_and_uses_supported_explore_panes_when_enabled(self):
        settings = {"loki_endpoint": "http://loki.logging.svc", "before_seconds": 300,
                    "after_seconds": 60, "max_lines": 20, "max_message_bytes": 8192}
        alert = {"status": "firing", "startsAt": "2026-11-17T00:00:00Z",
                 "labels": {"namespace": "payments", "pod": "api-0"}}
        self.assertIsNone(log_diagnostics.explore_url(settings, alert))
        settings.update({"grafana_explore_url": "https://grafana.example.com", "loki_datasource_uid": "loki-main"})
        result = urlsplit(log_diagnostics.explore_url(settings, alert))
        self.assertEqual(result.scheme, "https")
        self.assertEqual(result.netloc, "grafana.example.com")
        params = parse_qs(result.query)
        panes = json.loads(params["panes"][0])
        self.assertEqual(panes["1"]["queries"][0]["datasource"], {"type": "loki", "uid": "loki-main"})
        self.assertEqual(panes["1"]["queries"][0]["expr"], '{instance=~"^payments/api\\\\-0:.+$"}')
        for url in ("http://grafana.example.com", "https://127.0.0.1", "https://grafana.local"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                log_diagnostics.validate_settings({**settings, "grafana_explore_url": url})

    def test_loki_query_returns_bounded_lines_and_hides_query_failures(self):
        class Response:
            def __init__(self, status, body):
                self.status, self.body = status, body
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def getcode(self): return self.status
            def read(self, size): return self.body[:size]

        class Transport:
            def __init__(self, status=200): self.status = status
            def open(self, request, timeout):
                self.request, self.timeout = request, timeout
                body = json.dumps({"status": "success", "data": {"resultType": "streams", "result": [
                    {"stream": {}, "values": [["20", "new"], ["10", "old"]]}]}}).encode()
                return Response(self.status, body)

        alert = {"status": "firing", "startsAt": "2026-11-17T00:00:00Z",
                 "labels": {"namespace": "payments", "pod": "api-0"}}
        transport = Transport()
        result = loki.query("http://loki-gateway.logging.svc.cluster.local", alert, opener=transport)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.lines, ("new", "old"))
        self.assertEqual(transport.timeout, loki.TIMEOUT_SECONDS)
        self.assertEqual(loki.query("http://loki-gateway.logging.svc.cluster.local", alert,
                                    opener=Transport(503)).status, "query_failed")
        self.assertEqual(loki.query(None, alert).status, "source_missing")

    def test_grouped_alerts_never_trigger_more_than_five_loki_queries(self):
        alerts = tuple({"name": "AtlasNodeReadinessOrTelemetryMissing", "status": "firing",
                        "labels": {"namespace": "monitoring", "pod": "node-" + str(index),
                                   "container": "exporter"},
                        "startsAt": "2026-11-17T00:00:00Z", "endsAt": ""}
                       for index in range(12))
        notification = alertmanager.Notification("route", "alert", "firing", alerts)
        settings = {"loki_endpoint": "http://loki.logging.svc", "before_seconds": 60,
                    "after_seconds": 30, "max_lines": 10, "max_message_bytes": 4096}
        calls = []

        def query(*args):
            calls.append(args)
            return loki.QueryResult("ok", ("one log",))

        output = log_diagnostics.enrich_text(notification, settings, query)
        self.assertEqual(len(calls), log_diagnostics.MAX_ALERT_QUERIES)
        self.assertIn(log_diagnostics.status_text("truncated"), output)

    def test_complete_notification_is_bounded_even_if_alert_text_is_oversized(self):
        settings = {"loki_endpoint": None, "before_seconds": 60, "after_seconds": 30,
                    "max_lines": 10, "max_message_bytes": 256}
        notification = alertmanager.Notification("route", "告警" * 500, "firing", ())
        output = log_diagnostics.enrich_text(notification, settings, lambda *args: None)
        self.assertLessEqual(len(output.encode("utf-8")), 256)
        self.assertIn(log_diagnostics.status_text("truncated"), output)


if __name__ == "__main__":
    unittest.main()
