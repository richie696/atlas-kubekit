import unittest

from scripts.addons.logging_values import render
from scripts.addons.sources import yaml_module


class LoggingValuesTests(unittest.TestCase):
    def values(self, *args, **kwargs):
        loki_yaml, alloy_yaml = render(*args, **kwargs)
        yaml = yaml_module()
        return yaml.safe_load(loki_yaml), yaml.safe_load(alloy_yaml)

    def test_persistent_loki_defaults_and_log_retention(self):
        loki, alloy = self.values(retention_days=14, storage_class="longhorn", storage_size="50Gi")
        self.assertEqual(loki["deploymentMode"], "SingleBinary")
        self.assertEqual(loki["singleBinary"]["replicas"], 1)
        self.assertEqual(loki["singleBinary"]["persistence"], {
            "enabled": True, "size": "50Gi", "storageClass": "longhorn",
        })
        self.assertEqual(loki["loki"]["limits_config"]["retention_period"], "336h")
        self.assertTrue(loki["loki"]["compactor"]["retention_enabled"])
        self.assertNotIn("minio", loki)
        self.assertEqual(alloy["controller"], {"type": "deployment", "replicas": 1})
        self.assertTrue(alloy["rbac"]["create"])
        self.assertEqual(alloy["rbac"]["rules"], [
            {"apiGroups": [""], "resources": ["pods"],
             "verbs": ["get", "list", "watch"]},
            {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
        ])
        self.assertEqual(alloy["rbac"]["clusterRules"], [
            {"apiGroups": [""], "resources": ["namespaces"],
             "verbs": ["get", "list", "watch"]},
        ])
        self.assertFalse(alloy["alloy"]["mounts"]["varlog"])
        self.assertIn("loki.logging.svc.cluster.local:3100/loki/api/v1/push",
                      alloy["alloy"]["configMap"]["content"])

    def test_all_namespace_collection_does_not_emit_keep_filter(self):
        _, alloy = self.values(namespaces="all")
        config = alloy["alloy"]["configMap"]["content"]
        self.assertIn("discovery.relabel \"selected_pods\"", config)
        self.assertIn('target_label = "namespace"', config)
        self.assertIn('target_label = "pod"', config)
        self.assertIn('target_label = "container"', config)
        self.assertIn('target_label = "node"', config)
        self.assertNotIn('action = "keep"', config)

    def test_namespace_allowlist_is_escaped_and_restricted(self):
        _, alloy = self.values(namespaces="monitoring,longhorn-system")
        config = alloy["alloy"]["configMap"]["content"]
        self.assertIn("discovery.relabel.selected_pods.output", config)
        self.assertIn("monitoring|longhorn-system", config)
        self.assertIn('target_label = "node"', config)

    def test_ephemeral_loki_is_explicit(self):
        loki, _ = self.values(storage_class=None)
        self.assertFalse(loki["singleBinary"]["persistence"]["enabled"])

    def test_remote_loki_push_uses_https_secret_and_optional_ca(self):
        loki, alloy = self.values(
            remote_loki_url="https://logs.example.com/loki/api/v1/push",
            remote_token_secret="central-loki-token", remote_ca_secret="central-loki-ca",
            cluster_id="prod-east",
        )
        config = alloy["alloy"]["configMap"]["content"]
        self.assertIn('url = "https://logs.example.com/loki/api/v1/push"', config)
        self.assertIn('bearer_token = sys.env("ATLAS_LOKI_TOKEN")', config)
        self.assertIn('ca_file = "/etc/atlas-loki-ca/ca.crt"', config)
        self.assertIn('target_label = "cluster"', config)
        self.assertIn('replacement = "prod-east"', config)
        self.assertEqual(alloy["alloy"]["extraEnv"][0]["valueFrom"]["secretKeyRef"],
                         {"name": "central-loki-token", "key": "token"})
        self.assertEqual(alloy["volumes"]["extra"][0]["secret"]["secretName"], "central-loki-ca")
        self.assertNotIn("/loki/api/v1/push", loki["loki"]["storage"]["filesystem"]["chunks_directory"])

    def test_remote_loki_requires_https_and_auth_secret(self):
        for options in (
            {"remote_loki_url": "http://logs.example.com/loki/api/v1/push",
             "remote_token_secret": "token"},
            {"remote_loki_url": "https://user:pass@logs.example.com/loki/api/v1/push",
             "remote_token_secret": "token"},
            {"remote_loki_url": "https://logs.example.com/loki/api/v1/push"},
            {"remote_token_secret": "token"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.values(**options)

    def test_rejects_invalid_retention_storage_and_scope(self):
        for options in (
            {"retention_days": 0},
            {"retention_days": 366},
            {"storage_class": "../../secret"},
            {"storage_size": "0Gi"},
            {"namespaces": "monitoring,Monitoring"},
            {"namespaces": "monitoring,monitoring"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.values(**options)


if __name__ == "__main__":
    unittest.main()
