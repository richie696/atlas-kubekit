"""Diagnostic artifacts contain only explicit public operational projections."""
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations.diagnostics import bundle, views
from scripts.operations.models import OperationError


class Client:
    def cluster_identity(self):
        return 'cluster-uid'

    def get(self, kind):
        if kind == 'event':
            raise OperationError('ops.change.kubernetes')
        return dict(items=[dict(metadata=dict(name='test-resource', namespace='default',
            annotations={'private': 'SENSITIVE-ANNOTATION'}),
            spec=dict(containers=[dict(env=[dict(value='SENSITIVE-ENV')])]),
            status=dict(phase='Running', conditions=[dict(type='Ready', status='True',
                message='SENSITIVE-MESSAGE')]))])


class DiagnosticTests(unittest.TestCase):
    def test_partial_collection_is_marked_unverified_and_artifact_is_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, findings = bundle.publish(Path(temporary).resolve(), 'test-run', '0.3.0-dev', 'now', Client())
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            self.assertEqual(sum(row.status == 'UNVERIFIED' for row in findings), 1)
            manifest = json.loads((path / 'manifest.json').read_text())
            self.assertEqual(manifest['toolVersion'], '0.3.0-dev')
            with tarfile.open(path / 'diagnostics.tar.gz') as archive:
                self.assertEqual(len(archive.getmembers()), len(bundle.KINDS))
                for item in archive.getmembers():
                    raw = archive.extractfile(item).read().decode()
                    self.assertNotIn('SENSITIVE', raw)
                    self.assertEqual(item.mode, 0o600)
                self.assertIsNone(json.load(archive.extractfile('event.json'))['items'])

    def test_event_messages_and_unknown_fields_are_not_copied(self):
        result = views.project('event', dict(metadata=dict(name='event'), message='TOKEN',
            note='TOKEN', reason='FailedScheduling', type='Warning', count=2,
            involvedObject=dict(name='pod', namespace='default')))
        self.assertNotIn('TOKEN', json.dumps(result))
        self.assertEqual(result['reason'], 'FailedScheduling')

    def test_limit_failure_publishes_no_successful_artifact(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(bundle, 'MAX_BYTES', 1):
            root = Path(temporary).resolve()
            with self.assertRaises(OperationError):
                bundle.publish(root, 'run', 'dev', 'now', Client())
            self.assertFalse(list(root.iterdir()))
        with patch.object(views, 'MAX_ITEMS', 1):
            with self.assertRaises(OperationError):
                views.collection('node', dict(items=[{}, {}]))


if __name__ == '__main__':
    unittest.main()
