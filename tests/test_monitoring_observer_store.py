# SPDX-License-Identifier: MIT
import sqlite3
import tempfile
import unittest
from pathlib import Path
from scripts.operations.monitoring.observer import store


class ObserverStoreTest(unittest.TestCase):
    def test_restart_preserves_transitions_and_fifo_until_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "observer.sqlite")
            con = sqlite3.connect(path)
            store.initialize(con)
            store.observe(con, "cluster-a", "failed", 2, 2)
            self.assertIsNone(store.pending(con))
            store.observe(con, "cluster-a", "failed", 2, 2)
            first = store.pending(con)
            self.assertEqual(first[1:], ("cluster-a", "firing", None))
            store.acknowledge(con, first[0], "unknown")
            con.close()
            con = sqlite3.connect(path)
            store.observe(con, "cluster-a", "unknown", 2, 2)
            store.observe(con, "cluster-a", "healthy", 2, 2)
            store.observe(con, "cluster-a", "healthy", 2, 2)
            self.assertEqual(store.pending(con), first)
            with self.assertRaises(ValueError):
                store.acknowledge(con, first[0] + 1, "accepted")
            store.acknowledge(con, first[0], "accepted")
            second = store.pending(con)
            self.assertEqual(second[1:], ("cluster-a", "resolved", None))
            store.acknowledge(con, second[0], "accepted")
            self.assertIsNone(store.pending(con))
            con.close()

    def test_invalid_observation_rolls_back(self):
        con = sqlite3.connect(":memory:")
        store.initialize(con)
        with self.assertRaises(ValueError):
            store.observe(con, "cluster-a", "bad", 1, 1)
        self.assertEqual(con.execute("SELECT count(*) FROM observer_state").fetchone()[0], 0)
        self.assertIsNone(store.pending(con))
        con.close()

    def test_does_not_rollback_callers_transaction(self):
        con = sqlite3.connect(":memory:")
        store.initialize(con)
        con.execute("INSERT INTO observer_outbox(name,event) VALUES ('test','firing')")
        with self.assertRaises(ValueError):
            store.observe(con, "test", "healthy", 1, 1)
        self.assertTrue(con.in_transaction)
        self.assertEqual(store.pending(con)[1:], ("test", "firing", None))
        con.rollback()
        con.close()

    def test_failed_outbox_write_also_rolls_back_state(self):
        con = sqlite3.connect(":memory:")
        store.initialize(con)
        con.execute("CREATE TRIGGER reject_outbox BEFORE INSERT ON observer_outbox "
                    "BEGIN SELECT RAISE(ABORT, 'blocked'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store.observe(con, "cluster-a", "failed", 1, 1)
        self.assertEqual(con.execute("SELECT count(*) FROM observer_state").fetchone()[0], 0)
        self.assertIsNone(store.pending(con))
        con.close()

    def test_failed_diagnostic_write_does_not_acknowledge_event(self):
        con = sqlite3.connect(":memory:")
        store.initialize(con)
        store.observe(con, "cluster-a", "failed", 1, 1)
        event = store.pending(con)
        con.execute("CREATE TRIGGER reject_diagnostic BEFORE INSERT ON observer_delivery_diagnostics "
                    "BEGIN SELECT RAISE(ABORT, 'blocked'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store.acknowledge(con, event[0], "accepted")
        self.assertEqual(store.pending(con), event)
        self.assertEqual(con.execute("SELECT count(*) FROM observer_delivery_diagnostics").fetchone()[0], 0)
        con.close()


if __name__ == "__main__":
    unittest.main()
