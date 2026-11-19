# SPDX-License-Identifier: MIT
import sqlite3
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from scripts.operations.monitoring.observer import store, worker


class WorkerTest(unittest.TestCase):
    def test_probe_failure_and_recovery_are_persisted_and_delivered_in_order(self):
        class Handler(BaseHTTPRequestHandler):
            code = 503
            def do_GET(self):
                self.send_response(self.code)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        con = sqlite3.connect(":memory:")
        try:
            store.initialize(con)
            url = "http://127.0.0.1:" + str(server.server_port) + "/health"
            self.assertEqual(worker.sample(con, "cluster-a", url, 1, 1)[1], "firing")
            Handler.code = 200
            self.assertEqual(worker.sample(con, "cluster-a", url, 1, 1)[1], "resolved")
            sent = []
            def accept(name, state, reason):
                sent.append((name, state, reason))
                return "accepted"
            worker.deliver_one(con, accept)
            worker.deliver_one(con, accept)
            self.assertEqual(sent, [("cluster-a", "firing", "unexpected-http-status"),
                                    ("cluster-a", "resolved", "expected-http-status")])
        finally:
            con.close()
            server.shutdown()
            thread.join()
            server.server_close()

    def test_delivery_failure_never_loses_event_or_exposes_exception(self):
        con = sqlite3.connect(":memory:")
        store.initialize(con)
        store.observe(con, "cluster-a", "failed", 1, 1)
        def fail(name, status, reason):
            raise RuntimeError("secret")
        self.assertEqual(worker.deliver_one(con, fail), "unknown")
        self.assertEqual(con.execute("SELECT outcome FROM observer_delivery_diagnostics").fetchone(), ("unknown",))
        self.assertNotIn("secret", str(con.execute("SELECT * FROM observer_delivery_diagnostics").fetchall()))
        self.assertEqual(store.pending(con)[1:], ("cluster-a", "firing", None))
        self.assertEqual(worker.deliver_one(con, lambda name, status, reason: "accepted"), "accepted")
        self.assertEqual(worker.deliver_one(con, lambda name, status, reason: "accepted"), "empty")
        con.close()


if __name__ == "__main__":
    unittest.main()
