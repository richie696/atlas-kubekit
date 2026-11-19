# SPDX-License-Identifier: MIT
import subprocess
import unittest
from unittest.mock import patch

from tests.e2e import monitoring_network_policy as e2e


class NamespaceCleanupTest(unittest.TestCase):
    def test_deleted_namespace_is_verified_absent(self):
        responses = [
            subprocess.CompletedProcess([], 0, "deleted", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        with patch.object(e2e, "command", side_effect=responses) as command:
            self.assertIsNone(e2e.delete_namespace(["kubectl"], "atlas-test"))
        self.assertEqual(command.call_count, 2)
        self.assertIn("--wait=true", command.call_args_list[0].args[0])
        self.assertIn("--ignore-not-found", command.call_args_list[1].args[0])

    def test_cleanup_reports_namespace_that_remains(self):
        responses = [
            subprocess.CompletedProcess([], 0, "deleted", ""),
            subprocess.CompletedProcess([], 0, "namespace/atlas-test", ""),
        ]
        with patch.object(e2e, "command", side_effect=responses), \
             patch.object(e2e.time, "monotonic", side_effect=(0, 91)):
            result = e2e.delete_namespace(["kubectl"], "atlas-test")
        self.assertIn("namespace remains after cleanup: atlas-test", result)

    def test_cleanup_reports_failure_to_verify_namespace_state(self):
        responses = [
            subprocess.CompletedProcess([], 0, "deleted", ""),
            subprocess.CompletedProcess([], 1, "", "API unavailable"),
        ]
        with patch.object(e2e, "command", side_effect=responses):
            result = e2e.delete_namespace(["kubectl"], "atlas-test")
        self.assertIn("could not verify namespace removal", result)


if __name__ == "__main__":
    unittest.main()
