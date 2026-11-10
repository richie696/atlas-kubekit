"""An occupied listener survives; a failed rehearsal stops only its owned child."""
from contextlib import nullcontext
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations import recovery_rehearsal as rehearsal
from scripts.operations.models import OperationError


class RehearsalTests(unittest.TestCase):
    def test_occupied_loopback_listener_is_preserved(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            address = listener.getsockname()
            with self.assertRaises(OperationError):
                rehearsal.reserve_check((address[1],))
            self.assertEqual(listener.getsockname(), address)

    def test_failed_health_check_stops_owned_child_and_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            record = dict(schemaVersion=1, operation='isolated-restore', sourceFingerprint='a' * 64,
                verification=dict(etcdRestored=True, etcdServing=False),
                dependencies=dict(clusterIdentity='test-cluster-uid', etcdVersion='3.6.8'))
            file = root / 'restore.json'
            file.write_text(json.dumps(record))
            file.chmod(0o600)
            original = subprocess.Popen
            children = []
            def launch(*args, **kwargs):
                process = original(*args, **kwargs)
                children.append(process)
                return process
            with patch.object(rehearsal.backup, 'require_root'), \
                 patch.object(rehearsal.backup, 'operation_lock', return_value=nullcontext()), \
                 patch.object(rehearsal.etcd_tools, 'for_backup', return_value='fixed-tool'), \
                 patch.object(rehearsal, 'reserve_check'), \
                 patch.object(rehearsal, 'command', return_value=[sys.executable, '-c', 'import time;time.sleep(60)']), \
                 patch.object(rehearsal, 'client', return_value=[]), \
                 patch.object(rehearsal.common, 'run', side_effect=OperationError('ops.host.command', 'test')), \
                 patch.object(rehearsal.time, 'monotonic', side_effect=[0, 46]), \
                 patch.object(rehearsal.subprocess, 'Popen', side_effect=launch):
                with self.assertRaises(OperationError):
                    rehearsal.run(root, 'a' * 64)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            outcome = json.loads((root / 'rehearsal.json').read_text())
            self.assertEqual(outcome['state'], 'failed')
            self.assertTrue(outcome['stopped'])
            self.assertFalse(json.loads(file.read_text())['verification']['etcdServing'])


if __name__ == '__main__':
    unittest.main()
