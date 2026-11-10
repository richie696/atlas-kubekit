"""Recover a killed multi-file publication without overwriting external edits."""

from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.lib import host_lock
from scripts.lb import files

ROOT = Path(__file__).resolve().parents[1]


class RecoveryTests(unittest.TestCase):
    def fixture(self, stack):
        base = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        spec = importlib.util.spec_from_file_location(
            "atlas_lb_test_remote", ROOT / "scripts/lb/remote.py"
        )
        remote = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"atlas_host_lock": host_lock, "atlas_lb_files": files}):
            spec.loader.exec_module(remote)
        root = base / "transactions"
        root.mkdir(mode=0o700)
        directory = root / ("a" * 32)
        directory.mkdir(mode=0o700)
        for field in ("hap", "keep", "state_file"):
            path = base / field
            stack.enter_context(patch.object(remote, field, str(path)))
        stack.enter_context(patch.object(remote, "TRANSACTION_ROOT", str(root)))
        stack.enter_context(patch.object(remote, "LEGACY_TRANSACTION_ROOT", str(base / "legacy")))
        for name, data in {
            "haproxy.old": b"original hap",
            "haproxy.new": b"candidate hap",
            "keepalived.old": b"original keep",
            "keepalived.new": b"candidate keep",
            "state.old": b'  {"routes": []}\n',
            "state.new": b'{"routes": []}',
        }.items():
            path = directory / name
            path.write_bytes(data)
            path.chmod(0o600)
        (directory / "committed").touch()
        (directory / "state.existed").touch()
        Path(remote.hap).write_bytes(b"candidate hap")
        Path(remote.keep).write_bytes(b"original keep")
        Path(remote.state_file).write_bytes(b'  {"routes": []}\n')
        for path in (remote.hap, remote.keep, remote.state_file):
            Path(path).chmod(0o600)
        runner = stack.enter_context(patch.object(remote, "run", return_value=("", 0)))
        return base, directory, remote, runner

    def test_mixed_original_and_candidate_files_are_recovered(self):
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            base, directory, remote, runner = self.fixture(stack)

            def release(owner):
                self.assertTrue(directory.exists())
                self.assertEqual(owner, "a" * 32)

            stack.enter_context(patch.object(remote, "release_operation", side_effect=release))
            remote.complete(dict(token="a" * 32), remote.rollback)
            self.assertEqual(Path(remote.hap).read_bytes(), b"original hap")
            self.assertEqual(Path(remote.keep).read_bytes(), b"original keep")
            self.assertEqual(Path(remote.state_file).read_bytes(), b'  {"routes": []}\n')
            self.assertFalse(directory.exists())
            self.assertEqual(
                sum(
                    call.args[0][:2] == ["systemctl", "is-active"] for call in runner.call_args_list
                ),
                2,
            )

    def test_external_edit_prevents_recovery_and_preserves_journal(self):
        with ExitStack() as stack:
            base, directory, remote, runner = self.fixture(stack)
            Path(remote.keep).write_bytes(b"operator edit")
            with self.assertRaises(RuntimeError):
                remote.rollback(dict(token="a" * 32))
            self.assertEqual(Path(remote.hap).read_bytes(), b"candidate hap")
            self.assertEqual(Path(remote.keep).read_bytes(), b"operator edit")
            self.assertTrue(directory.exists())
            runner.assert_not_called()

    def test_failed_reservation_release_keeps_recovery_evidence(self):
        with ExitStack() as stack, redirect_stdout(io.StringIO()):
            base, directory, remote, runner = self.fixture(stack)
            stack.enter_context(
                patch.object(remote, "release_operation", side_effect=OSError("fixture failure"))
            )
            with self.assertRaises(OSError):
                remote.complete(dict(token="a" * 32), remote.rollback)
            self.assertTrue(directory.exists())
            self.assertEqual(Path(remote.hap).read_bytes(), b"original hap")


if __name__ == "__main__":
    unittest.main()
