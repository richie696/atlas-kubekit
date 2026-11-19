# SPDX-License-Identifier: MIT
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.operations import cli


class ObserverInstallCommandTest(unittest.TestCase):
    def test_cli_routes_dns_install_to_local_systemd_manager_without_secret_value(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("scripts.operations.commands.observer_install.systemd.install",
                   return_value={"name": "vip-dns", "unit": "/home/observer/unit", "state_directory": "/home/observer/state"}) as install, \
             redirect_stdout(io.StringIO()) as output:
            status = cli.main([
                "--lang", "zh_CN", "--monitoring-observer-install",
                "--observer-name", "vip-dns", "--observer-dns-server", "192.0.2.53",
                "--observer-dns-name", "app.example.test", "--observer-dns-expected", "192.0.2.20",
                "--observer-wecom-endpoint-file", "/home/observer/webhook", "--observer-interval", "20",
                "--output-dir", directory,
            ])
        self.assertEqual(status, 0)
        config = install.call_args.args[0]
        self.assertEqual(config["name"], "vip-dns")
        self.assertEqual(config["dns_server"], "192.0.2.53")
        self.assertEqual(config["dns_expected"], ["192.0.2.20"])
        self.assertIsNone(config["ca_file"])
        self.assertEqual(config["interval"], 20)
        self.assertNotIn("webhook", output.getvalue().lower())

    def test_cli_passes_ca_bundle_path_to_https_observer(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("scripts.operations.commands.observer_install.systemd.install",
                   return_value={"name": "api-vip", "unit": "/home/observer/unit", "state_directory": "/home/observer/state"}) as install, \
             redirect_stdout(io.StringIO()):
            status = cli.main([
                "--lang", "zh_CN", "--monitoring-observer-install", "--observer-name", "api-vip",
                "--observer-url", "https://10.20.1.31:6443/readyz", "--observer-ca-file", "/etc/kubernetes/pki/ca.crt",
                "--observer-wecom-endpoint-file", "/home/observer/webhook", "--output-dir", directory,
            ])
        self.assertEqual(status, 0)
        self.assertEqual(install.call_args.args[0]["ca_file"], "/etc/kubernetes/pki/ca.crt")

    def test_remove_requires_matching_name_and_keeps_state(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch("scripts.operations.commands.observer_install.systemd.remove",
                   return_value={"name": "vip-dns", "state_preserved": True}) as remove, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as error:
            failed = cli.main(["--lang", "zh_CN", "--monitoring-observer-remove", "--observer-name", "vip-dns",
                               "--confirm", "wrong", "--output-dir", directory])
        self.assertEqual(failed, 1)
        remove.assert_not_called()
        self.assertIn("确认", error.getvalue())

        with patch("scripts.operations.commands.observer_install.systemd.remove",
                   return_value={"name": "vip-dns", "state_preserved": True}) as remove, \
             redirect_stdout(io.StringIO()) as output:
            succeeded = cli.main(["--lang", "zh_CN", "--monitoring-observer-remove", "--observer-name", "vip-dns",
                                  "--confirm", "vip-dns"])
        self.assertEqual(succeeded, 0)
        remove.assert_called_once_with("vip-dns")
        self.assertIn("状态数据", output.getvalue())


if __name__ == "__main__":
    unittest.main()
