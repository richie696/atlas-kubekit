# SPDX-License-Identifier: MIT
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import sqlite3
import unittest

from scripts.operations import cli
from scripts.operations.monitoring.observer import runtime, status, store


class ObserverStatusTest(unittest.TestCase):
    def test_active_owner_pending_recovery_and_stopped_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            with runtime.database(directory) as connection:
                store.observe(connection, "cluster-a", "failed", 1, 1)
                store.observe(connection, "cluster-a", "healthy", 1, 1)
                data = status.snapshot(directory)
                self.assertEqual(data["owner"], "lock-held")
                self.assertEqual(data["observations"], (("cluster-a", "healthy", "healthy"),))
                self.assertEqual(data["pending"], 2)
                self.assertEqual(data["oldest"], ("cluster-a", "firing"))
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main(["--lang", "zh_CN", "--monitoring-observer-status",
                                               "--observer-state-directory", directory]), 2)
                self.assertIn("待发送通知：2", output.getvalue())
                self.assertIn("不代表", output.getvalue())
            self.assertEqual(status.snapshot(directory)["owner"], "not-running")

    def test_insecure_and_symlinked_database_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with runtime.database(directory):
                pass
            path = Path(directory)
            (path / "observer.sqlite").unlink()
            (path / "observer.sqlite").symlink_to(path / "outside")
            with self.assertRaises(OSError):
                status.snapshot(directory)
            path.chmod(0o755)
            with self.assertRaises(ValueError):
                status.snapshot(directory)

    def test_existing_database_migrates_without_losing_pending_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "observer.lock").touch(mode=0o600)
            with sqlite3.connect(str(path / "observer.sqlite")) as con:
                con.executescript("CREATE TABLE observer_state (name TEXT PRIMARY KEY, status TEXT NOT NULL, "
                                  "last_observation TEXT NOT NULL, failures INTEGER NOT NULL, recoveries INTEGER NOT NULL);"
                                  "CREATE TABLE observer_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, event TEXT NOT NULL);"
                                  "INSERT INTO observer_state VALUES ('cluster-a','firing','failed',1,0);"
                                  "INSERT INTO observer_outbox(name,event) VALUES ('cluster-a','firing');")
            (path / "observer.sqlite").chmod(0o600)
            self.assertEqual(status.snapshot(directory)["pending"], 1)
            self.assertIsNone(status.snapshot(directory)["delivery"])
            with runtime.database(directory) as con:
                self.assertEqual(store.pending(con)[1:], ("cluster-a", "firing", None))
                store.observe(con, "cluster-a", "unknown", 1, 1, reason="tls-error")
                event = store.pending(con)
                store.acknowledge(con, event[0], "retryable")
                data = status.snapshot(directory)
                self.assertEqual(data["probes"][0][1], "tls-error")
                self.assertEqual(data["delivery"][:3], ("cluster-a", "firing", "retryable"))
                self.assertEqual(data["pending"], 1)
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main(["--lang", "zh_CN", "--monitoring-observer-status",
                                               "--observer-state-directory", directory]), 2)
                self.assertIn("TLS 错误", output.getvalue())
                self.assertIn("可重试失败", output.getvalue())
                store.acknowledge(con, event[0], "accepted")
                self.assertEqual(status.snapshot(directory)["pending"], 0)
                self.assertEqual(status.snapshot(directory)["delivery"][2], "accepted")


if __name__ == "__main__":
    unittest.main()
