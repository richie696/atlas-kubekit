# SPDX-License-Identifier: MIT
import unittest
from scripts.operations.monitoring.channels import email


class EmailReceiverTest(unittest.TestCase):
    def build(self, **changes):
        values = dict(name="test", version="0.28.1", host="smtp.example.com", port=465,
                      sender="sender@example.com", recipient="test@example.com", username="sender@example.com",
                      password_file="/etc/alertmanager/secrets/smtp-password")
        values.update(changes)
        return email.receiver(**values)

    def test_version_specific_tls_and_secret_reference(self):
        old = self.build()["email_configs"][0]
        self.assertFalse(old["require_tls"])
        self.assertEqual(old["smarthost"], "smtp.example.com:465")
        self.assertEqual(old["auth_password_file"], "/etc/alertmanager/secrets/smtp-password")
        self.assertNotIn("auth_password", old)
        self.assertTrue(self.build(port=587)["email_configs"][0]["require_tls"])
        self.assertTrue(self.build(version="0.31.1")["email_configs"][0]["require_tls"])
        self.assertTrue(self.build(version="0.34.2")["email_configs"][0]["require_tls"])
        self.assertTrue(self.build(version="0.35.0")["email_configs"][0]["require_tls"])

    def test_invalid_values_and_unknown_version_fail_closed(self):
        for kwargs in ({"version": "0.29.0"}, {"port": 25}, {"port": True},
                       {"host": "smtp.example.com:465"}, {"password_file": "/a/../password"},
                       {"recipient": "x@example.com\nBCC:attacker@example.com"},
                       {"username": "{{ template }}"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.build(**kwargs)


if __name__ == "__main__":
    unittest.main()
