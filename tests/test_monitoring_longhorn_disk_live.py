# SPDX-License-Identifier: MIT
import unittest

from tests.e2e.monitoring_longhorn_disk_live import ready_longhorn_nodes


class LonghornDiskLivePreconditionTest(unittest.TestCase):
    def test_matches_canonical_longhorn_ready_condition_case_insensitively(self):
        items = [
            {"metadata": {"name": "w1"}, "status": {"conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "Schedulable", "status": "True"},
            ]}},
            {"metadata": {"name": "w2"}, "status": {"conditions": [
                {"type": "ready", "status": "True"},
            ]}},
            {"metadata": {"name": "w3"}, "status": {"conditions": [
                {"type": "Ready", "status": "False"},
            ]}},
        ]

        self.assertEqual(ready_longhorn_nodes(items), {"w1", "w2"})


if __name__ == "__main__":
    unittest.main()
