# SPDX-License-Identifier: MIT
import os
from pathlib import Path
import tempfile
import unittest
from scripts.operations.monitoring.channels.secret_file import read_private


class SecretFileTest(unittest.TestCase):
    def test_regular_file_must_be_private_and_non_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("test-token\n")
            path.chmod(0o600)
            self.assertEqual(read_private(str(path)), "test-token")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "invalid credential file"):
                read_private(str(path))
            path.chmod(0o600)
            link = path.parent / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                read_private(str(link))
            path.write_text("test\nsecond-line")
            with self.assertRaises(ValueError):
                read_private(str(path))


if __name__ == "__main__":
    unittest.main()
