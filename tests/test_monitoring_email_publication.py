# SPDX-License-Identifier: MIT
import copy
import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations.adapters.kubernetes import Kubernetes
from scripts.operations.models import OperationError
from scripts.operations.monitoring import email_publication as route, publication
from scripts.operations.monitoring.email_templates import templates
from scripts.operations import cli, configuration
from tests.test_monitoring_change_plan import Cluster


class Client:
    def __init__(self):
        self.manager = {'metadata': {'uid': 'manager', 'resourceVersion': '1'}, 'spec': {
            'version': 'v0.28.1', 'alertmanagerConfigSelector': {'matchLabels': {'release': 'stack'}},
            'alertmanagerConfigMatcherStrategy': {'type': 'OnNamespaceExceptForAlertmanagerNamespace'}}}
        self.secret = {'uid': 'secret', 'resourceVersion': '1'}
        self.current = None
    def get(self, kind, name, namespace, optional=False):
        return copy.deepcopy(self.manager if kind == route.ALERTMANAGER else self.current)
    def secret_metadata(self, namespace, name, key):
        return copy.deepcopy(self.secret)
    def create(self, value):
        self.current = copy.deepcopy(value)
        self.current['metadata'].update(uid='managed', resourceVersion='1')
        return copy.deepcopy(self.current)
    def replace(self, value):
        self.current = copy.deepcopy(value)
        self.current['metadata']['resourceVersion'] = '2'
        return copy.deepcopy(self.current)


class EmailPublicationTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.desired = route.manifest('monitoring', 'stack', '0.28.1', 'smtp.example.test', 465,
            'sender@example.test', 'receiver@example.test', 'sender@example.test', 'smtp-auth', 'password', 'warning', 'zh_CN')
    def capture(self):
        return route.capture(self.client, self.desired, 'stack', '0.28.1', 'smtp-auth', 'password')
    def test_owned_route_and_secret_reference(self):
        expected = self.capture()
        result = route.publish(self.client, self.desired, 'stack', '0.28.1', 'smtp-auth', 'password', expected)
        self.assertEqual(result['delivery'], 'unverified')
        config = self.client.current['spec']['receivers'][0]['emailConfigs'][0]
        self.assertEqual(config['authPassword'], {'name': 'smtp-auth', 'key': 'password'})
        self.assertFalse(config['requireTLS'])
        self.assertIn('告警', config['headers'][0]['value'])
        self.assertIn('{{ .Annotations.description }}', config['html'])
        self.assertIn('bgcolor="#f3f4f6"', config['html'])
        self.assertIn('bgcolor="{{ if eq .Status "firing" }}#c94f3d{{ else }}#67a83d{{ end }}"', config['html'])
        self.assertIn('bgcolor="#348bd4"', config['html'])
        self.assertIn('background-color:#f3f4f6', config['html'])
        self.assertIn('background-color:#348bd4', config['html'])
        self.assertIn('在 Alertmanager 中查看', config['html'])
        self.assertIn('{{ .ExternalURL }}', config['html'])
        for field in ('告警通知', '告警触发', '严重', '摘要', '处理建议', 'Pod', 'PVC', '告警来源'):
            self.assertIn(field, config['html'])
        self.assertIn('{{ .Labels.registered_members }}', config['html'])
        self.assertIn('{{ .Labels.quorum_required }}', config['html'])
        self.assertIn('{{ .Annotations.persistentvolumeclaim }}', config['html'])
        for source in ('.Annotations.summary', '.Annotations.description', '.Labels.pod',
                       '.Labels.persistentvolumeclaim', '.Fingerprint', '.GeneratorURL'):
            self.assertIn(source, config['html'])
        self.assertIn('{{ .CommonAnnotations.summary }}', config['headers'][0]['value'])
        self.assertIn('节点未就绪或监控数据缺失', config['html'])
        self.assertIn('AtlasNodeReadinessOrTelemetryMissing', config['html'])
        self.assertIn('未提供处理说明', config['html'])
        self.assertNotIn('text', config)
        self.assertNotIn('credentials-value', json.dumps(result))
        self.assertTrue(self.client.current['spec']['route']['continue'])
        expected = self.capture()
        route.publish(self.client, self.desired, 'stack', '0.28.1', 'smtp-auth', 'password', expected)
        self.assertEqual(self.client.current['metadata']['uid'], 'managed')
    def test_email_labels_follow_selected_locale(self):
        self.assertIn('Alert notification', templates('en')['html'])
        self.assertIn('警報通知', templates('zh_TW')['html'])
        with self.assertRaises(ValueError):
            templates('unsupported')

    def test_notification_language_is_independent_of_cli_language(self):
        from scripts.operations.arguments import parser_for
        args = parser_for('en').parse_args(['--monitoring-email-recipient', 'ops@example.test'])
        self.assertEqual(args.monitoring_notification_language, 'zh_CN')
        args = parser_for('en').parse_args(['--monitoring-notification-language', 'zh_TW'])
        self.assertEqual(args.monitoring_notification_language, 'zh_TW')
    def test_incompatible_selectors_strategy_and_version_are_rejected(self):
        for change in (lambda s: s.update(alertmanagerConfigSelector=None),
                       lambda s: s.update(alertmanagerConfigMatcherStrategy={'type': 'OnNamespace'}),
                       lambda s: s.update(version='v0.31.1'), lambda s: s.update(image='custom')):
            self.client = Client(); change(self.client.manager['spec'])
            with self.subTest(spec=self.client.manager['spec']), self.assertRaises(OperationError): self.capture()
        self.client = Client()
        self.client.manager['spec']['image'] = 'quay.io/prometheus/alertmanager:v0.28.1'
        self.assertEqual(self.capture()['manager']['uid'], 'manager')
    def test_foreign_resource_and_secret_drift_stop_publication(self):
        expected = self.capture()
        self.client.secret['resourceVersion'] = '2'
        with self.assertRaises(OperationError):
            route.publish(self.client, self.desired, 'stack', '0.28.1', 'smtp-auth', 'password', expected)
        self.assertIsNone(self.client.current)
        self.client.current = {'metadata': {'uid': 'other', 'resourceVersion': '1', 'labels': {}}, 'spec': {}}
        with self.assertRaises(OperationError): self.capture()
    def test_secret_metadata_request_never_returns_secret(self):
        client = Kubernetes('/tmp/kubeconfig')
        with patch.object(client, 'call', return_value='uid\t123\tpresent') as call:
            self.assertEqual(client.secret_metadata('monitoring', 'smtp-auth', 'password'),
                             {'uid': 'uid', 'resourceVersion': '123'})
            self.assertNotIn('.data.password', repr(call.call_args))
        with patch.object(client, 'call', return_value='uid\t123\tmissing'):
            with self.assertRaises(OperationError): client.secret_metadata('monitoring', 'smtp-auth', 'password')

    def test_cli_plan_and_apply_keep_credentials_out_and_delivery_unverified(self):
        class FullClient(Client, Cluster):
            def __init__(self):
                Cluster.__init__(self); Client.__init__(self)
            def get(self, kind, name=None, namespace=None, optional=False):
                if kind == 'configmap': return Cluster.get(self, kind, name, namespace, optional)
                return Client.get(self, kind, name, namespace, optional)
            def create(self, value):
                return Cluster.create(self, value) if value.get('kind') == 'ConfigMap' else Client.create(self, value)
            def replace(self, value):
                return Cluster.replace(self, value) if value.get('kind') == 'ConfigMap' else Client.replace(self, value)
        class Environment:
            def __init__(self): self.client = FullClient()
            def verify(self, config): return configuration.normalize(config), {}
        env = Environment()
        with tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').is_dir() else None) as directory, \
             patch('scripts.operations.commands.changes.environment', return_value=env), \
             patch('scripts.operations.commands.changes.configuration_cluster.read', return_value=configuration.template()), \
             redirect_stdout(io.StringIO()) as output:
            args = ['--lang', 'zh_CN', '--change-plan', '--operation', 'monitoring-email', '--output-dir', directory,
                    '--monitoring-release', 'stack', '--monitoring-alertmanager-name', 'stack',
                    '--monitoring-alertmanager-version', '0.28.1', '--monitoring-smtp-host', 'smtp.example.test',
                    '--monitoring-smtp-port', '465', '--monitoring-email-sender', 'sender@example.test',
                    '--monitoring-email-recipient', 'receiver@example.test', '--monitoring-email-username', 'sender@example.test',
                    '--monitoring-email-secret', 'smtp-auth', '--monitoring-email-secret-key', 'password',
                    '--monitoring-route-matcher', 'team=platform', '--monitoring-group-by', 'cluster',
                    '--monitoring-group-by', 'alertname', '--monitoring-group-wait', '45s']
            self.assertEqual(cli.main(args), 0)
            plan = next((Path(directory) / 'plans').glob('*.json'))
            document = json.loads(plan.read_text())
            self.assertNotIn('credentials-value', plan.read_text())
            self.assertEqual(document['parameters']['routePolicy']['matchers'], [
                {'name': 'team', 'value': 'platform', 'matchType': '='}])
            self.assertEqual(document['parameters']['routePolicy']['groupBy'], ['cluster', 'alertname'])
            self.assertIn('team=platform', output.getvalue())
            self.assertEqual(cli.main(['--lang', 'zh_CN', '--change-apply', '--plan-file', str(plan),
                                       '--confirm', document['id'], '--output-dir', directory]), 2)
            self.assertIn('尚未验证', output.getvalue())
