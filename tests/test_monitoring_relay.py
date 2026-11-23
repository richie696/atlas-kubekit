# SPDX-License-Identifier: MIT
import http.client
import json
import threading
import unittest
from unittest.mock import patch
from scripts.operations.monitoring.channels import dispatch, relay
from scripts.operations.monitoring import loki, relay_publication
from tests.test_monitoring_alertmanager import payload


class RelayHTTPTest(unittest.TestCase):
    def setUp(self):
        self.outcome = "accepted"
        self.sent = []
        def deliver(body):
            return dispatch.dispatch(body, "team-a", "wecom", "unused")
        # Keep the real dispatcher and replace only the external network call.
        self.patch = patch.object(dispatch.wecom, "deliver", side_effect=self.send)
        self.patch.start()
        self.token = "a" * 32
        self.server = relay.make_server("127.0.0.1", 0, self.token, deliver)
        self.metrics_server = relay.make_metrics_server("0.0.0.0", 0, self.server)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.metrics_thread = threading.Thread(target=self.metrics_server.serve_forever, daemon=True)
        self.metrics_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.metrics_server.shutdown()
        self.metrics_thread.join()
        self.metrics_server.server_close()
        self.patch.stop()

    def test_route_config_selects_channel_by_alertmanager_receiver(self):
        config = {"version": 1, "routes": [
            {"receiver": "team-a", "kind": "wecom", "language": "zh_CN", "endpoint_env": "ATLAS_RELAY_WECOM"},
            {"receiver": "team-b", "kind": "dingtalk", "language": "zh_CN", "endpoint_env": "ATLAS_RELAY_DINGTALK",
             "secret_env": "ATLAS_RELAY_DINGTALK_SECRET"}]}
        environment = {"ATLAS_RELAY_WECOM": "https://wecom.example.test/hook",
                       "ATLAS_RELAY_DINGTALK": "https://dingtalk.example.test/hook",
                       "ATLAS_RELAY_DINGTALK_SECRET": "signing-key"}
        deliver = relay.route_dispatch(config, environment, now=lambda: 12)
        wecom_body = json.dumps(payload()).encode()
        prefixed_wecom = payload()
        prefixed_wecom["receiver"] = "monitoring/atlas-robot-team-a-1234567890/team-a"
        prefixed_wecom_body = json.dumps(prefixed_wecom).encode()
        dingtalk = payload()
        dingtalk["receiver"] = "team-b"
        dingtalk_body = json.dumps(dingtalk).encode()
        with patch.object(dispatch.wecom, "deliver", return_value="accepted") as wecom_send, \
             patch.object(dispatch.dingtalk, "deliver", return_value="accepted") as dingtalk_send:
            self.assertEqual(deliver(wecom_body), "accepted")
            self.assertEqual(deliver(prefixed_wecom_body), "accepted")
            self.assertEqual(deliver(dingtalk_body), "accepted")
        self.assertEqual(wecom_send.call_count, 2)
        self.assertEqual(wecom_send.call_args.args[0], "https://wecom.example.test/hook")
        self.assertEqual(dingtalk_send.call_args.args[0], "https://dingtalk.example.test/hook")
        self.assertEqual(dingtalk_send.call_args.args[2:4], (12000, "signing-key"))

    def test_route_config_rejects_ambiguous_or_missing_secret_references(self):
        route = {"receiver": "team-a", "kind": "wecom", "language": "zh_CN",
                 "endpoint_env": "ATLAS_RELAY_ENDPOINT"}
        config = {"version": 1, "routes": [route, dict(route)]}
        with self.assertRaises(ValueError):
            relay.route_dispatch(config, {"ATLAS_RELAY_ENDPOINT": "https://example.test/hook"})
        config["routes"] = [route]
        with self.assertRaises(ValueError):
            relay.route_dispatch(config, {})
        config["routes"] = [{**route, "endpoint_env": "ENDPOINT"}]
        with self.assertRaises(ValueError):
            relay.route_dispatch(config, {"ENDPOINT": "https://example.test/hook"})

    def test_diagnostics_enrich_robot_message_and_query_failure_does_not_drop_alert(self):
        config = {"version": 1, "routes": [{"receiver": "team-a", "kind": "wecom", "language": "zh_CN",
                    "endpoint_env": "ATLAS_RELAY_WECOM"}],
                  "diagnostics": {"loki_endpoint": "http://loki-gateway.logging.svc.cluster.local",
                                  "before_seconds": 300, "after_seconds": 60,
                                  "max_lines": 10, "max_message_bytes": 2048}}
        deliver = relay.route_dispatch(config, {"ATLAS_RELAY_WECOM": "https://wecom.example.test/hook"})
        body = payload()
        body["alerts"][0]["labels"].update(namespace="monitoring", container="api")
        with patch.object(dispatch.loki, "query", return_value=loki.QueryResult("ok", ("panic: disk unavailable",))), \
             patch.object(dispatch.wecom, "deliver", side_effect=self.send):
            self.assertEqual(deliver(json.dumps(body).encode()), "accepted")
        self.assertIn("panic: disk unavailable", self.sent[-1])
        self.assertNotIn("/explore?", self.sent[-1])

        linked_config = {**config, "diagnostics": {**config["diagnostics"],
                         "grafana_explore_url": "https://grafana.example.com",
                         "loki_datasource_uid": "loki-main"}}
        linked_deliver = relay.route_dispatch(
            linked_config, {"ATLAS_RELAY_WECOM": "https://wecom.example.test/hook"})
        with patch.object(dispatch.loki, "query", return_value=loki.QueryResult("ok", ("panic: disk unavailable",))), \
             patch.object(dispatch.wecom, "deliver", side_effect=self.send):
            self.assertEqual(linked_deliver(json.dumps(body).encode()), "accepted")
        self.assertNotIn("panic: disk unavailable", self.sent[-1])
        self.assertIn("https://grafana.example.com/explore?", self.sent[-1])

        self.sent.clear()
        failed_body = payload()
        failed_body["alerts"][0]["labels"].update(namespace="monitoring", container="api")
        with patch.object(dispatch.loki, "query", return_value=loki.QueryResult("query_failed")), \
             patch.object(dispatch.wecom, "deliver", side_effect=self.send):
            self.assertEqual(deliver(json.dumps(failed_body).encode()), "accepted")
        self.assertIn("日志查询失败", self.sent[-1])
        self.assertIn("NodeDown", self.sent[-1])

    def test_diagnostics_can_dispatch_to_secret_backed_email_route(self):
        email_route = {"receiver": "ops-email", "kind": "email", "language": "zh_CN",
                       "password_env": "ATLAS_RELAY_SMTP_PASSWORD",
                       "smtp": {"host": "smtp.example.com", "port": 465, "sender": "alerts@example.com",
                                "recipient": "ops@example.com", "username": "alerts@example.com",
                                "language": "zh_CN"}}
        body = payload()
        body["receiver"] = "ops-email"
        body["alerts"][0]["labels"].update(namespace="monitoring", container="api")
        config = {"version": 1, "routes": [email_route],
                  "diagnostics": {"loki_endpoint": "http://loki.logging.svc", "before_seconds": 300,
                                  "after_seconds": 60, "max_lines": 10, "max_message_bytes": 2048}}
        deliver = relay.route_dispatch(config, {"ATLAS_RELAY_SMTP_PASSWORD": "test-password"})
        with patch.object(dispatch.loki, "query", return_value=loki.QueryResult("ok", ("panic: sample",))), \
             patch.object(dispatch.smtp, "deliver", return_value="accepted") as smtp_send:
            self.assertEqual(deliver(json.dumps(body).encode()), "accepted")
        notification, message, settings, password = smtp_send.call_args.args
        self.assertEqual(notification.status, "firing")
        self.assertIn("panic: sample", message)
        self.assertEqual(settings["recipient"], "ops@example.com")
        self.assertEqual(password, "test-password")
        self.assertNotIn("test-password", json.dumps(config))

    def test_email_route_rejects_untrusted_or_missing_password_reference(self):
        config = {"version": 1, "routes": [{"receiver": "ops-email", "kind": "email", "language": "zh_CN",
                  "password_env": "ATLAS_RELAY_SMTP_PASSWORD",
                  "smtp": {"host": "smtp.example.com", "port": 465, "sender": "alerts@example.com",
                           "recipient": "ops@example.com", "username": "alerts@example.com",
                           "language": "zh_CN"}}]}
        with self.assertRaises(ValueError):
            relay.route_dispatch(config, {})
        config["routes"][0]["smtp"]["host"] = "smtp.example.com/path"
        with self.assertRaises(ValueError):
            relay.route_dispatch(config, {"ATLAS_RELAY_SMTP_PASSWORD": "not-empty"})

    def test_published_email_route_metadata_is_accepted_by_runtime_dispatch(self):
        route = {"receiver": "ops-email", "kind": "email", "language": "zh_CN",
                 "secretName": "smtp-secret", "passwordKey": "password", "smtpHost": "smtp.example.com",
                 "smtpPort": 465, "sender": "alerts@example.com", "recipient": "ops@example.com",
                 "username": "alerts@example.com"}
        config = relay_publication.route_config([route], {"loki_endpoint": None, "before_seconds": 300,
            "after_seconds": 60, "max_lines": 20, "max_message_bytes": 8192})
        password_env = config["routes"][0]["password_env"]
        deliver = relay.route_dispatch(config, {password_env: "secret-password"})
        body = payload()
        body["receiver"] = "ops-email"
        with patch.object(dispatch.loki, "query", return_value=loki.QueryResult("source_missing")), \
             patch.object(dispatch.smtp, "deliver", return_value="accepted") as smtp_send:
            self.assertEqual(deliver(json.dumps(body).encode()), "accepted")
        notification, message, settings, password = smtp_send.call_args.args
        self.assertEqual(notification.receiver, "ops-email")
        self.assertIn("日志采集源未配置", message)
        self.assertEqual(settings["host"], "smtp.example.com")
        self.assertEqual(password, "secret-password")

    def send(self, endpoint, text, opener):
        self.sent.append(text)
        return self.outcome

    def post(self, token=None, body=None, path="/v1/alert"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Authorization": "Bearer " + token} if token else {}
        connection.request("POST", path, body if body is not None else json.dumps(payload()), headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_authentication_and_receiver_isolation(self):
        self.assertEqual(self.post("b" * 32)[0], 401)
        self.assertEqual(self.post(self.token, path="/other")[0], 400)
        other = payload()
        other["receiver"] = "team-b"
        self.assertEqual(self.post(self.token, json.dumps(other))[0], 400)
        self.assertFalse(self.sent)
        self.assertEqual(self.post(self.token), (200, b""))
        self.assertEqual(len(self.sent), 1)

    def test_uncertain_or_rejected_delivery_is_not_success(self):
        self.outcome = "unknown"
        self.assertEqual(self.post(self.token)[0], 503)
        self.outcome = "rejected"
        self.assertEqual(self.post(self.token)[0], 503)
        # Exercise the early Content-Length guard without racing the server's
        # 413 response against a large client body write on loopback TCP.
        oversized = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        oversized.putrequest("POST", "/v1/alert")
        oversized.putheader("Authorization", "Bearer " + self.token)
        oversized.putheader("Content-Length", "262145")
        oversized.endheaders()
        self.assertEqual(oversized.getresponse().status, 413)
        oversized.close()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", "/metrics")
        self.assertEqual(connection.getresponse().status, 401)
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", "/metrics", headers={"Authorization": "Bearer " + self.token})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        metrics = response.read().decode()
        self.assertIn('outcome="unknown"} 1', metrics)
        self.assertIn('outcome="rejected"} 1', metrics)
        self.assertNotIn(self.token, metrics)
        connection.close()

    def test_health_probe_is_available_without_exposing_delivery_metrics(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", "/-/healthy")
        response = connection.getresponse()
        self.assertEqual((response.status, response.read()), (200, b""))
        connection.close()

    def test_metrics_listener_is_authenticated_and_does_not_accept_alerts(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.metrics_server.server_port, timeout=3)
        connection.request("GET", "/metrics")
        self.assertEqual(connection.getresponse().status, 401)
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", self.metrics_server.server_port, timeout=3)
        connection.request("POST", "/v1/alert", "{}", {"Authorization": "Bearer " + self.token})
        self.assertEqual(connection.getresponse().status, 404)
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", self.metrics_server.server_port, timeout=3)
        connection.request("GET", "/metrics", headers={"Authorization": "Bearer " + self.token})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("atlas_relay_deliveries_total", response.read().decode())
        connection.close()
        self.assertFalse(self.sent)

    def test_malformed_data_and_non_loopback_are_rejected(self):
        self.assertEqual(self.post(self.token, "{}"), (400, b""))
        with self.assertRaises(ValueError):
            relay.make_server("0.0.0.0", 8080, self.token, lambda body: "accepted")


if __name__ == "__main__":
    unittest.main()
