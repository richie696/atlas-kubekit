"""Native summaries share the report contract and never imply broader coverage."""

import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from scripts.operations.native_reports import publish
from tests.shell import BASH


class NativeReportTests(unittest.TestCase):
    def test_native_action_validation_failure_cannot_be_summarized_as_passed(self):
        source = (Path(__file__).resolve().parents[1] / "04-verify-cluster.sh").read_text()
        functions = "\n".join(
            re.search(r"^" + name + r"\(\) \{\n.*?^\}", source, re.M | re.S).group()
            for name in ("execute_selected_action", "http_test")
        )
        program = r"""
ERRORS=0; SKIPS=0
valid_url() { return 0; }
warn() { return 0; }
ask() { case "$1" in
  *390cc833c0a7) printf http://127.0.0.1;;
  *1004e46a7f94) printf 000;;
esac; }
"""
        result = subprocess.run(
            [
                BASH,
                "-c",
                program + functions + '\nexecute_selected_action http_test; printf %s "$ERRORS"',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, "1")
        result = subprocess.run(
            [
                BASH,
                "-c",
                program.replace("valid_url() { return 0; }", "valid_url() { return 1; }")
                + functions
                + '\nexecute_selected_action http_test; printf %s "$ERRORS"',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, "1")
        # Keep an existing command error count; an explicit skip remains a skip.
        result = subprocess.run(
            [
                BASH,
                "-c",
                program
                + functions
                + """
failed() { ERRORS=2; return 1; }
skipped() { SKIPS=1; return 0; }
execute_selected_action failed; printf '%s ' "$ERRORS"
ERRORS=0; execute_selected_action skipped; printf '%s %s' "$ERRORS" "$SKIPS"
""",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, "2 0 1")

    def test_reports_preserve_failure_skip_and_cancellation_without_command_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for result, errors, skips, expected in (
                ("passed", 0, 0, "passed"),
                ("passed", 0, 2, "attention"),
                ("failed", 1, 0, "failed"),
                ("cancelled", 0, 0, "attention"),
            ):
                report = publish("04", "services", result, errors, skips, root, "zh_CN")
                value = json.loads((report / "report.json").read_text())
                self.assertEqual(value["result"], expected)
                self.assertEqual(value["operation"], "native-04")
                self.assertEqual(value["findings"][0]["check"], "phase04.services")
                self.assertEqual((report / "report.json").stat().st_mode & 0o777, 0o600)
                self.assertIn("仅汇总", (report / "report.txt").read_text())
                self.assertNotIn("command", value)
            self.assertEqual(len(list(root.iterdir())), 4)

    def test_invalid_native_protocol_does_not_create_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for args in (
                ("01", "services", "passed", 0, 0),
                ("02", "bad action", "passed", 0, 0),
                ("02", "deployment", "passed", -1, 0),
            ):
                with self.assertRaises(ValueError):
                    publish(*args, root, "en")
            self.assertEqual(list(root.iterdir()), [])
