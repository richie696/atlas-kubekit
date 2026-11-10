"""Enrollment validation and secret lifetime; native join still requires E2E evidence."""
import base64
import copy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.operations.adapters.host import bundle
from scripts.operations.configuration import template
from scripts.operations.lifecycle import join
from scripts.operations.models import OperationError


class JoinTests(unittest.TestCase):
    def setUp(self):
        self.values = dict(configuration=template(), candidate=dict(
            name='w1', address='192.0.2.20', interface='eth0', dataDevice='none'))

    def test_join_refuses_duplicate_inventory_and_unplanned_formatting(self):
        for change in (dict(name='cp1'), dict(address='192.0.2.12'), dict(dataDevice='/dev/sdb'), dict(dataDevice='')):
            values = copy.deepcopy(self.values)
            values['candidate'].update(change)
            with self.assertRaises(OperationError):
                join.parameters(values)
        self.assertEqual(join.parameters(self.values)['candidate']['name'], 'w1')

    def test_failed_join_revokes_minted_token_and_never_publishes_configuration(self):
        environment = Mock()
        target, issuer = Mock(), Mock()
        environment.host.side_effect = [target, issuer]
        target.call.side_effect = [dict(prepared=True), OperationError('ops.host.failed')]
        secret = dict(token='abcdef.' + 'a' * 16, caHash='sha256:' + 'b' * 64)
        issuer.call.return_value = secret
        facts = dict(existing=None, candidate=dict(identity=dict(identityFingerprint='a' * 64)))
        with patch.object(join, 'observe', return_value=facts), \
                patch.object(join.configuration_cluster, 'publish') as publish:
            with self.assertRaises(OperationError):
                join.apply(self.values, environment, lambda step: None, 'a' * 64)
        self.assertEqual(issuer.call.call_args[0][:2], ('join-revoke', dict(id='abcdef')))
        self.assertEqual(secret, {})
        publish.assert_not_called()

    def test_embedded_native_assets_are_available_from_a_zip_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / 'runtime.zip'
            file.write_bytes(base64.b64decode(bundle()))
            code = '''import sys
sys.path.insert(0,sys.argv[1])
from scripts.operations.host_actions.stage_assets import installed
with installed() as root:
 assert (root/'stages/setup-k8s-node.sh').is_file()
 assert (root/'lib/stage-runtime.sh').is_file()
 assert (root/'locales/en.json').is_file()
'''
            process = subprocess.run([sys.executable, '-c', code, str(file)], capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr.decode())


if __name__ == '__main__':
    unittest.main()
