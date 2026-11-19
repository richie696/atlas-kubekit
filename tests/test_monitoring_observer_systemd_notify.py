# SPDX-License-Identifier: MIT
import os
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations.monitoring.observer import systemd_notify


class SystemdNotifyTest(unittest.TestCase):
    def test_sends_notify_datagram_to_filesystem_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "notify.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(path)
            try:
                with patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
                    self.assertTrue(systemd_notify.notify("READY=1"))
                self.assertEqual(server.recv(128), b"READY=1")
            finally:
                server.close()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux abstract Unix sockets are required")
    def test_supports_abstract_socket_addresses(self):
        address = "\x00atlas-observer-notify-test"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(address)
        try:
            with patch.dict(os.environ, {"NOTIFY_SOCKET": "@atlas-observer-notify-test"}):
                self.assertTrue(systemd_notify.notify("WATCHDOG=1"))
            self.assertEqual(server.recv(128), b"WATCHDOG=1")
        finally:
            server.close()

    def test_is_noop_without_systemd_and_rejects_invalid_state(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(systemd_notify.notify("READY=1"))
        for value in ("", "READY=1\x00WATCHDOG=1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                systemd_notify.notify(value)


if __name__ == "__main__":
    unittest.main()
