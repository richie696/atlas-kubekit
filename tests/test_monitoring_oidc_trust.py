# SPDX-License-Identifier: MIT
import tempfile
import unittest
from pathlib import Path

from scripts.addons.monitoring_oidc_trust import resources
from scripts.addons.monitoring_public_access import resources as access_resources


class MonitoringOidcTrustTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ca = Path(self.temp.name) / "oidc-ca.pem"
        self.ca.write_text("-----BEGIN CERTIFICATE-----\nZm9v\n-----END CERTIFICATE-----\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_renders_private_ca_backend_and_tls_policy_without_secrets(self):
        configmap, backend, policy = resources("https://oauth.home.arpa", self.ca)

        self.assertEqual((configmap["kind"], configmap["data"]["ca.crt"].strip()),
                         ("ConfigMap", self.ca.read_text().strip()))
        self.assertEqual(backend["spec"]["endpoints"], [{"fqdn": {
            "hostname": "oauth.home.arpa", "port": 443}}])
        self.assertEqual(policy["apiVersion"], "gateway.networking.k8s.io/v1")
        self.assertEqual(policy["spec"]["targetRefs"][0]["kind"], "Backend")
        self.assertEqual(policy["spec"]["validation"]["hostname"], "oauth.home.arpa")
        self.assertEqual(policy["spec"]["validation"]["caCertificateRefs"], [{
            "name": "atlas-monitoring-oidc-ca", "group": "", "kind": "ConfigMap"}])
        self.assertNotIn("private", str([configmap, backend, policy]).lower())

    def test_public_access_policy_uses_backend_only_when_private_ca_is_configured(self):
        default = access_resources("monitoring", "eg", "monitoring.home.arpa",
                                   "tls", "https://oauth.home.arpa", "client", "secret")
        with_ca = access_resources("monitoring", "eg", "monitoring.home.arpa",
                                   "tls", "https://oauth.home.arpa", "client", "secret",
                                   oidc_ca_bundle=self.ca)

        default_policy = next(item for item in default if item["kind"] == "SecurityPolicy")
        policy = next(item for item in with_ca if item["kind"] == "SecurityPolicy")
        self.assertNotIn("backendRefs", default_policy["spec"]["oidc"]["provider"])
        self.assertEqual(policy["spec"]["oidc"]["provider"]["backendRefs"], [{
            "group": "gateway.envoyproxy.io", "kind": "Backend",
            "name": "atlas-monitoring-oidc-provider", "port": 443}])
        self.assertEqual([item["kind"] for item in with_ca[-4:]], [
            "ConfigMap", "Backend", "BackendTLSPolicy", "SecurityPolicy"])

    def test_rejects_invalid_issuer_port_and_non_pem_bundle(self):
        with self.assertRaises(ValueError):
            resources("http://oauth.home.arpa", self.ca)
        with self.assertRaises(ValueError):
            resources("https://oauth.home.arpa:8443", self.ca)
        self.ca.write_text("not a certificate")
        with self.assertRaisesRegex(ValueError, "PEM"):
            resources("https://oauth.home.arpa", self.ca)


if __name__ == "__main__":
    unittest.main()
