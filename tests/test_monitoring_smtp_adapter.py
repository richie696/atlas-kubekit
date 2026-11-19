import unittest
from email import message_from_bytes
from email.policy import default

from scripts.operations.monitoring.channels import alertmanager, smtp


class SMTPAdapterTests(unittest.TestCase):
    def notification(self, status="firing"):
        return alertmanager.Notification("atlas-email", "告警内容\n相关日志\npassword=[已脱敏]", status)

    def settings(self, port=465):
        return {"host": "smtp.example.com", "port": port, "sender": "alerts@example.com",
                "recipient": "ops@example.com", "username": "alerts@example.com", "language": "zh_CN"}

    def test_render_produces_chinese_html_card_and_escapes_content(self):
        notification = alertmanager.Notification("atlas-email", "<script>alert(1)</script>\n告警详情", "firing")
        subject, body = smtp.render(notification, notification.text, self.settings())
        self.assertIn("告警触发", subject)
        self.assertIn("#c94f3d", body)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertIn("<br>", body)

    def test_resolved_uses_recovery_style(self):
        subject, body = smtp.render(self.notification("resolved"), "恢复", self.settings())
        self.assertIn("告警恢复", subject)
        self.assertIn("#67a83d", body)

    def test_implicit_tls_sends_html_alternative_without_exposing_password(self):
        calls = []

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def login(self, username, password):
                calls.append(("login", username, password))

            def send_message(self, message):
                calls.append(("send", message.as_bytes()))

        def factory(*args, **kwargs):
            calls.append(("connect", args, kwargs))
            return Client()

        result = smtp.deliver(self.notification(), "告警内容", self.settings(), "secret-value", factory,
                              object())
        self.assertEqual(result, "accepted")
        self.assertEqual(calls[0][0], "connect")
        self.assertEqual(calls[1], ("login", "alerts@example.com", "secret-value"))
        parsed = message_from_bytes(calls[2][1], policy=default)
        self.assertEqual(parsed.get_content_type(), "text/html")
        self.assertNotIn("secret-value", calls[2][1].decode("utf-8"))

    def test_submission_failure_is_retryable_and_starttls_is_verified(self):
        calls = []

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def ehlo(self):
                calls.append("ehlo")

            def starttls(self, context):
                calls.append(("starttls", context))

            def login(self, *_):
                pass

            def send_message(self, _):
                raise OSError("must not be returned")

        result = smtp.deliver(self.notification(), "告警", self.settings(587), "secret",
                              lambda *args, **kwargs: Client(), object())
        self.assertEqual(result, "retryable")
        self.assertEqual(calls[0], "ehlo")
        self.assertEqual(calls[2], "ehlo")
        self.assertEqual(calls[1][0], "starttls")

    def test_invalid_recipient_and_markup_size_are_rejected(self):
        settings = self.settings()
        settings["recipient"] = "ops@example.com\r\nBcc:x@example.com"
        with self.assertRaises(ValueError):
            smtp.render(self.notification(), "告警", settings)
        with self.assertRaises(ValueError):
            smtp.render(self.notification(), "x" * (smtp.MAX_TEXT_BYTES + 1), self.settings())


if __name__ == "__main__":
    unittest.main()
