# SPDX-License-Identifier: MIT
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from urllib.error import URLError
from unittest.mock import patch

from scripts.operations.monitoring.observer.probe import probe, validate_ca_file, validate_url


class ObserverProbeTest(unittest.TestCase):
    def test_invalid_endpoint_and_policy_fail_closed(self):
        for url in ("http://192.0.2.1/ready", "https://user:pass@example.com/", "https://example.com/?token=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)
        with self.assertRaises(ValueError):
            probe("http://127.0.0.1:1/ready", timeout=True)

    def test_tls_error_is_unknown_and_connection_failure_is_observer_local(self):
        class Opener:
            def __init__(self, reason):
                self.reason = reason
            def open(self, request, timeout):
                raise URLError(self.reason)
        self.assertEqual(probe("https://example.com/ready", opener=Opener(ssl.SSLError("secret")))["state"], "unknown")
        self.assertEqual(probe("https://example.com/ready", opener=Opener(TimeoutError()))["reason"], "unreachable-from-observer")

    def test_ca_bundle_must_be_a_regular_absolute_pem_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster-ca.pem"
            path.write_text("test CA")
            with patch("scripts.operations.monitoring.observer.probe.ssl.create_default_context",
                       return_value=type("Context", (), {"get_ca_certs": lambda _self: [object()]})()):
                self.assertEqual(validate_ca_file(str(path)), str(path))
                with self.assertRaises(ValueError):
                    validate_ca_file("relative-ca.pem")
                with self.assertRaises(ValueError):
                    validate_ca_file(str(Path(directory) / "missing.pem"))
                link = Path(directory) / "ca-link.pem"
                link.symlink_to(path)
                with self.assertRaises(ValueError):
                    validate_ca_file(str(link))

    def test_ca_bundle_is_loaded_for_tls_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster-ca.pem"
            path.write_text("test CA")
            class Opener:
                def open(self, request, timeout):
                    return type("Response", (), {"status": 200, "__enter__": lambda self: self,
                                                  "__exit__": lambda *_args: None})()
            context = type("Context", (), {"get_ca_certs": lambda _self: [object()]})()
            with patch("scripts.operations.monitoring.observer.probe.ssl.create_default_context",
                       return_value=context) as create_context:
                self.assertEqual(probe("https://example.com/ready", opener=Opener(), ca_file=str(path))["state"],
                                 "healthy")
            self.assertEqual(create_context.call_args.kwargs, {"cafile": str(path)})

    def test_real_http_statuses_are_observed_without_following_redirects(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response({"/ok": 200, "/fail": 503, "/auth": 403, "/redirect": 302}.get(self.path, 404))
                if self.path == "/redirect":
                    self.send_header("Location", "/ok")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:" + str(server.server_port)
            self.assertEqual(probe(base + "/ok")["state"], "healthy")
            self.assertEqual(probe(base + "/fail")["state"], "failed")
            self.assertEqual(probe(base + "/auth")["state"], "unknown")
            self.assertEqual(probe(base + "/redirect")["state"], "unknown")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
