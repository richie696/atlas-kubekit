# SPDX-License-Identifier: MIT
import unittest
from unittest.mock import patch

from scripts.operations.adapters.kubernetes import Kubernetes
from scripts.operations.models import OperationError


class KubernetesAdapterTest(unittest.TestCase):
    def test_podmonitor_is_a_supported_namespaced_monitoring_resource(self):
        client = Kubernetes("/tmp/kubeconfig")
        with patch.object(client, "call", return_value={"items": []}) as call:
            client.get("podmonitors.monitoring.coreos.com", namespace="monitoring")
        args = call.call_args.args[0]
        self.assertIn("podmonitors.monitoring.coreos.com", args)
        self.assertNotIn("--all-namespaces", args)
        self.assertIn("-n", args)
        self.assertIn("monitoring", args)

    def test_monitoring_resource_delete_uses_uid_and_resource_version_preconditions(self):
        client = Kubernetes("/tmp/kubeconfig")
        with patch.object(client, "call", return_value={"kind": "Status"}) as call:
            client.delete_monitoring_resource("prometheusrule", "monitoring", "atlas-test", "uid-123", "17")
        args = call.call_args.args[0]
        payload = call.call_args.kwargs["value"]
        self.assertIn("--raw=/apis/monitoring.coreos.com/v1/namespaces/monitoring/prometheusrules/atlas-test", args)
        self.assertEqual(payload["preconditions"], {"uid": "uid-123", "resourceVersion": "17"})

    def test_monitoring_resource_delete_rejects_unbounded_target(self):
        client = Kubernetes("/tmp/kubeconfig")
        for values in (("secret", "monitoring", "credentials", "uid", "1"),
                       ("networkpolicy", "*", "policy", "uid", "1")):
            with self.assertRaises(OperationError):
                client.delete_monitoring_resource(*values)

    def test_agent_cluster_refuses_to_publish_center_rules_and_routes(self):
        client = Kubernetes("/tmp/kubeconfig")
        for kind in ("PrometheusRule", "AlertmanagerConfig"):
            value = {"apiVersion": "monitoring.coreos.com/v1", "kind": kind,
                     "metadata": {"name": "atlas-test", "namespace": "monitoring"}}
            for operation in (client.create, client.replace):
                with self.subTest(kind=kind, operation=operation.__name__), \
                        patch.object(client, "get", return_value={"data": {"mode": "agent"}}), \
                        patch.object(client, "call") as call:
                    with self.assertRaises(OperationError) as raised:
                        operation(value)
                    self.assertEqual(raised.exception.key, "ops.monitoring.center_resources_require_center")
                    call.assert_not_called()

    def test_notification_secret_identity_query_returns_only_metadata(self):
        client = Kubernetes("/tmp/kubeconfig")
        raw = "atlas-old\tuid-1\t17\tmonitoring-notification-setup\troute\tatlas-email\n"
        with patch.object(client, "call", return_value=raw) as call:
            result = client.notification_secret_identities("monitoring")
        self.assertEqual(result, [{"name": "atlas-old", "uid": "uid-1", "resourceVersion": "17",
                                   "owner": "monitoring-notification-setup", "component": "route",
                                   "receiver": "atlas-email"}])
        args = call.call_args.args[0]
        self.assertEqual(args[:4], ["get", "secrets", "-n", "monitoring"])
        self.assertNotIn("-o", args[:4])
        self.assertIn("go-template=", args[-1])
        self.assertNotIn("secret-value", str(args))

    def test_notification_secret_identity_normalizes_missing_receiver_label(self):
        client = Kubernetes("/tmp/kubeconfig")
        raw = "atlas-old-relay\tuid-1\t17\tmonitoring-notification-setup\trelay-auth\t%!s(<nil>)\n"
        with patch.object(client, "call", return_value=raw):
            result = client.notification_secret_identities("monitoring")
        self.assertEqual(result[0]["receiver"], "")

    def test_notification_secret_delete_refetches_owner_and_uses_uid_resource_version_fences(self):
        client = Kubernetes("/tmp/kubeconfig")
        identity = {"name": "atlas-old", "uid": "uid-1", "resourceVersion": "17",
                    "owner": "monitoring-notification-setup", "component": "route",
                    "receiver": "atlas-email"}
        current = "atlas-old\tuid-1\t17\tmonitoring-notification-setup\troute\tatlas-email"
        with patch.object(client, "call", side_effect=(current, {"kind": "Status"})) as call:
            client.delete_notification_secret("monitoring", identity)
        args = call.call_args.args[0]
        payload = call.call_args.kwargs["value"]
        self.assertIn("--raw=/api/v1/namespaces/monitoring/secrets/atlas-old", args)
        self.assertEqual(payload["preconditions"], {"uid": "uid-1", "resourceVersion": "17"})

    def test_notification_secret_delete_rejects_stale_identity_and_unowned_secret(self):
        client = Kubernetes("/tmp/kubeconfig")
        identity = {"name": "atlas-old", "uid": "uid-1", "resourceVersion": "17",
                    "owner": "monitoring-notification-setup", "component": "route",
                    "receiver": "atlas-email"}
        with patch.object(client, "call", return_value="atlas-old\tuid-1\t18\tmonitoring-notification-setup\troute\tatlas-email") as call:
            with self.assertRaises(OperationError):
                client.delete_notification_secret("monitoring", identity)
        call.assert_called_once()
        identity["owner"] = "other"
        with patch.object(client, "call") as call:
            with self.assertRaises(OperationError):
                client.delete_notification_secret("monitoring", identity)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
