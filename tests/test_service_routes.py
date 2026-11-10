"""Missing host Service routes fail before installation, without changing networking."""

from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
from tests.shell import BASH


class ServiceRouteTests(unittest.TestCase):
    def execute(self, mode, cidr="10.113.0.0/16"):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "calls"
            program = """set -euo pipefail
source "$1/scripts/deploy/verification.sh"
mode="$2"; log="$3"; SVC_CIDR="$4"
K8S_NODES=(cp1 w1)
q() { printf '%q' "$1"; }
atlas_t() { printf '%s' "$1"; }
atlas_printf() { printf '%s' "$1"; }
die() { printf '%s\\n' "$*" >&2; exit 33; }
run() { printf '%s %s\\n' "$1" "$2" >> "$log"; [ "$mode" = present ]; }
atlas_deploy_verify_service_routes
"""
            result = subprocess.run(
                [BASH, "-c", program, "test", str(ROOT), mode, str(log), cidr],
                capture_output=True,
                text=True,
            )
            return result, log.read_text().splitlines() if log.exists() else []

    def test_known_route_is_read_only_and_checked_on_all_kubernetes_nodes(self):
        result, calls = self.execute("present")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls,
            [
                "cp1 ip -4 route get 10.113.0.1 >/dev/null",
                "w1 ip -4 route get 10.113.0.1 >/dev/null",
            ],
        )

    def test_missing_route_stops_before_any_subsequent_node_or_mutation(self):
        result, calls = self.execute("absent")
        self.assertEqual(result.returncode, 33)
        self.assertIn("phase02.error.service_route", result.stderr)
        self.assertEqual(len(calls), 1)

    def test_invalid_network_does_not_send_remote_commands(self):
        result, calls = self.execute("present", "invalid")
        self.assertEqual(result.returncode, 33)
        self.assertEqual(calls, [])
