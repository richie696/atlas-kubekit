# SPDX-License-Identifier: MIT
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from scripts.operations.monitoring.observer.service import notification_text


class ServiceTest(unittest.TestCase):
    def test_message_content_is_localized_and_actionable(self):
        firing = notification_text("zh_CN", "api-vip", "firing")
        self.assertIn("独立探测告警", firing)
        self.assertIn("api-vip", firing)
        self.assertIn("DNS", firing)
        self.assertIn("负载均衡", firing)
        self.assertIn("观察者无法连接", firing)
        self.assertIn("解析结果与预期地址不符", notification_text(
            "zh_CN", "business-vip-dns", "firing", "dns-answer-mismatch"))
        self.assertIn("解析结果符合预期", notification_text(
            "zh_CN", "business-vip-dns", "resolved", "dns-answer-expected"))
        self.assertIn("恢复", notification_text("zh_CN", "api-vip", "resolved"))
        self.assertIn("Independent probe alert", notification_text("en", "api-vip", "firing"))
        for name, event in (("", "firing"), ("api-vip", "unknown")):
            with self.subTest(name=name, event=event), self.assertRaises(ValueError):
                notification_text("zh_CN", name, event)

    def test_real_process_observes_and_stops_cleanly(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        process = None
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                secret = path / "endpoint"
                secret.write_text("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-token")
                secret.chmod(0o600)
                process = subprocess.Popen([sys.executable, "-m", "scripts.operations.monitoring.observer.service",
                    "--state-directory", directory, "--name", "outside", "--url",
                    "http://127.0.0.1:" + str(server.server_port), "--wecom-endpoint-file", str(secret),
                    "--interval", "1", "--recover-after", "1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        with sqlite3.connect((path / "observer.sqlite").as_uri() + "?mode=ro", uri=True) as con:
                            row = con.execute("SELECT status FROM observer_state WHERE name='outside'").fetchone()
                        if row == ("healthy",):
                            break
                    except sqlite3.Error:
                        pass
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail("observer process did not record a healthy probe")
                    time.sleep(0.1)
                process.send_signal(signal.SIGTERM)
                output, errors = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertNotIn(b"test-token", output + errors)
                self.assertEqual(os.stat(path / "observer.sqlite").st_mode & 0o077, 0)
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
