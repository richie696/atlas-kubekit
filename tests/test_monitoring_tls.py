# SPDX-License-Identifier: MIT
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.addons.monitoring_tls import secret_manifest, validated_tls_material


class MonitoringTlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        cls.ca_key = cls.directory / "ca.key"
        cls.ca_cert = cls.directory / "ca.crt"
        cls.key = cls.directory / "tls.key"
        cls.csr = cls.directory / "tls.csr"
        cls.cert = cls.directory / "tls.crt"
        cls.extensions = cls.directory / "tls.ext"
        cls.extensions.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:grafana.monitoring.home.arpa,"
            "DNS:prometheus.monitoring.home.arpa,"
            "DNS:alertmanager.monitoring.home.arpa,DNS:ops.monitoring.home.arpa\n"
        )
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(cls.ca_key), "-out", str(cls.ca_cert), "-days", "60",
                        "-subj", "/CN=monitoring-test-ca"], check=True,
                       capture_output=True, timeout=20)
        subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(cls.key), "-out", str(cls.csr), "-subj",
                        "/CN=grafana.monitoring.home.arpa"], check=True,
                       capture_output=True, timeout=20)
        subprocess.run(["openssl", "x509", "-req", "-in", str(cls.csr), "-CA",
                        str(cls.ca_cert), "-CAkey", str(cls.ca_key), "-CAcreateserial",
                        "-out", str(cls.cert), "-days", "397", "-sha256", "-extfile",
                        str(cls.extensions)], check=True, capture_output=True, timeout=20)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_accepts_matching_certificate_and_renders_managed_tls_secret(self):
        cert, key = validated_tls_material(
            self.cert, self.key, "monitoring.home.arpa")
        self.assertTrue(cert.startswith(b"-----BEGIN CERTIFICATE-----"))
        self.assertTrue(key.startswith(b"-----BEGIN PRIVATE KEY-----"))

        manifest = secret_manifest("atlas-monitoring-tls", "monitoring.home.arpa",
                                   self.cert, self.key)
        self.assertEqual(manifest["type"], "kubernetes.io/tls")
        self.assertEqual(manifest["metadata"]["namespace"], "monitoring")
        self.assertEqual(manifest["metadata"]["labels"][
            "atlas-kubekit.io/purpose"], "monitoring-public-access-tls")
        self.assertEqual(json.loads(json.dumps(manifest)), manifest)
        self.assertNotIn("PRIVATE KEY", json.dumps(manifest))

    def test_rejects_wrong_domain_and_mismatched_key(self):
        with self.assertRaisesRegex(ValueError, "invalid"):
            validated_tls_material(self.cert, self.key, "other.example.internal")

        other_key = self.directory / "other.key"
        subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt",
                        "ec_paramgen_curve:P-256", "-out", str(other_key)], check=True,
                       capture_output=True, timeout=10)
        with self.assertRaisesRegex(ValueError, "do not match"):
            validated_tls_material(self.cert, other_key, "monitoring.home.arpa")

    def test_rejects_certificate_expiring_within_thirty_days(self):
        short_cert = self.directory / "short.crt"
        subprocess.run(["openssl", "x509", "-req", "-in", str(self.csr), "-CA",
                        str(self.ca_cert), "-CAkey", str(self.ca_key), "-CAcreateserial",
                        "-out", str(short_cert), "-days", "10", "-sha256", "-extfile",
                        str(self.extensions)], check=True, capture_output=True, timeout=20)
        with self.assertRaisesRegex(ValueError, "invalid"):
            validated_tls_material(short_cert, self.key, "monitoring.home.arpa")

    def test_rejects_missing_files_and_invalid_secret_name(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            validated_tls_material(self.directory / "missing.crt", self.key,
                                   "monitoring.home.arpa")
        with self.assertRaises(ValueError):
            secret_manifest("bad_name", "monitoring.home.arpa", self.cert, self.key)


if __name__ == "__main__":
    unittest.main()
