# SPDX-License-Identifier: MIT
import os
from pathlib import Path
import stat
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.operations.monitoring.observer import systemd


class ObserverSystemdTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.endpoint = self.home / "webhook endpoint"
        self.endpoint.write_text("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-only")
        self.endpoint.chmod(0o600)
        self.config = {
            "name": "business-dns",
            "state_directory": None,
            "url": None,
            "dns_server": "192.0.2.53",
            "dns_port": 53,
            "dns_name": "app.example.test",
            "dns_expected": ["192.0.2.20", "2001:db8::20"],
            "wecom_endpoint_file": str(self.endpoint),
            "lang": "zh_CN",
            "interval": 30,
            "fail_after": 2,
            "recover_after": 2,
        }
        self.bin = self.home / "bin"
        self.bin.mkdir()
        systemctl = self.bin / "systemctl"
        systemctl.write_text("#!/bin/sh\nexit 0\n")
        systemctl.chmod(0o700)
        for command in ("loginctl", "sudo"):
            executable = self.bin / command
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
        self.path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.bin) + os.pathsep + self.path

    def tearDown(self):
        os.environ["PATH"] = self.path
        self.temp.cleanup()

    @staticmethod
    def success_runner(calls, fail_on=None, linger=True):
        linger_state = [linger]
        def run(argv, **_kwargs):
            call = tuple(argv)
            calls.append(call)
            if argv[0] == "loginctl" and argv[1] == "show-user":
                return SimpleNamespace(returncode=0, stdout="yes\n" if linger_state[0] else "no\n", stderr="")
            if argv[0] == "sudo" and argv[1:] == ["-n", "loginctl", "enable-linger", systemd.pwd.getpwuid(os.geteuid()).pw_name]:
                linger_state[0] = True
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            failed = fail_on and call == fail_on
            return SimpleNamespace(returncode=1 if failed else 0, stdout="", stderr="")
        return run

    def test_rendered_service_is_localized_hardened_and_contains_no_webhook_secret(self):
        config = systemd.normalize(self.config, home=self.home)
        unit = systemd.render_unit(config, python="/opt/python with space", module_root=self.home / "source tree")
        self.assertIn("--dns-name", unit)
        self.assertIn("--dns-expected", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("Type=notify", unit)
        self.assertIn("NotifyAccess=main", unit)
        self.assertIn("WatchdogSec=50s", unit)
        self.assertIn("OnFailure=atlas-kubekit-observer-failure-business-dns.service", unit)
        self.assertIn("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", unit)
        self.assertIn("WorkingDirectory=" + systemd._systemd_escape(self.home / "source tree"), unit)
        self.assertNotIn('WorkingDirectory="', unit)
        self.assertIn("\x20", unit)
        self.assertNotIn("test-only", unit)
        self.assertIn(str(self.home / ".local/state/atlas-kubekit/observer/business-dns"), unit)
        failure_unit = systemd.render_failure_unit(config, python="/usr/bin/python3", module_root=self.home / "source")
        self.assertIn("Type=oneshot", failure_unit)
        self.assertIn("failure_notify", failure_unit)
        self.assertIn(systemd._systemd_quote(self.endpoint), failure_unit)
        self.assertNotIn("test-only", failure_unit)

    def test_install_writes_private_user_unit_and_starts_service(self):
        calls = []
        result = systemd.install(self.config, home=self.home, python="/usr/bin/python3",
                                 module_root=self.home / "source", runner=self.success_runner(calls, linger=False))
        unit_path = Path(result["unit"])
        failure_path = Path(result["failure_unit"])
        self.assertTrue(unit_path.is_file())
        self.assertTrue(failure_path.is_file())
        self.assertEqual(stat.S_IMODE(unit_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(failure_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(result["state_directory"]).stat().st_mode), 0o700)
        self.assertIn(("systemctl", "--user", "enable", "--now", unit_path.name), calls)
        self.assertIn(("sudo", "-n", "loginctl", "enable-linger", systemd.pwd.getpwuid(os.geteuid()).pw_name), calls)
        self.assertEqual(result["name"], "business-dns")

    def test_already_enabled_linger_does_not_request_privilege(self):
        calls = []
        systemd.install(self.config, home=self.home, runner=self.success_runner(calls, linger=True))
        self.assertFalse(any(call[:1] == ("sudo",) for call in calls))

    def test_linger_failure_stops_before_writing_observer_unit(self):
        calls = []
        runner = self.success_runner(calls, linger=False)
        runner_with_linger_failure = lambda argv, **kwargs: (
            SimpleNamespace(returncode=1, stdout="", stderr="")
            if argv[:3] == ["sudo", "-n", "loginctl"] else runner(argv, **kwargs)
        )
        with self.assertRaisesRegex(ValueError, "sudo loginctl enable-linger"):
            systemd.install(self.config, home=self.home, runner=runner_with_linger_failure)
        self.assertFalse((self.home / ".config/systemd/user/atlas-kubekit-observer-business-dns.service").exists())
        self.assertFalse(any(call[0] == "systemctl" for call in calls))

    def test_https_service_receives_optional_ca_bundle_and_dns_rejects_it(self):
        ca_file = self.home / "cluster-ca.pem"
        ca_file.write_text("public CA material")
        config = dict(self.config, url="https://10.20.1.31:6443/readyz", dns_server=None,
                      dns_name=None, dns_expected=None, ca_file=str(ca_file))
        with patch("scripts.operations.monitoring.observer.systemd.probe.validate_ca_file",
                   return_value=str(ca_file)):
            normalized = systemd.normalize(config, home=self.home)
        unit = systemd.render_unit(normalized, module_root=self.home / "source")
        self.assertIn("--ca-file", unit)
        self.assertIn(str(ca_file), unit)
        with self.assertRaises(ValueError):
            systemd.normalize(dict(self.config, ca_file=str(ca_file)), home=self.home)

    def test_failed_update_restores_the_previous_unit(self):
        config = systemd.normalize(self.config, home=self.home)
        unit_directory = self.home / ".config/systemd/user"
        unit_directory.mkdir(parents=True, mode=0o700)
        unit_path = unit_directory / "atlas-kubekit-observer-business-dns.service"
        failure_path = unit_directory / "atlas-kubekit-observer-failure-business-dns.service"
        previous_failure = "# Atlas KubeKit managed independent observer failure notifier\nold\n"
        failure_path.write_text(previous_failure)
        failure_path.chmod(0o600)
        previous = systemd.render_unit(config, python="/old/python", module_root=self.home / "old-source")
        unit_path.write_text(previous)
        unit_path.chmod(0o600)
        calls = []
        runner = self.success_runner(calls, fail_on=("systemctl", "--user", "enable", "--now", unit_path.name))
        with self.assertRaises(ValueError):
            systemd.install(self.config, home=self.home, python="/new/python",
                             module_root=self.home / "new-source", runner=runner)
        self.assertEqual(unit_path.read_text(), previous)
        self.assertEqual(failure_path.read_text(), previous_failure)
        self.assertIn(("systemctl", "--user", "daemon-reload"), calls)

    def test_foreign_unit_is_never_overwritten_or_removed(self):
        unit_directory = self.home / ".config/systemd/user"
        unit_directory.mkdir(parents=True, mode=0o700)
        unit_path = unit_directory / "atlas-kubekit-observer-business-dns.service"
        failure_path = unit_directory / "atlas-kubekit-observer-failure-business-dns.service"
        failure_path.write_text("[Service]\nExecStart=/foreign/program\n")
        failure_path.chmod(0o600)
        unit_path.write_text("[Service]\nExecStart=/foreign/program\n")
        unit_path.chmod(0o600)
        original = unit_path.read_bytes()
        original_failure = failure_path.read_bytes()
        calls = []
        with self.assertRaises(ValueError):
            systemd.install(self.config, home=self.home, runner=self.success_runner(calls))
        with self.assertRaises(ValueError):
            systemd.remove("business-dns", home=self.home, runner=self.success_runner(calls))
        self.assertEqual(unit_path.read_bytes(), original)
        self.assertEqual(failure_path.read_bytes(), original_failure)
        self.assertNotIn(("systemctl", "--user", "daemon-reload"), calls)
        self.assertFalse(any(call[:3] == ("systemctl", "--user", "enable") for call in calls))

    def test_foreign_failure_handler_prevents_partial_removal(self):
        calls = []
        result = systemd.install(self.config, home=self.home, runner=self.success_runner(calls))
        failure_path = Path(result["failure_unit"])
        failure_path.write_text("[Service]\nExecStart=/foreign/program\n")
        original = failure_path.read_bytes()
        calls.clear()
        with self.assertRaises(ValueError):
            systemd.remove("business-dns", home=self.home, runner=self.success_runner(calls))
        self.assertTrue(Path(result["unit"]).exists())
        self.assertEqual(failure_path.read_bytes(), original)
        self.assertFalse(any(call[:3] == ("systemctl", "--user", "disable") for call in calls))

    def test_remove_stops_only_managed_unit_and_preserves_observer_state(self):
        calls = []
        result = systemd.install(self.config, home=self.home, runner=self.success_runner(calls))
        state_directory = Path(result["state_directory"])
        failure_path = Path(result["failure_unit"])
        (state_directory / "observer.sqlite").write_text("state")
        removed = systemd.remove("business-dns", home=self.home, runner=self.success_runner(calls))
        self.assertTrue(removed["state_preserved"])
        self.assertFalse(Path(result["unit"]).exists())
        self.assertFalse(failure_path.exists())
        self.assertEqual((state_directory / "observer.sqlite").read_text(), "state")
        self.assertIn(("systemctl", "--user", "disable", "--now", Path(result["unit"]).name), calls)
        self.assertIn(("systemctl", "--user", "stop", failure_path.name), calls)


if __name__ == "__main__":
    unittest.main()
