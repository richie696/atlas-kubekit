# SPDX-License-Identifier: MIT
import struct
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from scripts.addons.monitoring_dns_forward import (
    ensure_forward,
    main,
    parse_ipv4_answers,
    validate_resolver,
    validate_zone,
)


class MonitoringDnsForwardTests(unittest.TestCase):
    def setUp(self):
        self.corefile = (
            ".:53 {\n"
            "    errors\n"
            "    forward . /etc/resolv.conf {\n"
            "        max_concurrent 1000\n"
            "    }\n"
            "}\n"
        )

    def test_adds_split_zone_before_root_and_is_idempotent(self):
        configured = ensure_forward(self.corefile, "home.arpa", "10.20.1.45")
        self.assertTrue(configured.startswith(
            "home.arpa:53 {\n    errors\n    cache 30\n    forward . 10.20.1.45\n}\n\n.:53"))
        self.assertEqual(ensure_forward(configured, "home.arpa", "10.20.1.45"), configured)

    def test_refuses_conflicting_zone_without_overwriting_it(self):
        existing = "home.arpa:53 {\n    forward . 10.20.1.46\n}\n\n" + self.corefile
        with self.assertRaisesRegex(ValueError, "different"):
            ensure_forward(existing, "home.arpa", "10.20.1.45")

    def test_rejects_invalid_zone_resolver_and_missing_root_block(self):
        with self.assertRaises(ValueError):
            validate_zone("bad zone")
        for resolver in ("8.8.8.8", "127.0.0.1", "not-an-ip"):
            with self.subTest(resolver=resolver), self.assertRaises(ValueError):
                validate_resolver(resolver)
        with self.assertRaisesRegex(ValueError, "root server block"):
            ensure_forward("home.arpa:53 { forward . 10.20.1.45 }", "office.home.arpa", "10.20.1.45")

    def test_extracts_ipv4_address_from_valid_dns_response(self):
        transaction_id = 0x71A3
        name = b"\x05oauth\x04home\x04arpa\0"
        question = name + struct.pack("!HH", 1, 1)
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, 4) + b"\x0a\x14\x01\xc8"
        packet = struct.pack("!HHHHHH", transaction_id, 0x8180, 1, 1, 0, 0) + question + answer
        self.assertEqual(parse_ipv4_answers(packet, transaction_id), ["10.20.1.200"])

    def test_port_forward_probe_accepts_loopback_as_transport_endpoint(self):
        output = StringIO()
        with patch("scripts.addons.monitoring_dns_forward.lookup_ipv4",
                   return_value=["10.20.1.43"]), redirect_stdout(output):
            self.assertEqual(main(["--probe", "--tcp", "--server", "127.0.0.1",
                                   "--port", "43123", "--name", "grafana.home.arpa",
                                   "--expected-address", "10.20.1.43"]), 0)
        self.assertIn('"server":"127.0.0.1"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
