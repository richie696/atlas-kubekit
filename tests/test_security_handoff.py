"""A failed independent management probe must restore only toolkit-owned access."""
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.operations.host_actions import common, security
from scripts.operations.models import OperationError


class HandoffTests(unittest.TestCase):
    def fixture(self, stack):
        temporary = stack.enter_context(tempfile.TemporaryDirectory())
        root = Path(temporary).resolve()
        for name in ('BOOTSTRAP', 'ENROLL_UNIT', 'ENROLL_TOKEN', 'PUBLIC_STATE'):
            stack.enter_context(patch.object(security, name, root / name))
        stack.enter_context(patch.object(common, 'JOURNAL_DIRECTORY', root / 'journal'))
        stack.enter_context(patch.object(security.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1001)))
        stack.enter_context(patch.object(security, 'identity_facts', return_value={'hostname': 'cp1'}))
        security.BOOTSTRAP.write_text('ubuntu ALL=(root) NOPASSWD:ALL\n')
        security.BOOTSTRAP.chmod(0o440)
        security.ENROLL_UNIT.write_text('ExecStart=/usr/bin/bash /usr/local/lib/k8s-deploy/01-prepare-node.sh --enroll-server\n')
        security.ENROLL_UNIT.chmod(0o600)
        security.ENROLL_TOKEN.write_bytes(b'private-token')
        security.ENROLL_TOKEN.chmod(0o600)
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return 0, ''

        stack.enter_context(patch.object(common, 'run', run))
        stack.enter_context(redirect_stdout(io.StringIO()))
        values = dict(deploymentUser='ubuntu', retainedUser='atlasops',
                      expectedSha=common.digest(security.BOOTSTRAP.read_bytes()))
        return root, values, calls

    def test_failed_fresh_probe_restores_grant_token_and_enrollment(self):
        with ExitStack() as stack:
            root, values, calls = self.fixture(stack)
            def probe():
                self.assertFalse(security.BOOTSTRAP.exists())
                return False
            with self.assertRaises(OperationError):
                security.finalize(values, 'a' * 64, probe)
            self.assertEqual(security.BOOTSTRAP.read_text(), 'ubuntu ALL=(root) NOPASSWD:ALL\n')
            self.assertEqual(security.BOOTSTRAP.stat().st_mode & 0o777, 0o440)
            self.assertEqual(security.ENROLL_TOKEN.read_bytes(), b'private-token')
            self.assertFalse(security.PUBLIC_STATE.exists())
            self.assertIn(['systemctl', 'start', 'k8s-enroll.service'], calls)
            self.assertEqual(json.loads((root / 'journal' / ('a' * 64) / 'state.json').read_text())['state'], 'rolled-back')

    def test_success_publishes_only_public_identity_after_probe(self):
        with ExitStack() as stack:
            _, values, _ = self.fixture(stack)
            result = security.finalize(values, 'b' * 64, lambda: not security.BOOTSTRAP.exists())
            self.assertTrue(result['committed'])
            self.assertFalse(security.BOOTSTRAP.exists())
            self.assertFalse(security.ENROLL_TOKEN.exists())
            self.assertEqual(json.loads(security.PUBLIC_STATE.read_text()),
                             dict(schemaVersion=1, operationsUser='atlasops', planId='b' * 64))

    def test_foreign_sudoers_and_stale_plan_do_not_disable_service(self):
        with ExitStack() as stack:
            _, values, calls = self.fixture(stack)
            values['expectedSha'] = '0' * 64
            with self.assertRaises(OperationError):
                security.finalize(values, 'c' * 64, lambda: True)
            self.assertFalse(any('disable' in row for row in calls))
            security.BOOTSTRAP.chmod(0o600)
            security.BOOTSTRAP.write_text('ubuntu ALL=(ALL) ALL\n')
            with self.assertRaises(OperationError):
                security.observe({k: values[k] for k in ('deploymentUser', 'retainedUser')})
            self.assertEqual(security.BOOTSTRAP.read_text(), 'ubuntu ALL=(ALL) ALL\n')

    def test_disconnected_probe_restores_grant(self):
        with ExitStack() as stack:
            _, values, _ = self.fixture(stack)
            def disconnected():
                raise BrokenPipeError()
            with self.assertRaises(BrokenPipeError):
                security.finalize(values, 'd' * 64, disconnected)
            self.assertTrue(security.BOOTSTRAP.exists())


if __name__ == '__main__':
    unittest.main()
