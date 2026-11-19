# SPDX-License-Identifier: MIT
import unittest

from scripts.operations.models import OperationError
from scripts.operations.monitoring import notification_secrets


class FakeKubernetes:
    def __init__(self):
        self.deleted = []
        self.monitoring_resources = {
            "alertmanagerconfigs.monitoring.coreos.com",
            "alertmanagers.monitoring.coreos.com",
            "podmonitors.monitoring.coreos.com",
            "servicemonitors.monitoring.coreos.com",
            "prometheuses.monitoring.coreos.com",
            "prometheusrules.monitoring.coreos.com",
        }
        self.denied = None

    def call(self, args, json_output=True):
        assert args == ["api-resources", "--api-group=monitoring.coreos.com",
                        "--namespaced=true", "--verbs=list", "-o", "name"] and not json_output
        return "\n".join(sorted(self.monitoring_resources))

    def notification_secret_identities(self, _namespace):
        owner = notification_secrets.OWNER
        return [
            {"name": "atlas-old-email", "uid": "uid-old", "resourceVersion": "2", "owner": owner,
             "component": "route", "receiver": "atlas-email"},
            {"name": "atlas-old-relay", "uid": "uid-relay", "resourceVersion": "3", "owner": owner,
             "component": "relay-auth", "receiver": ""},
            {"name": "atlas-pending", "uid": "uid-pending", "resourceVersion": "1", "owner": owner,
             "component": "route", "receiver": "atlas-email"},
            {"name": "atlas-referenced", "uid": "uid-shared", "resourceVersion": "4", "owner": owner,
             "component": "route", "receiver": "atlas-email"},
            {"name": "atlas-serviceaccount-pull", "uid": "uid-sa", "resourceVersion": "6", "owner": owner,
             "component": "route", "receiver": "atlas-email"},
            {"name": "atlas-ingress-tls", "uid": "uid-ingress", "resourceVersion": "7", "owner": owner,
             "component": "route", "receiver": "atlas-email"},
            {"name": "atlas-other-route", "uid": "uid-other", "resourceVersion": "5", "owner": owner,
             "component": "route", "receiver": "atlas-feishu"},
            {"name": "atlas-monitor-token", "uid": "uid-monitor", "resourceVersion": "8", "owner": owner,
             "component": "route", "receiver": "atlas-email"},
            {"name": "atlas-alertmanager-config", "uid": "uid-config", "resourceVersion": "9", "owner": owner,
             "component": "route", "receiver": "atlas-email"},
        ]

    def get(self, kind, namespace=None):
        if kind == self.denied:
            raise OperationError("ops.change.kubernetes")
        if kind == "deployment":
            return {"items": [{"spec": {"template": {"spec": {"containers": [{
                "env": [{"name": "MAIL_PASSWORD", "valueFrom": {
                    "secretKeyRef": {"name": "atlas-referenced", "key": "password"}}}],
                "envFrom": [{"secretRef": {"name": "atlas-envfrom"}}],
                "imagePullSecrets": [{"name": "atlas-pull"}],
                "volumes": [{"secret": {"secretName": "atlas-volume"}},
                            {"projected": {"sources": [{"secret": {"name": "atlas-projected"}}]}}],
            }]}}}}]}
        if kind == "alertmanagerconfigs.monitoring.coreos.com":
            return {"items": [{"spec": {"receivers": [{"emailConfigs": [{
                "authPassword": {"name": "atlas-alertmanager", "key": "password"}}]}]}}]}
        if kind == "alertmanagers.monitoring.coreos.com":
            return {"items": [
                {"metadata": {"name": "main"}, "spec": {"configSecret": "atlas-alertmanager-config"}},
                {"metadata": {"name": "implicit"}, "spec": {}},
            ]}
        if kind == "servicemonitors.monitoring.coreos.com":
            return {"items": [{"spec": {"endpoints": [
                {"bearerTokenSecret": {"name": "atlas-monitor-token", "key": "password"}},
                {"basicAuth": {"password": {"name": "atlas-basic-auth", "key": "password"}}},
                {"tlsConfig": {"ca": {"configMap": {"name": "atlas-config-map", "key": "ca.crt"}}}},
            ]}}]}
        if kind == "podmonitors.monitoring.coreos.com":
            return {"items": [{"spec": {"podMetricsEndpoints": [
                {"authorization": {"credentials": {"name": "atlas-podmonitor-token", "key": "token"}}},
            ]}}]}
        if kind == "prometheuses.monitoring.coreos.com":
            return {"items": [{"spec": {"secrets": ["atlas-prometheus-secret"],
                                       "additionalScrapeConfigs": {"name": "atlas-extra-config", "key": "config"}}}]}
        if kind == "serviceaccount":
            return {"items": [{"imagePullSecrets": [{"name": "atlas-serviceaccount-pull"}],
                               "secrets": [{"name": "atlas-serviceaccount-secret"}]}]}
        if kind == "ingress":
            return {"items": [{"spec": {"tls": [{"secretName": "atlas-ingress-tls"}]}}]}
        if kind == "persistentvolume":
            return {"items": [{"spec": {"csi": {"nodeStageSecretRef": {
                "name": "atlas-csi", "namespace": "monitoring"}}}}]}
        return {"items": []}

    def delete_notification_secret(self, _namespace, identity):
        self.deleted.append(identity["name"])


