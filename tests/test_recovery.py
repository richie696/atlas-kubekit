"""Recovery filesystem safety; real age and serving-etcd tests are separate."""
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations import backup,recovery,reporting
from scripts.operations.models import OperationError


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None)
        self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name).resolve()
        self.source='a'*64;self.directory=self.base/'backup';self.directory.mkdir(mode=0o700)
        self.identity=self.base/'identity';self.identity.write_text('fixture');self.identity.chmod(0o600)
        self.content=b'127.0.0.1 localhost\n'
        self.manifest=dict(schemaVersion=1,runId='fixture',scope='configuration',sourceFingerprint=self.source,
            coverage=dict(configuration=True,etcd=False,volumes=False,applications=False),
            files=[dict(path='etc/hosts',size=len(self.content),sha256=hashlib.sha256(self.content).hexdigest())])
        self.archive=self.base/'fixture.tar'
        with tarfile.open(self.archive,'w') as archive:
            for name,data in [('payload/etc/hosts',self.content),('manifest.json',json.dumps(self.manifest).encode())]:
                member=tarfile.TarInfo(name);member.size=len(data);archive.addfile(member,io.BytesIO(data))
        cipher=self.directory/'payload.tar.age';cipher.write_bytes(b'fixture-cipher');cipher.chmod(0o600)
        reporting.atomic_json(self.directory/'backup.json',dict(self.manifest,payload=dict(name=cipher.name,size=cipher.stat().st_size,sha256=backup.digest_file(cipher))))
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(backup,'require_root'))
        self.stack.enter_context(patch.object(backup,'LOCK_DIRECTORY',self.base/'locks'))
        self.stack.enter_context(patch.object(backup,'reservation',return_value=None))
        def decrypt(args,**kwargs):Path(args[args.index('--output')+1]).write_bytes(self.archive.read_bytes());return ''
        self.stack.enter_context(patch.object(backup,'run_tool',decrypt))

    def restore(self,path,**kwargs):
        return recovery.restore(self.directory,self.identity,path,expected_source=kwargs.get('source',self.source),confirmation=kwargs.get('confirmation','RESTORE ISOLATED'))

    def test_success_restores_only_private_isolated_files_and_reports_coverage(self):
        target=self.base/'isolated';result=self.restore(target)
        file=target/'configuration/etc/hosts'
        self.assertEqual(file.read_bytes(),self.content);self.assertEqual(file.stat().st_mode&0o777,0o600)
        self.assertTrue(result['verification']['configuration']);self.assertFalse(result['verification']['etcdServing'])
        self.assertFalse(result['verification']['volumes']);self.assertTrue((target/'restore.json').is_file())
        self.assertEqual(list(self.base.glob('.restore-*')),[])

    def test_wrong_source_or_confirmation_never_creates_destination(self):
        for args in ({'source':'b'*64},{'confirmation':'yes'}):
            target=self.base/'refused'
            with self.assertRaises(OperationError):self.restore(target,**args)
            self.assertFalse(target.exists())

    def test_existing_or_production_directory_is_never_overwritten(self):
        target=self.base/'existing';target.mkdir();sentinel=target/'sentinel';sentinel.write_text('preserve')
        for path in (target,'/var/lib/etcd/recovery','/etc/kubernetes/recovery'):
            with self.assertRaises(OperationError):self.restore(path)
        self.assertEqual(sentinel.read_text(),'preserve')

    def test_invalid_ciphertext_has_no_published_or_partial_restore(self):
        (self.directory/'payload.tar.age').write_bytes(b'tampered')
        with self.assertRaises(OperationError):self.restore(self.base/'isolated')
        self.assertFalse((self.base/'isolated').exists());self.assertEqual(list(self.base.glob('.restore-*')),[])


if __name__=='__main__':unittest.main()
