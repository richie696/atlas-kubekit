import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import Mock, patch

from scripts.addons import monitoring_notification_setup as setup


class MonitoringNotificationSetupTests(unittest.TestCase):
    def test_agent_topology_rejects_notification_setup_before_collecting_secrets(self):
        output = StringIO()
        with patch.dict(setup.os.environ, {"KUBECONFIG": "/tmp/kubeconfig"}), \
                patch.object(setup, "verify_cluster_configuration"), \
                patch.object(setup, "monitoring_topology_mode", return_value="agent"), \
                patch.object(setup, "ask") as ask, \
                patch.object(setup, "create_secret") as create_secret, \
                redirect_stderr(output):
            result = setup.main(["--lang", "en", "--require-complete"])

        self.assertEqual(result, 1)
        self.assertIn("only be published to the independent monitoring center", output.getvalue())
        ask.assert_not_called()
        create_secret.assert_not_called()

    def test_successful_setup_cleans_old_secrets_only_after_plan_apply_and_protects_new_ones(self):
        route_args = {"namespace": "monitoring", "release": "stack", "alertmanager": "alertmanager",
                      "alertmanager_version": "0.28.1", "receiver": "atlas-email"}
        state_seen = {}
        old_secret = {"name": "old-route-secret"}
        client = Mock()
        client.get.return_value = {"spec": {}}

        def apply_plan(_lang, _operation, _args, _path, _names, state):
            state["apply_started"] = True
            state["uncertain"] = set()
            state_seen.update(state)

        def cleanup(_client, namespace, candidates):
            self.assertEqual(namespace, "monitoring")
            self.assertEqual(candidates, (old_secret,))
            self.assertTrue(state_seen["apply_started"])
            return {"removed": ["old-route-secret"], "retained": []}

        with patch.dict(setup.os.environ, {"KUBECONFIG": "/tmp/kubeconfig"}), \
                patch.object(setup.subprocess, "run", return_value=type("Result", (), {"returncode": 0})()), \
                patch.object(setup, "verify_cluster_configuration"), \
                patch.object(setup, "alertmanager_identity", return_value=("alertmanager", "0.28.1", "stack")), \
                patch.object(setup, "ask", side_effect=("", "", "yes", "no")), \
                patch.object(setup, "unique_name", return_value="relay-auth-secret"), \
                patch.object(setup, "configure_route", return_value=(
                    "monitoring-email-diagnostics", route_args, "route-secret", "password", {"password": "fake"})), \
                patch.object(setup.relay_publication, "runtime_image", return_value="registry.test/relay@sha256:" + "a" * 64), \
                patch.object(setup.relay_publication, "container"), \
                patch.object(setup, "create_secret") as create_secret, \
                patch.object(setup, "plan_and_apply", side_effect=apply_plan) as plan_apply, \
                patch.object(setup, "Kubernetes", return_value=client), \
                patch.object(setup.relay_publication, "existing_credentials",
                             return_value=(("relay-auth-secret", "shared-key"), {"old-route-secret"})), \
                patch.object(setup.notification_secrets, "replacement_candidates",
                             return_value=(old_secret,)) as candidates, \
                patch.object(setup.notification_secrets, "cleanup_unreferenced", side_effect=cleanup) as cleanup_call:
            self.assertEqual(setup.main(["--lang", "en", "--require-complete"]), 0)

        create_secret.assert_called_once_with("monitoring", "route-secret", {"password": "fake"},
                                              "route", "atlas-email")
        candidates.assert_called_once_with(client, "monitoring", "atlas-email", {"old-route-secret"})
        plan_apply.assert_called_once()
        self.assertEqual(plan_apply.call_args.args[2]["relay_key"], "shared-key")
        cleanup_call.assert_called_once()
        self.assertTrue(state_seen["apply_started"])

    def test_failed_plan_apply_reports_error_without_cleaning_old_or_uncertain_secrets(self):
        route_args = {"namespace": "monitoring", "release": "stack", "alertmanager": "alertmanager",
                      "alertmanager_version": "0.28.1", "receiver": "atlas-email"}
        output = StringIO()

        def fail_apply(_lang, _operation, _args, _path, secret_names, state):
            state["apply_started"] = True
            state["uncertain"] = set(secret_names)
            raise RuntimeError("apply failed")

        delete_secret = patch.object(setup, "delete_secret")
        with patch.dict(setup.os.environ, {"KUBECONFIG": "/tmp/kubeconfig"}), \
                patch.object(setup.subprocess, "run", return_value=type("Result", (), {"returncode": 0})()), \
                patch.object(setup, "verify_cluster_configuration"), \
                patch.object(setup, "alertmanager_identity", return_value=("alertmanager", "0.28.1", "stack")), \
                patch.object(setup, "ask", side_effect=("", "", "yes")), \
                patch.object(setup, "unique_name", side_effect=("relay-secret", "route-secret")), \
                patch.object(setup, "configure_route", return_value=(
                    "monitoring-email-diagnostics", route_args, "route-secret", "password", {"password": "fake"})), \
                patch.object(setup.relay_publication, "runtime_image", return_value="registry.test/relay@sha256:" + "a" * 64), \
                patch.object(setup.relay_publication, "container"), \
                patch.object(setup, "create_secret"), \
                patch.object(setup, "plan_and_apply", side_effect=fail_apply), \
                patch.object(setup, "Kubernetes") as client_type, \
                patch.object(setup.relay_publication, "existing_credentials", return_value=(None, frozenset())), \
                patch.object(setup.notification_secrets, "cleanup_unreferenced") as cleanup, \
                delete_secret as delete_call, redirect_stderr(output):
            result = setup.main(["--lang", "en", "--require-complete"])

        self.assertEqual(result, 1)
        self.assertIn("Notification setup failed", output.getvalue())
        self.assertIn("keep these Secrets", output.getvalue())
        cleanup.assert_not_called()
        client_type.return_value.get.assert_called_once()
        delete_call.assert_not_called()

    def test_full_setup_reports_cancelled_route_as_incomplete(self):
        with patch.dict(setup.os.environ, {"KUBECONFIG": ""}), \
                patch.object(setup.subprocess, "run", return_value=type("Result", (), {"returncode": 0})()), \
                patch.object(setup, "verify_cluster_configuration"), \
                patch.object(setup, "alertmanager_identity", return_value=("alertmanager", "0.28.1", "kube-prometheus-stack")), \
                patch.object(setup, "ask", side_effect=("registry.test/relay@sha256:" + "a" * 64, "")), \
                patch.object(setup.relay_publication, "container"), \
                patch.object(setup, "configure_route", side_effect=InterruptedError("cancelled")):
            self.assertEqual(setup.main(["--lang", "en", "--require-complete"]), 4)

    def test_missing_cluster_configuration_fails_before_notification_prompts(self):
        output = StringIO()
        expected = setup.tr("en", "phase03.monitoring.setup_requires_cluster_configuration")
        with patch.object(setup, "verify_cluster_configuration",
                          side_effect=ValueError(expected)), \
                patch.object(setup, "ask") as ask, \
                redirect_stderr(output):
            result = setup.main(["--lang", "en", "--require-complete"])

        self.assertEqual(result, 1)
        self.assertIn("complete stage 02 first", output.getvalue())
        ask.assert_not_called()

    def test_missing_configuration_is_reported_before_secret_collection(self):
        with patch.object(setup, "Kubernetes") as client_type, \
                patch.object(setup.configuration_cluster, "read", return_value=None), \
                patch.object(setup, "Environment") as environment_type:
            with self.assertRaisesRegex(ValueError, "complete stage 02 first"):
                setup.verify_cluster_configuration("en")

        client_type.assert_called_once()
        environment_type.assert_not_called()

    def test_secret_is_sent_on_stdin_and_not_in_kubectl_arguments(self):
        with patch.object(setup, "run") as command:
            setup.create_secret("monitoring", "atlas-test-secret", {"password": "secret-value"},
                                "route", "atlas-email")

        args = command.call_args.args[0]
        payload = json.loads(command.call_args.kwargs["input_text"])
        self.assertEqual(args, ["kubectl", "create", "-f", "-"])
        self.assertNotIn("secret-value", str(args))
        self.assertEqual(payload["data"]["password"], "c2VjcmV0LXZhbHVl")
        self.assertEqual(payload["metadata"]["labels"]["atlas-kubekit.io/owner"],
                         "monitoring-notification-setup")
        self.assertEqual(payload["metadata"]["labels"]["atlas-kubekit.io/component"], "route")
        self.assertEqual(payload["metadata"]["labels"]["atlas-kubekit.io/receiver"], "atlas-email")
        self.assertNotIn("kubectl.kubernetes.io/last-applied-configuration",
                         payload["metadata"].get("annotations", {}))

    def test_email_diagnostics_plan_requires_full_plan_id_and_uses_secret_references(self):
        args = {
            "namespace": "monitoring", "release": "kube-prometheus-stack",
            "alertmanager": "kube-prometheus-stack-alertmanager", "alertmanager_version": "0.28.1",
            "relay_image": "registry.example.test/relay@sha256:" + "a" * 64,
            "relay_secret": "atlas-relay-auth-12345678", "relay_key": "shared-key",
            "route_secret": "atlas-smtp-12345678",
            "receiver": "atlas-email", "signing_key": "-", "kind": "email", "severity": "warning",
            "notification_language": "zh_CN", "loki_endpoint": "http://loki.logging.svc.cluster.local:3100",
            "before_seconds": 300, "after_seconds": 60, "max_lines": 20, "max_message_bytes": 8192,
            "grafana_url": "", "datasource_uid": "", "grafana_org_id": 1,
            "smtp_host": "smtp.example.test", "smtp_port": 465, "sender": "sender@example.test",
            "recipient": "receiver@example.test", "username": "sender@example.test",
        }
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if "--change-plan" in command:
                Path(command[command.index("--plan-file") + 1]).write_text(
                    json.dumps({"id": "plan-identifier"}), encoding="utf-8")
                return None
            return subprocess.CompletedProcess(command, 2)

        state = {"apply_started": False}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(setup, "run", side_effect=fake_run), \
                patch.object(setup, "ask", return_value="plan-identifier"):
            setup.plan_and_apply("zh_CN", "monitoring-email-diagnostics", args,
                                 Path(directory) / "plan.json", [], state)

        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][commands[0].index("--output-dir") + 1],
                         commands[0][commands[0].index("--plan-file") + 1].rsplit("/", 1)[0])
        self.assertIn("--monitoring-email-receiver", commands[0])
        self.assertEqual(commands[0][commands[0].index("--monitoring-relay-auth-key") + 1], "shared-key")
        self.assertIn("atlas-email", commands[0])
        self.assertIn("--change-apply", commands[1])
        self.assertNotIn("--operation", commands[1])
        self.assertIn("--confirm", commands[1])
        self.assertIn("plan-identifier", commands[1])
        self.assertTrue(state["apply_started"])

    def test_blank_plan_confirmation_cancels_without_applying(self):
        args = {
            "namespace": "monitoring", "release": "kube-prometheus-stack",
            "alertmanager": "kube-prometheus-stack-alertmanager", "alertmanager_version": "0.28.1",
            "relay_image": "registry.example.test/relay@sha256:" + "a" * 64,
            "relay_secret": "atlas-relay-auth-12345678", "relay_key": "token",
            "route_secret": "atlas-smtp-12345678",
            "receiver": "atlas-email", "signing_key": "-", "kind": "email", "severity": "warning",
            "notification_language": "zh_CN", "loki_endpoint": "http://loki.logging.svc.cluster.local:3100",
            "before_seconds": 300, "after_seconds": 60, "max_lines": 20, "max_message_bytes": 8192,
            "grafana_url": "", "datasource_uid": "", "grafana_org_id": 1,
            "smtp_host": "smtp.example.test", "smtp_port": 465, "sender": "sender@example.test",
            "recipient": "receiver@example.test", "username": "sender@example.test",
        }
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            if "--change-plan" in command:
                Path(command[command.index("--plan-file") + 1]).write_text(
                    json.dumps({"id": "plan-identifier"}), encoding="utf-8")
            return None

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(setup, "run", side_effect=fake_run), \
                patch.object(setup, "ask", return_value="") as ask:
            with self.assertRaises(InterruptedError):
                setup.plan_and_apply("zh_CN", "monitoring-email-diagnostics", args,
                                     Path(directory) / "plan.json", [], {"apply_started": False})

        self.assertEqual(len(commands), 1)
        ask.assert_called_once_with("zh_CN", "phase03.monitoring.setup_confirm_plan")

    def test_webhooks_are_restricted_to_official_channel_endpoints(self):
        endpoints = {
            "wecom": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "a" * 32,
            "dingtalk": "https://oapi.dingtalk.com/robot/send?access_token=" + "b" * 32,
            "feishu": "https://open.feishu.cn/open-apis/bot/v2/hook/12345678-abcd",
        }
        for kind, endpoint in endpoints.items():
            with self.subTest(kind=kind):
                self.assertEqual(setup.validate_webhook(kind, endpoint), endpoint)

        invalid = {
            "wecom": "https://attacker.example/cgi-bin/webhook/send?key=" + "a" * 32,
            "dingtalk": "https://oapi.dingtalk.com.evil.example/robot/send?access_token=" + "b" * 32,
            "feishu": "https://open.feishu.cn.evil.example/open-apis/bot/v2/hook/id",
        }
        for kind, endpoint in invalid.items():
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    setup.validate_webhook(kind, endpoint)

    def test_email_receiver_override_is_validated(self):
        responses = iter(("1", "atlas-email", "zh_CN", "warning", "invalid receiver",
                          "smtp.example.test",
                          "465", "sender@example.test", "receiver@example.test", "sender@example.test", "pass"))
        with patch.object(setup, "ask", side_effect=lambda *args, **kwargs: next(responses)):
            with self.assertRaises(ValueError):
                setup.configure_route("en", "monitoring", "alertmanager", "0.28.1",
                                      "kube-prometheus-stack", "registry.test/relay@sha256:" + "a" * 64,
                                      "", "", 1)


if __name__ == "__main__":
    unittest.main()
