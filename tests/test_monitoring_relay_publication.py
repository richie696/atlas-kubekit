# SPDX-License-Identifier: MIT
import json
import copy
import base64
import hashlib
import zipfile
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from scripts.operations.models import OperationError, fingerprint
from scripts.operations import configuration
from scripts.operations.capabilities import monitoring_robot
from scripts.operations.capabilities import monitoring_email_diagnostics
from scripts.operations.monitoring import relay_publication as publication


class RelayPublicationTest(unittest.TestCase):
    def setUp(self):
        self.routes = [{"receiver": "team-wecom", "kind": "wecom", "language": "zh_CN",
                        "secretName": "wecom-secret", "endpointKey": "webhook", "secretKey": None},
                       {"receiver": "team-dingtalk", "kind": "dingtalk", "language": "zh_TW",
                        "secretName": "dingtalk-secret", "endpointKey": "endpoint", "secretKey": "sign"}]

    def test_configmap_keeps_secret_values_out_and_builds_deterministic_routes(self):
        rendered = publication.config_map("monitoring", "prom-stack", self.routes)
        config = json.loads(rendered["data"]["routes.json"])
        self.assertEqual([item["receiver"] for item in config["routes"]], ["team-dingtalk", "team-wecom"])
        self.assertIn("endpoint_env", config["routes"][0])
        self.assertIn("secret_env", config["routes"][0])
        self.assertNotIn("wecom-secret", rendered["data"]["routes.json"])
        self.assertNotIn("webhook", rendered["data"]["routes.json"])
        self.assertNotIn("sign", rendered["data"]["routes.json"])

    def test_sidecar_uses_secret_references_and_restricted_health_endpoint(self):
        rendered = publication.container("registry.example.test/relay@sha256:" + "a" * 64,
                                         "relay-auth", "token", self.routes)
        env = {item["name"]: item for item in rendered["env"]}
        self.assertEqual(env["ATLAS_RELAY_TOKEN"]["valueFrom"]["secretKeyRef"],
                         {"name": "relay-auth", "key": "token"})
        self.assertTrue(all("value" not in item for item in rendered["env"]
                            if item["name"] not in {publication.OWNER_ENV, "ATLAS_LOCALES_DIR",
                                                     "PYTHONDONTWRITEBYTECODE"}))
        self.assertEqual(next(item["value"] for item in rendered["env"]
                              if item["name"] == publication.OWNER_ENV), publication.OWNER_MARKER)
        self.assertTrue(rendered["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(rendered["ports"], [
            {"name": "atlas-relay", "containerPort": publication.PORT, "protocol": "TCP"},
            {"name": publication.METRICS_PORT_NAME, "containerPort": publication.METRICS_PORT, "protocol": "TCP"},
        ])
        self.assertIn("--metrics-address", rendered["args"])
        self.assertIn("0.0.0.0", rendered["args"])
        self.assertEqual(rendered["command"], ["python", publication.CODE_MOUNT_PATH + "/relay.pyz"])
        probe = rendered["readinessProbe"]["exec"]["command"]
        self.assertEqual(probe[:2], ["python3", "-c"])
        self.assertIn("127.0.0.1:18080/-/healthy", probe[2])
        self.assertEqual(rendered["livenessProbe"]["exec"]["command"], probe)
        self.assertEqual(publication.volume()["configMap"]["name"], publication.CONFIG_NAME)

    def test_relay_runtime_bundle_is_deterministic_immutable_and_content_addressed(self):
        first = publication.code_config_map("monitoring", "stack")
        second = publication.code_config_map("monitoring", "stack")
        self.assertEqual(first, second)
        self.assertTrue(first["immutable"])
        body = {"binaryData": first["binaryData"], "data": first["data"]}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(first["metadata"]["annotations"][publication.SPEC_HASH], digest)
        self.assertEqual(first["metadata"]["name"], publication.CODE_CONFIG_PREFIX + digest[:16])
        archive = zipfile.ZipFile(BytesIO(base64.b64decode(first["binaryData"]["relay.pyz"])))
        self.assertIsNone(archive.testzip())
        self.assertIn("scripts/operations/monitoring/channels/relay.py", archive.namelist())
        self.assertLess(len(json.dumps(first)), publication.MAX_CODE_CONFIG_BYTES)
        mounted = publication.code_volume(first["metadata"]["name"])
        self.assertEqual(mounted["configMap"]["name"], first["metadata"]["name"])
        self.assertEqual([item["path"] for item in mounted["configMap"]["items"]], [
            "relay.pyz", "locales/en.json", "locales/zh_cn.json", "locales/zh_tw.json"])

    def test_default_runtime_image_uses_selected_mirror_and_accepts_only_pinned_override(self):
        with TemporaryDirectory() as directory:
            source_file = Path(directory) / "source.json"
            source_file.write_text('{"profile":"daocloud"}', encoding="utf-8")
            with patch.dict("os.environ", {"ATLAS_ADDON_SOURCE_CONFIG": str(source_file)}):
                self.assertEqual(publication.runtime_image(),
                    "m.daocloud.io/docker.io/library/python@sha256:" + "70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f")
                override = "registry.example.test/python@sha256:" + "b" * 64
                self.assertEqual(publication.runtime_image(override), override)
                with self.assertRaises(ValueError):
                    publication.runtime_image("python:latest")

    def test_route_resource_has_local_webhook_and_secret_backed_auth(self):
        rendered = publication.alertmanager_config("monitoring", "prom-stack", "team-wecom", "warning",
                                                    "relay-auth", "token")
        self.assertLessEqual(len(rendered["metadata"]["name"]), 63)
        webhook = rendered["spec"]["receivers"][0]["webhookConfigs"][0]
        self.assertEqual(rendered["spec"]["route"]["receiver"], "team-wecom")
        self.assertEqual(rendered["spec"]["receivers"][0]["name"], "team-wecom")
        self.assertEqual(webhook["url"], "http://127.0.0.1:18080/v1/alert")
        self.assertEqual(webhook["httpConfig"]["authorization"]["credentials"],
                         {"name": "relay-auth", "key": "token"})
        self.assertTrue(webhook["sendResolved"])

    def test_invalid_image_route_and_receiver_fail_closed(self):
        for image in ("relay:latest", "relay@sha256:bad"):
            with self.subTest(image=image), self.assertRaises(ValueError):
                publication.container(image, "relay-auth", "token", self.routes)
        for routes in ([], self.routes + [self.routes[0]],
                       [{**self.routes[1], "secretKey": None}],
                       [{**self.routes[0], "endpointKey": "../token"}]):
            with self.subTest(routes=routes), self.assertRaises(ValueError):
                publication.route_config(routes)
        rendered = publication.alertmanager_config("monitoring", "prom-stack", "x" * 63, "warning",
                                                    "relay-auth", "token")
        self.assertLessEqual(len(rendered["metadata"]["name"]), 63)

    def test_route_metadata_merge_replaces_one_route_and_preserves_others(self):
        original = publication.route_config(self.routes)
        replacement = {**self.routes[0], "language": "en", "endpointKey": "url"}
        merged = publication.merge_route_config(original, replacement)
        self.assertEqual([item["receiver"] for item in merged["routes"]],
                         ["team-dingtalk", "team-wecom"])
        wecom = next(item for item in merged["routes"] if item["receiver"] == "team-wecom")
        self.assertEqual(wecom["language"], "en")
        self.assertEqual(wecom["endpoint_env"], original["routes"][1]["endpoint_env"])
        self.assertEqual(next(item for item in merged["routes"] if item["receiver"] == "team-dingtalk"),
                         original["routes"][0])

    def test_diagnostics_config_is_validated_and_preserved_while_routes_change(self):
        diagnostics = {"loki_endpoint": "http://loki.logging.svc", "before_seconds": 300,
                       "after_seconds": 60, "max_lines": 20, "max_message_bytes": 8192}
        normalized = publication.route_config(self.routes, diagnostics)["diagnostics"]
        original = publication.route_config(self.routes, diagnostics)
        merged = publication.merge_route_config(original, {**self.routes[0], "language": "en"})
        self.assertEqual(merged["diagnostics"], normalized)
        config = json.loads(publication.config_map_data("monitoring", "stack", merged)["data"]["routes.json"])
        self.assertEqual(config["diagnostics"], normalized)
        for invalid in ({**diagnostics, "loki_endpoint": "https://grafana.example.com"},
                        {**diagnostics, "max_lines": 101}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                publication.route_config(self.routes, invalid)

    def test_email_relay_route_keeps_smtp_password_in_secret_reference(self):
        route = {"receiver": "ops-email", "kind": "email", "language": "zh_CN",
                 "secretName": "smtp-secret", "passwordKey": "password", "smtpHost": "smtp.example.com",
                 "smtpPort": 465, "sender": "alerts@example.com", "recipient": "ops@example.com",
                 "username": "alerts@example.com"}
        config = publication.route_config([route])
        serialized = json.dumps(config)
        self.assertNotIn("smtp-secret", serialized)
        self.assertNotIn('"password"', serialized)
        route_metadata = config["routes"][0]
        self.assertEqual(route_metadata["kind"], "email")
        self.assertIn("password_env", route_metadata)
        self.assertEqual(publication.route_environment(route)[0]["valueFrom"]["secretKeyRef"],
                         {"name": "smtp-secret", "key": "password"})
        relay_container = publication.container("registry.example.test/relay@sha256:" + "a" * 64,
                                                "relay-auth", "token", [route])
        env = {item["name"]: item for item in relay_container["env"]}
        self.assertEqual(env[route_metadata["password_env"]]["valueFrom"]["secretKeyRef"],
                         {"name": "smtp-secret", "key": "password"})

    def test_capability_publish_preserves_owned_routes_and_uses_only_secret_refs(self):
        class Client:
            def __init__(self):
                self.manager = {"apiVersion": "monitoring.coreos.com/v1", "kind": "Alertmanager",
                    "metadata": {"name": "stack", "namespace": "monitoring", "uid": "manager-uid",
                                 "resourceVersion": "1"},
                    "spec": {"version": "v0.28.1", "alertmanagerConfigSelector": {"matchLabels": {
                        "release": "stack", publication.OWNER: publication.OWNER_VALUE}},
                        "alertmanagerConfigMatcherStrategy": {"type": "OnNamespaceExceptForAlertmanagerNamespace"}}}
                self.objects = {}
                self.writes = []
            def get(self, kind, name, namespace, optional=False):
                if kind == publication.ALERTMANAGER:
                    return copy.deepcopy(self.manager)
                return copy.deepcopy(self.objects.get((kind, namespace, name)))
            def secret_metadata(self, namespace, name, key):
                return {"uid": "secret-" + name, "resourceVersion": "1"}
            def create(self, value):
                return self._store(value, "create")
            def replace(self, value):
                return self._store(value, "replace")
            def _store(self, value, operation):
                item = copy.deepcopy(value)
                if item["kind"] == "Alertmanager":
                    self.manager = item
                else:
                    key = (publication.CONFIG_MAP if item["kind"] == "ConfigMap" else
                           publication.ALERTMANAGER_CONFIG, item["metadata"]["namespace"],
                           item["metadata"]["name"])
                    previous = self.objects.get(key)
                    item["metadata"].setdefault("uid", previous["metadata"]["uid"] if previous else "uid-" + item["metadata"]["name"])
                    item["metadata"]["resourceVersion"] = str(int(previous["metadata"]["resourceVersion"]) + 1) if previous else "1"
                    self.objects[key] = item
                self.writes.append((operation, item["kind"], copy.deepcopy(item)))
                return copy.deepcopy(item)

        client = Client()
        values = {"namespace": "monitoring", "release": "stack", "managerName": "stack",
                  "version": "0.28.1", "image": "registry.example.test/relay@sha256:" + "a" * 64,
                  "authSecret": "relay-auth", "authKey": "token", "route": self.routes[0], "severity": "warning"}
        values["expected"] = publication.capture(client, values)
        before = publication.desired(values)
        result = publication.publish(client, values)
        self.assertEqual(result["delivery"], "unverified")
        self.assertEqual([kind for _, kind, _ in client.writes],
                         ["ConfigMap", "ConfigMap", "Alertmanager", "AlertmanagerConfig"])
        manager_spec = client.manager["spec"]
        sidecar = next(item for item in manager_spec["containers"] if item["name"] == publication.CONTAINER_NAME)
        self.assertEqual(manager_spec["podMetadata"]["labels"][publication.METRICS_POD_LABEL],
                         publication.METRICS_POD_LABEL_VALUE)
        self.assertIn("atlas-kubekit.io/relay-config-sha256", manager_spec["podMetadata"]["annotations"])
        self.assertTrue(all("value" not in item or item["name"] in {
            publication.OWNER_ENV, "ATLAS_LOCALES_DIR", "PYTHONDONTWRITEBYTECODE"} for item in sidecar["env"]))
        self.assertEqual(client.writes[1][2]["data"]["routes.json"], before["configMap"]["data"]["routes.json"])
        self.assertNotIn("credentials-value", json.dumps(client.writes))

        other_route = self.routes[1]
        next_values = dict(values, route=other_route)
        next_values["expected"] = publication.capture(client, next_values)
        publication.publish(client, next_values)
        self.assertEqual(publication.existing_credentials(client.manager["spec"], values["route"]["receiver"]),
                         (("relay-auth", "token"), frozenset({values["route"]["secretName"]})))
        with self.assertRaises(OperationError):
            publication.capture(client, dict(values, authSecret="other-token"))

        replacement = {"receiver": values["route"]["receiver"], "kind": "email", "language": "zh_CN",
                       "secretName": "new-smtp-secret", "passwordKey": "password",
                       "smtpHost": "smtp.example.com", "smtpPort": 465, "sender": "alerts@example.com",
                       "recipient": "ops@example.com", "username": "alerts@example.com"}
        replaced_values = dict(values, route=replacement)
        replaced_values["expected"] = publication.capture(client, replaced_values)
        publication.publish(client, replaced_values)
        self.assertEqual(publication.existing_credentials(client.manager["spec"], replacement["receiver"]),
                         (("relay-auth", "token"), frozenset({"new-smtp-secret"})))
        self.assertEqual(publication.existing_credentials(client.manager["spec"], other_route["receiver"]),
                         (("relay-auth", "token"), frozenset({other_route["secretName"]})))

    def test_api_preflight_uses_replace_for_existing_resources(self):
        class Client:
            def __init__(self):
                self.manager = {"metadata": {"name": "stack", "namespace": "monitoring", "uid": "manager", "resourceVersion": "7"},
                    "spec": {"version": "v0.28.1", "alertmanagerConfigSelector": {"matchLabels": {
                        "release": "stack", publication.OWNER: publication.OWNER_VALUE}},
                        "alertmanagerConfigMatcherStrategy": {"type": "None"}}}
                self.objects = {}
                self.calls = []
            def get(self, kind, name, namespace, optional=False):
                return copy.deepcopy(self.manager if kind == publication.ALERTMANAGER else self.objects.get((kind, name)))
            def secret_metadata(self, namespace, name, key):
                return {"uid": "secret", "resourceVersion": "1"}
            def call(self, args, value=None):
                self.calls.append((list(args), copy.deepcopy(value)))
                return value

        client = Client()
        values = {"namespace": "monitoring", "release": "stack", "managerName": "stack", "version": "0.28.1",
                  "image": "registry.example.test/relay@sha256:" + "a" * 64, "authSecret": "relay-auth", "authKey": "token",
                  "route": self.routes[0], "severity": "warning"}
        values["expected"] = publication.capture(client, values)
        publication.preflight(client, values)
        self.assertEqual([item[0][0] for item in client.calls], ["create", "create", "replace", "create"])
        self.assertTrue(all(item[0][-2:] == ["-o", "json"] for item in client.calls))
        self.assertEqual(client.calls[2][1]["metadata"]["resourceVersion"], "7")
        client.objects[(publication.CONFIG_MAP, publication.CONFIG_NAME)] = {
            "kind": "ConfigMap", "metadata": {"name": publication.CONFIG_NAME, "namespace": "monitoring",
                "uid": "map", "resourceVersion": "4", "labels": {publication.OWNER: publication.OWNER_VALUE},
                "annotations": {publication.SPEC_HASH: fingerprint({"routes.json": json.dumps(
                    {"version": 1, "routes": []}, sort_keys=True, separators=(",", ":"))})}},
            "data": {"routes.json": '{"routes":[],"version":1}'}}
        values["expected"] = publication.capture(client, values)
        client.calls.clear()
        publication.preflight(client, values)
        self.assertEqual(client.calls[0][0][0], "create")
        self.assertEqual(client.calls[1][0][0], "replace")
        self.assertEqual(client.calls[1][1]["metadata"]["resourceVersion"], "4")

    def test_foreign_manager_selection_and_modified_route_fail_before_writes(self):
        class Client:
            def __init__(self):
                self.manager = {"metadata": {"name": "stack", "namespace": "monitoring", "uid": "manager", "resourceVersion": "1"},
                    "spec": {"version": "v0.28.1", "alertmanagerConfigSelector": {"matchLabels": {"release": "stack"}},
                        "alertmanagerConfigMatcherStrategy": {"type": "None"}}}
                self.objects = {}
                self.writes = 0
            def get(self, kind, name, namespace, optional=False):
                return copy.deepcopy(self.manager if kind == publication.ALERTMANAGER else self.objects.get((kind, name)))
            def secret_metadata(self, namespace, name, key):
                return {"uid": "secret", "resourceVersion": "1"}
            def create(self, value):
                self.writes += 1
            replace = create

        client = Client()
        values = {"namespace": "monitoring", "release": "stack", "managerName": "stack", "version": "0.28.1",
                  "image": "registry.example.test/relay@sha256:" + "a" * 64, "authSecret": "relay-auth", "authKey": "token",
                  "route": self.routes[0], "severity": "warning"}
        with self.assertRaises(OperationError):
            publication.capture(client, values)
        self.assertEqual(client.writes, 0)

        client.manager["spec"]["alertmanagerConfigSelector"]["matchLabels"][publication.OWNER] = publication.OWNER_VALUE
        values["expected"] = publication.capture(client, values)
        route_manifest = publication.desired(values)["route"]
        route_manifest["metadata"]["uid"] = "foreign"
        route_manifest["metadata"]["resourceVersion"] = "1"
        route_manifest["metadata"]["labels"][publication.OWNER] = "some-other-manager"
        route_manifest["metadata"]["annotations"] = {publication.SPEC_HASH: fingerprint(route_manifest["spec"])}
        client.objects[(publication.ALERTMANAGER_CONFIG, route_manifest["metadata"]["name"])] = route_manifest
        with self.assertRaises(OperationError):
            publication.capture(client, values)
        self.assertEqual(client.writes, 0)

    def test_robot_capability_maps_dash_to_no_signing_secret(self):
        class Client:
            def get(self, kind, name, namespace, optional=False):
                if kind == publication.ALERTMANAGER:
                    return {"metadata": {"uid": "manager", "resourceVersion": "1"},
                        "spec": {"version": "v0.28.1", "alertmanagerConfigSelector": {"matchLabels": {
                            publication.OWNER: publication.OWNER_VALUE}},
                            "alertmanagerConfigMatcherStrategy": {"type": "None"}}}
                return None
            def secret_metadata(self, namespace, name, key):
                return {"uid": "secret", "resourceVersion": "1"}
        class Environment:
            client = Client()
            def verify(self, value):
                return configuration.normalize(value), {}
        from types import SimpleNamespace
        args = SimpleNamespace(monitoring_namespace="monitoring", monitoring_release="stack",
            monitoring_alertmanager_name="stack", monitoring_alertmanager_version="0.28.1",
            monitoring_relay_image="registry.example.test/relay@sha256:" + "a" * 64,
            monitoring_relay_auth_secret="relay-auth", monitoring_relay_auth_key="token",
            monitoring_robot_receiver="ops-wecom", monitoring_robot_kind="wecom",
            monitoring_notification_language="zh_CN", monitoring_robot_secret="wecom-secret",
            monitoring_robot_endpoint_key="url", monitoring_robot_signing_key="-", monitoring_severity="warning")
        values = monitoring_robot.from_arguments(args, configuration.template(), Environment())
        self.assertIsNone(values["route"]["secretKey"])
        args.monitoring_loki_endpoint = "http://loki.logging.svc"
        values = monitoring_robot.from_arguments(args, configuration.template(), Environment())
        self.assertEqual(values["diagnostics"]["loki_endpoint"], "http://loki.logging.svc")
        self.assertIsNone(values["diagnostics"]["grafana_explore_url"])

    def test_email_diagnostics_capability_creates_a_secret_backed_relay_plan(self):
        class Client:
            def __init__(self):
                self.manager = {"metadata": {"uid": "manager", "resourceVersion": "1"}, "spec": {
                    "version": "v0.28.1", "alertmanagerConfigSelector": {"matchLabels": {
                        "release": "stack", publication.OWNER: publication.OWNER_VALUE}},
                    "alertmanagerConfigMatcherStrategy": {"type": "OnNamespaceExceptForAlertmanagerNamespace"}}}
            def get(self, kind, name, namespace, optional=False):
                if kind == publication.ALERTMANAGER:
                    return copy.deepcopy(self.manager)
                return None
            def secret_metadata(self, namespace, name, key):
                return {"uid": "secret-" + name, "resourceVersion": "1"}
            def call(self, args, value=None):
                return copy.deepcopy(value)
        class Environment:
            client = Client()
            def verify(self, value):
                return configuration.normalize(value), {}
        from types import SimpleNamespace
        args = SimpleNamespace(operation="monitoring-email-diagnostics", monitoring_namespace="monitoring",
            monitoring_release="stack", monitoring_alertmanager_name="stack", monitoring_alertmanager_version="0.28.1",
            monitoring_relay_image="registry.example.test/relay@sha256:" + "a" * 64,
            monitoring_relay_auth_secret="relay-auth", monitoring_relay_auth_key="token",
            monitoring_email_receiver="ops-email", monitoring_email_secret="smtp-secret",
            monitoring_email_secret_key="password", monitoring_smtp_host="smtp.example.com",
            monitoring_smtp_port=465, monitoring_email_sender="alerts@example.com",
            monitoring_email_recipient="ops@example.com", monitoring_email_username="alerts@example.com",
            monitoring_severity="warning", monitoring_notification_language="zh_CN",
            monitoring_loki_endpoint="http://loki.logging.svc", monitoring_log_before_seconds=300,
            monitoring_log_after_seconds=60, monitoring_log_max_lines=20, monitoring_log_max_message_bytes=8192,
            monitoring_route_matcher=None, monitoring_group_by=None, monitoring_group_wait="30s",
            monitoring_group_interval="5m", monitoring_repeat_interval="12h")
        values = monitoring_email_diagnostics.from_arguments(args, configuration.template(), Environment())
        self.assertEqual(values["route"]["kind"], "email")
        self.assertEqual(values["route"]["passwordKey"], "password")
        desired = publication.desired(values)
        routes = json.loads(desired["configMap"]["data"]["routes.json"])
        self.assertEqual(routes["routes"][0]["receiver"], "ops-email")
        self.assertEqual(routes["diagnostics"]["loki_endpoint"], "http://loki.logging.svc")
        sidecar_env = {item["name"]: item for item in desired["container"]["env"]}
        password_env = routes["routes"][0]["password_env"]
        self.assertEqual(sidecar_env[password_env]["valueFrom"]["secretKeyRef"],
                         {"name": "smtp-secret", "key": "password"})
        config_map_text = json.dumps(desired["configMap"])
        self.assertNotIn("smtp-secret", config_map_text)
        self.assertNotIn('"key":"password"', config_map_text)


if __name__ == "__main__":
    unittest.main()
