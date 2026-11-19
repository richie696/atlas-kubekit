# SPDX-License-Identifier: MIT
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from scripts.operations.monitoring.observer import runtime, store


class RuntimeTest(unittest.TestCase):
    def test_lock_and_persistent_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            with runtime.database(directory) as con:
                store.observe(con, "outside", "failed", 1, 1)
                with self.assertRaises(BlockingIOError):
                    with runtime.database(directory):
                        pass
            with runtime.database(directory) as con:
                self.assertEqual(store.pending(con)[1:], ("outside", "firing", None))
            self.assertEqual(os.stat(Path(directory) / "observer.sqlite").st_mode & 0o077, 0)

    def test_insecure_paths_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "observer.sqlite").symlink_to(path / "target")
            with self.assertRaises(OSError):
                with runtime.database(directory):
                    pass
            (path / "observer.sqlite").unlink()
            path.chmod(0o755)
            with self.assertRaises(ValueError):
                with runtime.database(directory):
                    pass

    def test_runtime_keeps_failed_delivery_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as directory, runtime.database(directory) as con:
            outcomes = iter(("failed", "healthy", "healthy"))
            attempts = []
            ready = []
            ticks = []
            def sample(con, name, url, fail_after, recover_after, **_kwargs):
                return store.observe(con, name, next(outcomes), fail_after, recover_after)
            def deliver(name, event, reason):
                attempts.append((event, reason))
                return "unknown" if len(attempts) == 1 else "accepted"
            with patch.object(runtime.worker, "sample", side_effect=sample):
                runtime.run(con, "outside", "http://127.0.0.1", 1, 1, 1, deliver,
                            stop=lambda: len(attempts) == 3, sleep=lambda delay: None,
                            on_ready=lambda: ready.append("ready"), on_tick=lambda: ticks.append("tick"))
            self.assertEqual(attempts, [("firing", None), ("firing", None), ("resolved", None)])
            self.assertEqual(ready, ["ready"])
            self.assertEqual(ticks, ["tick", "tick", "tick"])
            self.assertIsNone(store.pending(con))

    def test_separate_process_restart_retains_pending_event(self):
        with tempfile.TemporaryDirectory() as directory:
            script = ("from scripts.operations.monitoring.observer import runtime,store; import sys\n"
                      "with runtime.database(sys.argv[1]) as con:\n"
                      " if sys.argv[2]=='first':\n"
                      "  store.observe(con,'outside','failed',1,1)\n"
                      "  store.acknowledge(con,store.pending(con)[0],'unknown')\n"
                      " else:\n"
                      "  print(store.pending(con)[2])\n")
            subprocess.run([sys.executable, "-c", script, directory, "first"], check=True, capture_output=True)
            result = subprocess.run([sys.executable, "-c", script, directory, "second"],
                                    check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "firing")

    def test_real_probe_recovery_across_processes(self):
        class Handler(BaseHTTPRequestHandler):
            status = 503
            def do_GET(self):
                self.send_response(self.status)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        script = ("from scripts.operations.monitoring.observer import runtime,worker; import sys\n"
                  "with runtime.database(sys.argv[1]) as con:\n"
                  " worker.sample(con,'outside',sys.argv[2],1,1)\n"
                  " def send(name,event,reason):\n"
                  "  print(event,flush=True); return sys.argv[3]\n"
                  " worker.deliver_one(con,send)\n")
        try:
            with tempfile.TemporaryDirectory() as directory:
                url = "http://127.0.0.1:" + str(server.server_port)
                def run(outcome):
                    return subprocess.run([sys.executable, "-c", script, directory, url, outcome],
                                          check=True, capture_output=True, text=True).stdout.strip()
                self.assertEqual(run("unknown"), "firing")
                Handler.status = 200
                self.assertEqual(run("accepted"), "firing")
                self.assertEqual(run("accepted"), "resolved")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
