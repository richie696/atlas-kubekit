# SPDX-License-Identifier: MIT
import json
import re
import unittest
from unittest.mock import patch

from scripts.operations.monitoring.channels import alertmanager, dispatch, dingtalk, feishu, wecom
from tests.test_monitoring_alertmanager import payload


def large_group():
    item = payload()
    item['alerts'] = []
    for i in range(20):
        item['alerts'].append({'status': 'firing', 'fingerprint': f'{i + 1:x}',
            'startsAt': '2026-11-10T00:00:00Z', 'labels': {'alertname': f'Problem{i}'},
            'annotations': {'description': '故障\\"' * 220}})
    return json.dumps(item, ensure_ascii=False).encode()


class GroupBatchTest(unittest.TestCase):
    def test_large_group_survives_all_three_routes(self):
        body = large_group()
        expected = alertmanager.parse(body).text
        self.assertGreater(len(expected.encode()), 12000)
        for kind, limit in (('wecom', 2048), ('dingtalk', 16000), ('feishu', 20000)):
            sent = []
            def send(_url, message, maximum, _opener):
                self.assertEqual(maximum, limit)
                self.assertLessEqual(len(json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode()), limit)
                sent.append(message['text']['content'])
                return 200, b'{"errcode":0}'
            def send_feishu(_url, message, _opener):
                self.assertLessEqual(len(json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode()), limit)
                self.assertIn('sign', message)
                sent.append(message['content']['text'])
                return 'accepted'
            endpoint = {'wecom': 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test',
                        'dingtalk': 'https://oapi.dingtalk.com/robot/send?access_token=test',
                        'feishu': 'https://open.feishu.cn/open-apis/bot/v2/hook/test'}[kind]
            with self.subTest(kind=kind), patch.object(wecom.common, 'send_json', side_effect=send), \
                    patch.object(feishu, 'deliver', side_effect=send_feishu):
                result = dispatch.dispatch(body, 'team-a', kind, endpoint,
                                           None if kind == 'wecom' else 'secret', now=lambda: 100)
                self.assertEqual(result, 'accepted')
                self.assertGreater(len(sent), 1)
                if kind in ('dingtalk', 'feishu'):
                    self.assertTrue(all(text.startswith('atlas | ') for text in sent))
                    sent = [text[len('atlas | '):] for text in sent]
                self.assertEqual(''.join(re.sub(r'^\[part \d+/\d+\]\n', '', text) for text in sent), expected)

    def test_dingtalk_failure_stops_the_batch(self):
        with patch.object(dingtalk.common, 'send_json', side_effect=[(200, b'{"errcode":0}'), (503, b'')]) as send:
            result = dingtalk.deliver('https://oapi.dingtalk.com/robot/send?access_token=test', '故障' * 10000)
        self.assertEqual(result, 'unknown')
        self.assertEqual(send.call_count, 2)

    def test_feishu_failure_stops_the_batch(self):
        with patch.object(feishu, 'deliver', side_effect=['accepted', 'unknown']) as send:
            result = dispatch.dispatch(large_group(), 'team-a', 'feishu', 'credential', 'secret', now=lambda: 100)
        self.assertEqual(result, 'unknown')
        self.assertEqual(send.call_count, 2)
