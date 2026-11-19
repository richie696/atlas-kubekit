# SPDX-License-Identifier: MIT
import base64
import hashlib
import hmac
import json
import unittest
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from scripts.operations.monitoring.channels import common, dingtalk, wecom


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.body[:size]


class Transport:
    def __init__(self, body=b'{"errcode":0}'):
        self.body = body

    def open(self, request, timeout):
        self.url = request.full_url
        self.body_sent = json.loads(request.data)
        self.timeout = timeout
        return Response(self.body)


class RobotChannelContractTest(unittest.TestCase):
    def test_wecom_sends_text_and_checks_business_result(self):
        transport = Transport()
        result = wecom.deliver("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key", "告警恢复", transport)
        self.assertEqual(result, "accepted")
        self.assertEqual(transport.body_sent, {"msgtype": "text", "text": {"content": "告警恢复"}})
        self.assertEqual(transport.timeout, 10)
        self.assertEqual(wecom.deliver("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key", "x",
                                      Transport(b'{"errcode":40001}')), "rejected")

    def test_dingtalk_signs_timestamp_and_preserves_access_token(self):
        transport = Transport()
        result = dingtalk.deliver("https://oapi.dingtalk.com/robot/send?access_token=token", "故障",
                                  timestamp_ms=1700000000000, secret="example", opener=transport)
        self.assertEqual(result, "accepted")
        query = parse_qs(urlsplit(transport.url).query)
        expected = base64.b64encode(hmac.new(b"example", b"1700000000000\nexample", hashlib.sha256).digest()).decode()
        self.assertEqual(query, {"access_token": ["token"], "timestamp": ["1700000000000"], "sign": [expected]})
        self.assertEqual(transport.body_sent["text"]["content"], "atlas | 故障")

    def test_destinations_and_payload_limits_fail_closed(self):
        for url in ("https://qyapi.weixin.qq.com.evil.test/cgi-bin/webhook/send?key=x",
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x&key=y",
                    "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                wecom.deliver(url, "message", Transport())
        with self.assertRaises(ValueError):
            dingtalk.deliver("https://oapi.dingtalk.com/robot/send?access_token=x&other=y", "x", opener=Transport())
        with self.assertRaises(ValueError):
            common.send_json("https://example.test", wecom.text_payload("中" * 1000), 2048, Transport())

    def test_failed_or_ambiguous_response_never_accepted(self):
        for status, body, expected in ((200, b'{"errcode":false}', "unknown"),
                                       (200, b'{}', "unknown"), (200, b'{', "unknown"),
                                       (429, b'', "retryable"), (503, b'', "unknown"),
                                       (403, b'', "rejected"), (None, b'', "unknown")):
            with self.subTest(status=status, body=body):
                self.assertEqual(common.classify(status, body), expected)
        class Failure:
            def open(self, request, timeout):
                raise URLError("secret must never be exposed")
        self.assertEqual(wecom.deliver("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x", "x", Failure()), "unknown")


if __name__ == "__main__":
    unittest.main()
