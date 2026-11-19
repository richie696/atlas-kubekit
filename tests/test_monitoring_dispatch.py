# SPDX-License-Identifier: MIT
import json
import unittest
from unittest.mock import patch
from scripts.operations.monitoring.channels import dispatch
from tests.test_monitoring_alertmanager import payload


class DispatchTest(unittest.TestCase):
    def test_one_route_delivers_all_alerts_once(self):
        with patch.object(dispatch.wecom, "deliver", return_value="accepted") as send:
            result = dispatch.dispatch(json.dumps(payload()).encode(), "team-a", "wecom", "credential")
        self.assertEqual(result, "accepted")
        self.assertEqual(send.call_count, 1)
        self.assertIn("[告警恢复] 告警规则: Other", send.call_args.args[1])

    def test_mismatched_receiver_never_sends(self):
        with patch.object(dispatch.wecom, "deliver") as send:
            with self.assertRaises(ValueError):
                dispatch.dispatch(json.dumps(payload()).encode(), "team-b", "wecom", "credential")
            send.assert_not_called()

    def test_signatures_use_vendor_time_units_and_unknown_is_preserved(self):
        body = json.dumps(payload()).encode()
        with patch.object(dispatch.dingtalk, "deliver", return_value="unknown") as send:
            self.assertEqual(dispatch.dispatch(body, "team-a", "dingtalk", "credential", "key", now=lambda: 12), "unknown")
            self.assertEqual(send.call_args.args[2:4], (12000, "key"))
        with patch.object(dispatch.feishu, "deliver", return_value="retryable") as send:
            self.assertEqual(dispatch.dispatch(body, "team-a", "feishu", "credential", "key", now=lambda: 12), "retryable")
            self.assertEqual(send.call_args.args[1]["timestamp"], "12")


if __name__ == "__main__":
    unittest.main()