class NotificationSecretLifecycleTests(unittest.TestCase):
    def test_labels_distinguish_route_credentials_from_relay_auth(self):
        self.assertEqual(notification_secrets.labels("route", "atlas-email"), {
            "atlas-kubekit.io/owner": "monitoring-notification-setup",
            "atlas-kubekit.io/component": "route",
            "atlas-kubekit.io/receiver": "atlas-email",
        })
        self.assertEqual(notification_secrets.labels("relay-auth"), {
            "atlas-kubekit.io/owner": "monitoring-notification-setup",
            "atlas-kubekit.io/component": "relay-auth",
        })
        with self.assertRaises(ValueError):
            notification_secrets.labels("route", "not a receiver")

    def test_reference_scan_covers_workloads_alertmanager_monitoring_and_csi(self):
        client = FakeKubernetes()
        names = notification_secrets.referenced_names(client, "monitoring")
        self.assertEqual(names, {"atlas-referenced", "atlas-envfrom", "atlas-pull", "atlas-volume",
                                 "atlas-projected", "atlas-alertmanager", "atlas-serviceaccount-pull",
                                 "atlas-ingress-tls", "atlas-csi", "atlas-serviceaccount-secret",
                                 "atlas-monitor-token", "atlas-basic-auth", "atlas-alertmanager-config",
                                 "alertmanager-implicit", "atlas-podmonitor-token",
                                 "atlas-prometheus-secret", "atlas-extra-config"})

    def test_cleanup_removes_only_unreferenced_owned_secrets_for_current_receiver(self):
        client = FakeKubernetes()
        candidates = notification_secrets.replacement_candidates(client, "monitoring", "atlas-email", {
            "atlas-old-email", "atlas-old-relay", "atlas-referenced", "atlas-serviceaccount-pull",
            "atlas-ingress-tls", "atlas-other-route", "atlas-monitor-token", "atlas-alertmanager-config",
        })
        result = notification_secrets.cleanup_unreferenced(client, "monitoring", candidates)
        self.assertEqual(client.deleted, ["atlas-old-email"])
        self.assertEqual(result, {"removed": ["atlas-old-email"],
                                  "retained": ["atlas-referenced", "atlas-serviceaccount-pull",
                                               "atlas-ingress-tls", "atlas-monitor-token",
                                               "atlas-alertmanager-config"]})

    def test_installed_monitoring_api_permission_failure_preserves_all_candidates(self):
        client = FakeKubernetes()
        client.denied = "servicemonitors.monitoring.coreos.com"
        candidates = notification_secrets.replacement_candidates(
            client, "monitoring", "atlas-email", {"atlas-old-email"})
        with self.assertRaises(OperationError):
            notification_secrets.cleanup_unreferenced(client, "monitoring", candidates)
        self.assertEqual(client.deleted, [])


if __name__ == "__main__":
    unittest.main()
