# SPDX-License-Identifier: MIT
import socketserver
import socket
import os
from pathlib import Path
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from scripts.operations.monitoring.observer import dns_probe, runtime, store, worker


class DNSFixture(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.answer = "192.0.2.20"
        self.rcode = 0
        self.malformed = False
        self.answer_owner = "app.example.test"
        self.alias = False
        super().__init__(("127.0.0.1", 0), DNSHandler)


class DNSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        query, sock = self.request
        if self.server.malformed:
            sock.sendto(b"bad", self.client_address)
            return
        txid, _, qd, _, _, _ = struct.unpack("!HHHHHH", query[:12])
        offset = 12
        while query[offset]:
            offset += query[offset] + 1
        question_end = offset + 5
        question = query[12:question_end]
        answers = 0
        answer = b""
        if self.server.answer and self.server.rcode == 0:
            address = socket.inet_aton(self.server.answer)
            if self.server.alias:
                target = "vip.example.test"
                cname = self._encode_name(target)
                alias_rr = (b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 30, len(cname)) + cname)
                target_owner = self._encode_name(target)
                address_rr = target_owner + struct.pack("!HHIH", 1, 1, 30, len(address)) + address
                answer = alias_rr + address_rr
                answers = 2
            else:
                owner = (b"\xc0\x0c" if self.server.answer_owner == "app.example.test"
                         else self._encode_name(self.server.answer_owner))
                answer = owner + struct.pack("!HHIH", 1, 1, 30, len(address)) + address
                answers = 1
        flags = 0x8180 | self.server.rcode
        response = struct.pack("!HHHHHH", txid, flags, qd, answers, 0, 0) + question + answer
        sock.sendto(response, self.client_address)

    @staticmethod
    def _encode_name(name):
        return b"".join(bytes((len(label),)) + label.encode("ascii") for label in name.split(".")) + b"\0"


class ObserverDNSProbeTest(unittest.TestCase):
    def setUp(self):
        self.server = DNSFixture()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.resolver = "127.0.0.1"
        self.port = self.server.server_address[1]
        self.query = lambda: dns_probe.probe(self.resolver, "app.example.test", ["192.0.2.20"],
                                               port=self.port, timeout=0.2)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_validates_explicit_dns_endpoint_and_expected_addresses(self):
        for resolver, name, expected in (
            ("resolver.example.test", "app.example.test", ["192.0.2.20"]),
            ("127.0.0.1", "app.example.test", ["not-an-ip"]),
            ("127.0.0.1", "app.example.test;evil", ["192.0.2.20"]),
            ("127.0.0.1", "münchen.example", ["192.0.2.20"]),
        ):
            with self.subTest(resolver=resolver, name=name), self.assertRaises(ValueError):
                dns_probe.validate(resolver, name, expected)

    def test_real_dns_response_matches_expected_vip_or_reports_failure_reason(self):
        self.assertEqual(self.query(), {"state": "healthy", "reason": "dns-answer-expected"})
        self.server.alias = True
        self.assertEqual(self.query(), {"state": "healthy", "reason": "dns-answer-expected"})
        self.server.alias = False
        self.server.answer_owner = "unrelated.example.test"
        self.assertEqual(self.query(), {"state": "failed", "reason": "dns-answer-mismatch"})
        self.server.answer_owner = "app.example.test"
        self.server.answer = "192.0.2.21"
        self.assertEqual(self.query(), {"state": "failed", "reason": "dns-answer-mismatch"})
        self.server.answer = None
        self.server.rcode = 3
        self.assertEqual(self.query(), {"state": "failed", "reason": "dns-nxdomain"})
        self.server.rcode = 0
        self.server.malformed = True
        self.assertEqual(self.query(), {"state": "unknown", "reason": "dns-invalid-response"})

    def test_real_dns_failure_and_recovery_feed_the_durable_observer_queue(self):
        with tempfile.TemporaryDirectory() as directory, runtime.database(directory) as connection:
            self.server.answer = "192.0.2.21"
            state, event, reason = worker.sample(connection, "business-vip-dns", None, 1, 1,
                                                  probe_fn=self.query)
            self.assertEqual((state.status, event, reason), ("firing", "firing", "dns-answer-mismatch"))
            self.assertEqual(store.pending(connection)[3], "dns-answer-mismatch")
            self.server.answer = "192.0.2.20"
            state, event, reason = worker.sample(connection, "business-vip-dns", None, 1, 1,
                                                  probe_fn=self.query)
            self.assertEqual((state.status, event, reason), ("healthy", "resolved", "dns-answer-expected"))
            events = connection.execute("SELECT event, reason FROM observer_outbox ORDER BY id").fetchall()
            self.assertEqual(events, [("firing", "dns-answer-mismatch"),
                                      ("resolved", "dns-answer-expected")])
            sent = []
            deliver = lambda name, event, reason: (sent.append((name, event, reason)) or "accepted")
            self.assertEqual(worker.deliver_one(connection, deliver), "accepted")
            self.assertEqual(worker.deliver_one(connection, deliver), "accepted")
            self.assertEqual(sent, [("business-vip-dns", "firing", "dns-answer-mismatch"),
                                    ("business-vip-dns", "resolved", "dns-answer-expected")])

    def test_service_cli_runs_the_real_dns_probe_loop(self):
        process = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "wecom-endpoint"
            secret.write_text("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-token")
            secret.chmod(0o600)
            state = root / "state"
            state.mkdir(mode=0o700)
            process = subprocess.Popen([
                sys.executable, "-m", "scripts.operations.monitoring.observer.service",
                "--state-directory", str(state), "--name", "business-dns-cli",
                "--dns-server", "127.0.0.1", "--dns-name", "app.example.test",
                "--dns-port", str(self.port),
                "--dns-expected", "192.0.2.20", "--wecom-endpoint-file", str(secret),
                "--lang", "zh_CN", "--interval", "1", "--recover-after", "1",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    try:
                        with sqlite3.connect((state / "observer.sqlite").as_uri() + "?mode=ro", uri=True) as con:
                            status = con.execute("SELECT status FROM observer_state WHERE name=?",
                                                 ("business-dns-cli",)).fetchone()
                            diagnostic = con.execute("SELECT reason FROM observer_probe_diagnostics WHERE name=?",
                                                     ("business-dns-cli",)).fetchone()
                        if status == ("healthy",):
                            break
                    except sqlite3.Error:
                        pass
                    if process.poll() is not None:
                        output, errors = process.communicate(timeout=2)
                        self.fail("DNS observer process exited: " + (output + errors).decode(errors="replace"))
                    time.sleep(0.1)
                else:
                    self.fail("DNS observer process did not record a healthy DNS probe")
                self.assertEqual(diagnostic, ("dns-answer-expected",))
                process.send_signal(signal.SIGTERM)
                output, errors = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertNotIn(b"test-token", output + errors)
                self.assertEqual(os.stat(state / "observer.sqlite").st_mode & 0o077, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
