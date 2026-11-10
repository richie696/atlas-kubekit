"""A prepared LB transaction blocks other writers until explicit owner recovery."""

from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

from scripts.lib import host_lock


class HostReservationTests(unittest.TestCase):
    def test_pending_transaction_survives_rpc_exit_and_refuses_foreign_owner(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(host_lock, "DIRECTORY", Path(temporary).resolve() / "lock"),
            patch.object(
                host_lock, "RESERVATION_DIRECTORY", Path(temporary).resolve() / "persistent"
            ),
        ):
            owner = "a" * 32
            with host_lock.host_operation(owner):
                host_lock.reserve_operation(owner)
            for other in (None, "b" * 32):
                with self.assertRaises(host_lock.HostLockError):
                    with host_lock.host_operation(other):
                        self.fail("foreign writer entered")
            with host_lock.host_operation(owner):
                with self.assertRaises(host_lock.HostLockError):
                    host_lock.release_operation("b" * 32)
                host_lock.release_operation(owner)
            with host_lock.host_operation():
                self.assertIsNone(host_lock.reservation())

    def test_reservation_symlink_or_world_writable_lock_is_never_followed(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(host_lock, "DIRECTORY", Path(temporary).resolve() / "lock"),
            patch.object(
                host_lock, "RESERVATION_DIRECTORY", Path(temporary).resolve() / "persistent"
            ),
        ):
            base = host_lock.directory()
            outside = Path(temporary) / "outside"
            outside.write_text("preserve")
            (base / "reservation.json").symlink_to(outside)
            with self.assertRaises(host_lock.HostLockError):
                with host_lock.host_operation():
                    pass
            self.assertEqual(outside.read_text(), "preserve")
            (base / "reservation.json").unlink()
            (base / "operation.lock").chmod(0o666)
            with self.assertRaises(host_lock.HostLockError):
                with host_lock.host_operation():
                    pass

    def test_reservation_survives_loss_of_runtime_directory(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(host_lock, "DIRECTORY", Path(temporary).resolve() / "run"),
            patch.object(
                host_lock, "RESERVATION_DIRECTORY", Path(temporary).resolve() / "persistent"
            ),
        ):
            owner = "c" * 32
            with host_lock.host_operation(owner):
                host_lock.reserve_operation(owner)
            shutil.rmtree(host_lock.DIRECTORY)
            self.assertEqual(host_lock.reservation(), owner)
            with self.assertRaises(host_lock.HostLockError):
                with host_lock.host_operation():
                    self.fail("reboot must not abandon reservation")
            with host_lock.host_operation(owner):
                host_lock.release_operation(owner)
            self.assertIsNone(host_lock.reservation())


if __name__ == "__main__":
    unittest.main()
