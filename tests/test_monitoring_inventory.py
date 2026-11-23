# SPDX-License-Identifier: MIT
import json
import io
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations import cli
from scripts.operations.models import OperationError, fingerprint
from scripts.operations.monitoring.inventory import collect
from scripts.operations.monitoring import relay_publication


class Client:
    def __init__(self, namespace=True, deny_nodes=False, deny_workloads=False):
        self.namespace = namespace
        self.deny_nodes = deny_nodes
        self.deny_workloads = deny_workloads

    def cluster_identity(self):
        return "cluster-uid"

    def get(self, kind, name=None, namespace=None, optional=False):
        if kind == "node":
            if self.deny_nodes:
                raise OperationError("ops.change.kubernetes")
            return {"items": [
                {"metadata": {"name": "worker"}, "status": {"conditions": [{"type": "Ready", "status": "False"}]}},
                {"metadata": {"name": "cp"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            ]}
        if kind == "namespace":
            return {"metadata": {"name": "monitoring"}} if self.namespace else None
        if self.deny_workloads:
            raise OperationError("ops.change.kubernetes")
        return {"items": []}


class MonitoringInventoryTest(unittest.TestCase):
    def test_node_and_namespace_observation_never_proves_coverage(self):
        result = collect(Client())
        self.assertEqual(result["nodes"]["items"], [{"name": "cp", "ready": "True"},
                                                    {"name": "worker", "ready": "False"}])
        self.assertEqual(result["monitoringNamespace"], "present")
        self.assertEqual(result["coverage"], "unverified")

    def test_denied_permissions_and_absent_namespace_are_distinct(self):
        result = collect(Client(deny_nodes=True, deny_workloads=True))
        self.assertEqual(result["nodes"]["state"], "unverified")
        self.assertIsNone(result["workloadCounts"]["deployment"])
        result = collect(Client(namespace=False))
        self.assertEqual(result["monitoringNamespace"], "absent")
        self.assertEqual(result["coverage"], "unverified")

    def test_email_template_inspection_reports_html_shape_without_recipient_data(self):
        class EmailClient(Client):
            def get(self, kind, name=None, namespace=None, optional=False):
                if kind == "alertmanagerconfigs.monitoring.coreos.com":
                    return {"items": [{
                        "metadata": {"name": "atlas-email", "namespace": "monitoring",
                                     "labels": {"atlas-kubekit.io/managed-by": "monitoring"}},
                        "spec": {"receivers": [{"name": "atlas-email", "emailConfigs": [{
                            "to": "private@example.test",
                            "html": '<html><table style="background-color:#f3f4f6">'
                                    '<a style="background-color:#348bd4">open</a></table></html>',
                            "authPassword": {"name": "smtp", "key": "password"},
                        }]}]},
                    }]}
                return super().get(kind, name, namespace, optional)

        result = collect(EmailClient())["emailTemplate"]
        self.assertEqual(result, {"state": "present", "html": "styled", "text": "disabled", "routes": 1})
        self.assertNotIn("private@example.test", json.dumps(result))
        self.assertNotIn("password", json.dumps(result))

    def test_email_template_inspection_recognizes_owned_html_relay(self):
        release = "kube-prometheus-stack"
        route = {"receiver": "atlas-email", "kind": "email", "language": "zh_CN",
                 "secretName": "atlas-smtp", "passwordKey": "password", "smtpHost": "smtp.example.test",
                 "smtpPort": 465, "sender": "sender@example.test", "recipient": "recipient@example.test",
                 "username": "sender@example.test"}
        config_map = relay_publication.config_map("monitoring", release, [route])
        config_map["metadata"].update({"uid": "routes-uid", "resourceVersion": "10"})
        config_map["metadata"].setdefault("annotations", {})[relay_publication.SPEC_HASH] = fingerprint(
            config_map["data"])
        code_map = relay_publication.code_config_map("monitoring", release)
        code_map["metadata"].update({"uid": "code-uid", "resourceVersion": "11"})
        alertmanager_config = {
            "metadata": {"name": "atlas-robot-atlas-email-0123456789", "namespace": "monitoring",
                         "labels": {"release": release, "atlas-kubekit.io/managed-by": "monitoring"}},
            "spec": {"route": {"receiver": "atlas-email"},
                     "receivers": [{"name": "atlas-email", "webhookConfigs": [{
                         "url": "http://127.0.0.1:" + str(relay_publication.PORT) + "/v1/alert"}]}]},
        }

        class RelayEmailClient(Client):
            def get(self, kind, name=None, namespace=None, optional=False):
                if kind == "alertmanagerconfigs.monitoring.coreos.com":
                    return {"items": [alertmanager_config]}
                if kind == "configmap" and name == relay_publication.CONFIG_NAME:
                    return config_map
                if kind == "configmap" and name == code_map["metadata"]["name"]:
                    return code_map
                return super().get(kind, name, namespace, optional)

        result = collect(RelayEmailClient())["emailTemplate"]
        self.assertEqual(result, {"state": "present", "html": "styled", "text": "disabled", "routes": 1})
        self.assertNotIn("recipient@example.test", json.dumps(result))
        self.assertNotIn("password", json.dumps(result))

    def test_email_template_inspection_distinguishes_missing_unstyled_and_unreadable(self):
        class EmailClient(Client):
            def __init__(self, items=None, denied=False):
                super().__init__()
                self.items, self.denied = items, denied

            def get(self, kind, name=None, namespace=None, optional=False):
                if kind == "alertmanagerconfigs.monitoring.coreos.com":
                    if self.denied:
                        raise OperationError("ops.change.kubernetes")
                    return {"items": self.items or []}
                if kind == "configmap":
                    return None
                return super().get(kind, name, namespace, optional)

        missing = collect(EmailClient())["emailTemplate"]
        self.assertEqual(missing["state"], "missing")
        self.assertEqual(collect(EmailClient(denied=True))["emailTemplate"]["state"], "unverified")
        unowned = {"metadata": {"name": "atlas-email", "labels": {}}, "spec": {}}
        self.assertEqual(collect(EmailClient([unowned]))["emailTemplate"]["state"], "missing")
        unstyled = {"metadata": {"name": "atlas-email", "labels": {
            "atlas-kubekit.io/managed-by": "monitoring"}}, "spec": {
            "receivers": [{"name": "atlas-email", "emailConfigs": [{"text": "body", "html": "<p>plain</p>"}]}]}}
        result = collect(EmailClient([unstyled]))["emailTemplate"]
        self.assertEqual(result, {"state": "incomplete", "html": "missing", "text": "enabled", "routes": 1})

        styled_legacy = {"metadata": {"name": "atlas-email", "labels": {
            "atlas-kubekit.io/managed-by": "monitoring"}}, "spec": {
            "receivers": [{"name": "atlas-email", "emailConfigs": [{
                "text": "old plain-text body",
                "html": '<html><table style="background-color:#f3f4f6">'
                        '<a style="background-color:#348bd4">open</a></table></html>'}]}]}}
        result = collect(EmailClient([styled_legacy]))["emailTemplate"]
        self.assertEqual(result, {"state": "incomplete", "html": "styled", "text": "enabled", "routes": 1})

    def test_cli_creates_private_report_and_returns_unverified(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.monitoring.Kubernetes", return_value=Client()):
                result = cli.main(["--lang", "zh_CN", "--monitoring-inspect", "--output-dir", temp])
            self.assertEqual(result, 2)
            reports = list(Path(temp).rglob("*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text())
            self.assertEqual(report["execution"]["result"]["coverage"], "unverified")

    def test_cli_queries_expected_nodes_and_never_records_url(self):
        sample = {"state": "observed", "nodes": [{"node": "cp", "state": "ready"},
                                                  {"node": "worker", "state": "not-ready"}]}
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            with patch("scripts.operations.commands.monitoring.Kubernetes", return_value=Client()), \
                 patch("scripts.operations.commands.monitoring.prometheus.collect", return_value=sample) as query, \
                 redirect_stdout(io.StringIO()) as output:
                result = cli.main(["--lang", "en", "--monitoring-inspect", "--output-dir", temp,
                                   "--prometheus-url", "http://127.0.0.1:19090", "--monitoring-max-age", "60"])
            self.assertEqual(result, 2)
            query.assert_called_once_with("http://127.0.0.1:19090", ["cp", "worker"], 60)
            data = next(Path(temp).rglob("*.json")).read_text()
            self.assertNotIn("19090", data)
            self.assertNotIn("19090", output.getvalue())
            self.assertEqual(json.loads(data)["execution"]["result"]["nodeMetrics"], sample)

    def test_cli_executes_only_read_operations_via_kubectl(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as temp:
            kubectl = Path(temp) / "kubectl"
            kubectl.write_text('''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if "get" not in args:
    sys.exit(3)
args = args[args.index("get") + 1:]
if args[:2] == ["namespace", "kube-system"]:
    value = {"metadata": {"uid": "cluster-uid"}}
elif args[:2] == ["namespace", "monitoring"]:
    value = {"metadata": {"name": "monitoring"}}
elif args[0] == "node":
    value = {"items": [{"metadata": {"name": "cp"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}
elif args[0] in ("deployment", "statefulset", "daemonset"):
    value = {"items": []}
else:
    sys.exit(4)
print(json.dumps(value))
''')
            kubectl.chmod(0o700)
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    is_timestamp = "timestamp%28" in self.path
                    body = json.dumps({"status": "success", "data": {"resultType": "vector", "result": [
                        {"metric": {"node": "cp"}, "value": [time.time(), str(time.time()) if is_timestamp else "1"]}]}}).encode()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass

            server = HTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            output = Path(temp) / "reports"
            try:
                with patch.dict(os.environ, {"PATH": temp + os.pathsep + os.environ.get("PATH", "")}), \
                     redirect_stdout(io.StringIO()):
                    result = cli.main(["--lang", "en", "--monitoring-inspect", "--output-dir", str(output),
                                       "--prometheus-url", "http://127.0.0.1:" + str(server.server_port)])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            self.assertEqual(result, 2)
            report = json.loads(next(output.rglob("*.json")).read_text())
            self.assertEqual(report["execution"]["result"]["nodes"]["items"][0]["name"], "cp")
            self.assertEqual(report["execution"]["result"]["nodeMetrics"]["state"], "observed")
            self.assertEqual(report["execution"]["result"]["coverage"], "unverified")


if __name__ == "__main__":
    unittest.main()
