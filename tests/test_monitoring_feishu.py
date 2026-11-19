# SPDX-License-Identifier: MIT
import base64
import hashlib
import hmac
import json
import unittest
from urllib.error import URLError

from scripts.operations.monitoring.channels import feishu


class FeishuContractTest(unittest.TestCase):
    def test_text_and_documented_signature(self):
        result = feishu.text_payload("告警恢复", timestamp=1599360473, secret="demo")
        expected = base64.b64encode(hmac.new(b"1599360473\ndemo", b"", hashlib.sha256).digest()).decode()
        self.assertEqual(result["sign"], expected)
        self.assertEqual(result["timestamp"], "1599360473")
        self.assertEqual(result["content"]["text"], "告警恢复")

    def test_invalid_or_oversize_request(self):
        for args in (("", None, None), ("x", 10, None), ("x", 0, "secret"), ("测" * 10000, None, None)):
            with self.subTest(args=args[:2]), self.assertRaises(ValueError):
                feishu.text_payload(*args)

    def test_http_success_requires_business_success(self):
        self.assertEqual(feishu.classify(200, b'{"code":0}'), "accepted")
        self.assertEqual(feishu.classify(200, b'{"code":19021}'), "rejected")
        self.assertEqual(feishu.classify(200, b'{"code":11232}'), "retryable")
        self.assertEqual(feishu.classify(200, b'{"code":false}'), "unknown")
        self.assertEqual(feishu.classify(200, b'{'), "unknown")
        self.assertEqual(feishu.classify(None, b''), "unknown")
        self.assertEqual(feishu.classify(500, b''), "unknown")
        self.assertEqual(feishu.classify(429, b''), "retryable")

    def test_transport_checks_endpoint_and_redacts_network_failure(self):
        class Transport:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                raise URLError("sensitive destination")

        transport = Transport()
        endpoint = "https://open.feishu.cn/open-apis/bot/v2/hook/example-key"
        self.assertEqual(feishu.deliver(endpoint, feishu.text_payload("firing"), transport), "unknown")
        self.assertEqual(transport.timeout, 10)
        self.assertEqual(transport.request.get_method(), "POST")
        for invalid in ("http://open.feishu.cn/open-apis/bot/v2/hook/key",
                        "https://open.feishu.cn.evil.test/open-apis/bot/v2/hook/key",
                        "https://open.feishu.cn/open-apis/bot/v2/hook/key?x=y"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                feishu.deliver(invalid, feishu.text_payload("firing"), transport)
        with self.assertRaises(ValueError):
            feishu.deliver(endpoint, {"msg_type": "text", "content": {"text": "x"}, "unexpected": "secret"}, transport)

    def test_transport_forwards_payload_and_checks_vendor_response(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return b'{"code":0,"msg":"success"}'

        class Transport:
            def open(self, request, timeout):
                self.body = json.loads(request.data)
                return Response()

        transport = Transport()
        payload = feishu.text_payload("恢复", timestamp=1599360473, secret="demo")
        outcome = feishu.deliver("https://open.feishu.cn/open-apis/bot/v2/hook/example-key", payload, transport)
        self.assertEqual(outcome, "accepted")
        self.assertEqual(transport.body, payload)


if __name__ == "__main__":
    unittest.main()
