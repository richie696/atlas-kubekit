"""Digest verification and overlay semantics with no filesystem archive extraction."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.operations import oci
from scripts.operations.models import OperationError


def layer(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, raw, kind in entries:
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            if kind == 'link':
                item.type, item.linkname, item.size = tarfile.SYMTYPE, '/etc/shadow', 0
            archive.addfile(item, io.BytesIO(raw) if item.isfile() else None)
    return output.getvalue()


def image(path, layers, tamper=False):
    blobs = {}
    def add(raw):
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        blobs[oci.blob_name(digest)] = raw
        return dict(digest=digest, size=len(raw))
    config = add(json.dumps(dict(os='linux', architecture='amd64')).encode())
    descriptor = add(json.dumps(dict(schemaVersion=2, config=config,
        layers=[add(raw) for raw in layers])).encode())
    if tamper:
        key = next(iter(blobs))
        blobs[key] += b'tampered'
    with tarfile.open(path, 'w') as archive:
        for name, raw in blobs.items():
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            archive.addfile(item, io.BytesIO(raw))
    return descriptor['digest']


class OciTests(unittest.TestCase):
    def test_only_requested_regular_files_are_returned(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'image.tar'
            digest = image(path, [layer([('usr/local/bin/etcd', b'binary', 'file'),
                                        ('etc/private', b'NEVER-EXTRACT', 'file')])])
            self.assertEqual(oci.selected_files(path, digest, ['usr/local/bin/etcd']),
                             {'usr/local/bin/etcd': b'binary'})
            self.assertEqual([p.name for p in Path(temporary).iterdir()], ['image.tar'])

    def test_corrupted_blob_and_requested_symlink_are_refused(self):
        for tamper, kind in ((True, 'file'), (False, 'link')):
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / 'image.tar'
                digest = image(path, [layer([('usr/local/bin/etcd', b'binary', kind)])], tamper)
                with self.assertRaises(OperationError):
                    oci.selected_files(path, digest, ['usr/local/bin/etcd'])

    def test_whiteouts_and_path_traversal_are_handled_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'image.tar'
            initial = layer([('usr/local/bin/etcd', b'old', 'file')])
            digest = image(path, [initial, layer([('usr/local/bin/.wh.etcd', b'', 'file')])])
            with self.assertRaises(OperationError):
                oci.selected_files(path, digest, ['usr/local/bin/etcd'])
            digest = image(path, [initial, layer([('usr/local/bin/etcd', b'new', 'file')])])
            self.assertEqual(oci.selected_files(path, digest, ['usr/local/bin/etcd'])['usr/local/bin/etcd'], b'new')
            digest = image(path, [initial, layer([('../../escape', b'bad', 'file')])])
            with self.assertRaises(OperationError):
                oci.selected_files(path, digest, ['usr/local/bin/etcd'])


if __name__ == '__main__':
    unittest.main()
