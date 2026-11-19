# SPDX-License-Identifier: MIT
import json
import http.client
import re
import threading
import unittest
from unittest.mock import patch

from scripts.operations.monitoring.channels import batching, dispatch, relay, wecom
from tests.test_monitoring_alertmanager import payload


class WecomBatchTest(unittest.TestCase):
    def test_multibyte_and_escaped_text_is_preserved_within_limit(self):
        text = ('告警 "node"\\path\n' * 500)
        parts = batching.split_text(text, wecom.text_payload, 2048)
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(re.sub(r'^\[part \d+/\d+\]\n', '', p) for p in parts), text)
        for index, part in enumerate(parts, 1):
            self.assertTrue(part.startswith(f'[part {index}/{len(parts)}]\n'))
            encoded = json.dumps(wecom.text_payload(part), ensure_ascii=False, separators=(',', ':')).encode()
            self.assertLessEqual(len(encoded), 2048)

    def test_delivery_stops_on_unconfirmed_part_and_reports_it(self):
        endpoint = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=testing'
        with patch.object(wecom.common, 'send_json', side_effect=[(200, b'{"errcode":0}'), (503, b'')]) as send:
            self.assertEqual(wecom.deliver(endpoint, '故障' * 3000), 'unknown')
            self.assertEqual(send.call_count, 2)
        with patch.object(wecom.common, 'send_json', return_value=(200, b'{"errcode":0}')) as send:
            self.assertEqual(wecom.deliver(endpoint, '故障' * 3000), 'accepted')
            self.assertGreater(send.call_count, 1)

    def test_short_message_stays_unchanged(self):
        self.assertEqual(batching.split_text('short', wecom.text_payload, 2048), ('short',))

    def test_relay_retries_an_unconfirmed_group(self):
        endpoint = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=testing'
        token = 'a' * 32
        notification = payload()
        notification['alerts'][0]['annotations']['description'] = '故障' * 500
        attempts = []
        def send(_url, body, _limit, _opener):
            attempts.append(body['text']['content'])
            return (503, b'') if len(attempts) == 2 else (200, b'{"errcode":0}')
        def deliver(body):
            return dispatch.dispatch(body, 'team-a', 'wecom', endpoint)
        server = relay.make_server('127.0.0.1', 0, token, deliver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(wecom.common, 'send_json', side_effect=send):
                for expected in (503, 200):
                    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                    connection.request('POST', '/v1/alert', json.dumps(notification),
                                       {'Authorization': 'Bearer ' + token})
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                    connection.close()
            self.assertEqual(attempts[0], attempts[2])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
