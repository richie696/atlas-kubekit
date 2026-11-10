"""Stage output stays bounded/private, and timeouts stop only owned jobs."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations.host_actions import private_process
from scripts.operations.models import OperationError


class ProcessTests(unittest.TestCase):
    def test_private_truncated_log_and_redacted_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary).resolve() / 'stage.log'
            with patch.object(private_process, 'MAX_LOG_BYTES', 1024):
                private_process.run([sys.executable, '-c', 'print("x"*4096)'], log, 5)
            self.assertLessEqual(log.stat().st_size, 1024)
            self.assertIn(private_process.TRUNCATED, log.read_bytes())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            failed = log.with_name('failed.log')
            with self.assertRaises(OperationError) as caught:
                private_process.run([sys.executable, '-c', 'print("PRIVATE-VALUE");raise SystemExit(7)'], failed, 5)
            self.assertNotIn('PRIVATE-VALUE', str(caught.exception))
            self.assertIn('PRIVATE-VALUE', failed.read_text())

    def test_timeout_leaves_an_unrelated_process_running(self):
        original = subprocess.Popen
        unrelated = original([sys.executable, '-c', 'import time;time.sleep(60)'], stdout=subprocess.DEVNULL)
        children = []
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            children.append(process)
            return process
        try:
            with tempfile.TemporaryDirectory() as temporary, \
                    patch.object(private_process.subprocess, 'Popen', side_effect=launch):
                with self.assertRaises(OperationError):
                    private_process.run([sys.executable, '-c', 'import time;time.sleep(60)'],
                                        Path(temporary).resolve() / 'timeout.log', 0.05)
            self.assertIsNotNone(children[0].poll())
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
