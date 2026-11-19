# SPDX-License-Identifier: MIT
import unittest

from scripts.operations.monitoring.observer import failure_notify


class ObserverFailureNotificationTest(unittest.TestCase):
    def test_localized_actionable_message(self):
        text = failure_notify.message("zh_CN", "business-vip")
        self.assertIn("独立观察器故障", text)
        self.assertIn("business-vip", text)
        self.assertIn("systemd 日志", text)
        self.assertIn("Independent observer failure", failure_notify.message("en", "business-vip"))
        self.assertIn("獨立觀察器故障", failure_notify.message("zh_TW", "business-vip"))

    def test_retries_unknown_delivery_but_stops_on_accept_or_rejection(self):
        outcomes = iter(("unknown", "retryable", "accepted"))
        delays = []
        self.assertTrue(failure_notify.notify("endpoint", "message",
                                              deliver=lambda *_: next(outcomes), sleep=delays.append))
        self.assertEqual(delays, [1, 2])

        delays.clear()
        self.assertFalse(failure_notify.notify("endpoint", "message",
                                               deliver=lambda *_: "rejected", sleep=delays.append))
        self.assertEqual(delays, [])

    def test_exhausted_delivery_retries_are_bounded(self):
        attempts = []
        delays = []
        result = failure_notify.notify("endpoint", "message",
                                       deliver=lambda *_: attempts.append("send") or "unknown",
                                       sleep=delays.append)
        self.assertFalse(result)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(delays, [1, 2])

    def test_invalid_observer_name_is_rejected(self):
        with self.assertRaises(ValueError):
            failure_notify.message("zh_CN", "../bad")


if __name__ == "__main__":
    unittest.main()
