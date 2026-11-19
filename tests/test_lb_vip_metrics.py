# SPDX-License-Identifier: MIT
import json
import importlib.util
from pathlib import Path
import subprocess
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/stages/lb-vip-metrics.py"
SPEC = importlib.util.spec_from_file_location("lb_vip_metrics", MODULE_PATH)
lb_vip_metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lb_vip_metrics)


class LoadBalancerVipMetricsTest(unittest.TestCase):
    def test_renders_active_owner_and_backup_metrics(self):
        active = lb_vip_metrics.render_metrics("lb1", ["10.20.1.31", "10.20.1.20"], "enp6s18", True,
                                              {"10.20.1.31", "10.20.1.20"})
        backup = lb_vip_metrics.render_metrics("lb2", ["10.20.1.31", "10.20.1.20"], "enp6s18", True, set())
        self.assertIn('atlas_keepalived_active{lb="lb1",vip="10.20.1.31",interface="enp6s18"} 1', active)
        self.assertIn('atlas_keepalived_vip_owner{lb="lb1",vip="10.20.1.31",interface="enp6s18"} 1', active)
        self.assertIn('atlas_keepalived_vip_owner{lb="lb2",vip="10.20.1.31",interface="enp6s18"} 0', backup)
        self.assertIn('vip="10.20.1.20"', active)

    def test_collects_real_command_contract_and_detects_vip(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if command[:3] == ["systemctl", "is-active", "--quiet"]:
                return subprocess.CompletedProcess(command, 0)
            body = json.dumps([{"addr_info": [{"family": "inet", "local": "10.20.1.31"}]}])
            return subprocess.CompletedProcess(command, 0, stdout=body)

        output = lb_vip_metrics.collect_metrics("lb1", ["10.20.1.31", "10.20.1.20"], "enp6s18", run)
        self.assertIn("atlas_keepalived_active{", output)
        self.assertIn("atlas_keepalived_vip_owner{", output)
        self.assertIn('atlas_keepalived_vip_owner{lb="lb1",vip="10.20.1.31",interface="enp6s18"} 1', output)
        self.assertEqual(calls[0], ["systemctl", "is-active", "--quiet", "keepalived"])
        self.assertEqual(calls[1], ["ip", "-j", "-4", "addr", "show", "dev", "enp6s18"])

    def test_inactive_keepalived_and_missing_vip_are_reported_as_zero(self):
        def run(command, **kwargs):
            if command[0] == "systemctl":
                return subprocess.CompletedProcess(command, 3)
            return subprocess.CompletedProcess(command, 0, stdout='[{"addr_info": []}]')

        output = lb_vip_metrics.collect_metrics("lb2", ["10.20.1.31"], "enp6s18", run)
        self.assertIn("atlas_keepalived_active{", output)
        self.assertIn("} 0\n", output)
        self.assertTrue(output.endswith("} 0\n"))

    def test_interface_failure_fails_closed(self):
        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, stdout="")

        with self.assertRaises(RuntimeError):
            lb_vip_metrics.collect_metrics("lb1", ["10.20.1.31"], "enp6s18", run)

    def test_rejects_unsafe_identity_values(self):
        for values in (
            ("worker", "10.20.1.31", "enp6s18"),
            ("lb1", "127.0.0.1", "enp6s18"),
            ("lb1", "10.20.1.31", "enp6s18\" bad"),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                lb_vip_metrics.validate_identity(*values)

    def test_rejects_empty_or_excess_vip_list(self):
        for vips in ([], [f"10.20.1.{index}" for index in range(1, 18)]):
            with self.subTest(vips=len(vips)), self.assertRaises(ValueError):
                lb_vip_metrics.render_metrics("lb1", vips, "enp6s18", True, set())


if __name__ == "__main__":
    unittest.main()
