# SPDX-License-Identifier: MIT
"""Pin the offline monitoring E2E path to the same chart as repository mode."""
from io import BytesIO
from pathlib import Path
import tarfile
import tempfile
import unittest

from tests.e2e.monitoring_operator import validate_monitoring_chart


class MonitoringOperatorChartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.archive = Path(self.temporary.name) / "chart.tgz"

    def write_archive(self, metadata):
        encoded = metadata.encode("utf-8")
        with tarfile.open(self.archive, mode="w:gz") as archive:
            member = tarfile.TarInfo("kube-prometheus-stack/Chart.yaml")
            member.size = len(encoded)
            archive.addfile(member, BytesIO(encoded))

    def test_accepts_the_pinned_chart_name_and_version(self):
        self.write_archive("apiVersion: v2\nname: kube-prometheus-stack\nversion: 92.22.0\n")

        self.assertEqual(validate_monitoring_chart(self.archive), str(self.archive.resolve()))

    def test_rejects_another_chart_version(self):
        self.write_archive("apiVersion: v2\nname: kube-prometheus-stack\nversion: 93.3.0\n")

        with self.assertRaisesRegex(ValueError, "92.22.0"):
            validate_monitoring_chart(self.archive)

    def test_rejects_a_different_chart_name(self):
        self.write_archive("apiVersion: v2\nname: other-chart\nversion: 92.22.0\n")

        with self.assertRaisesRegex(ValueError, "92.22.0"):
            validate_monitoring_chart(self.archive)

    def test_rejects_invalid_archive(self):
        self.archive.write_bytes(b"not a gzip tar archive")

        with self.assertRaisesRegex(ValueError, "invalid local monitoring chart archive"):
            validate_monitoring_chart(self.archive)

    def test_requires_an_absolute_existing_path(self):
        with self.assertRaisesRegex(ValueError, "absolute path"):
            validate_monitoring_chart(Path("relative-chart.tgz"))


if __name__ == "__main__":
    unittest.main()
