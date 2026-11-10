"""Host action layout, private file transitions and fixed runtime packaging."""
from contextlib import ExitStack
import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.operations.host_actions import common,stats
from scripts.operations.adapters.host import bundle
from scripts.operations.models import OperationError

CONFIG='''global
 daemon
frontend kube-apiserver
 bind *:6443
backend kube-apiserver-backend
 server cp1 192.0.2.12:6443 check
frontend stats
    bind *:7000
    mode http
    stats enable
    stats uri /
    stats refresh 10s

# BEGIN ATLAS KUBEKIT MANAGED ROUTES
frontend application
 bind 192.0.2.20:443
 default_backend application-servers
backend application-servers
 server worker 192.0.2.15:30443 check
# END ATLAS KUBEKIT MANAGED ROUTES
'''


class StatsTests(unittest.TestCase):
    def test_renderer_preserves_application_markers_and_is_idempotent(self):
        candidate=stats.render(CONFIG,'192.0.2.0/24')
        self.assertEqual(candidate.split('# BEGIN ATLAS')[1],CONFIG.split('# BEGIN ATLAS')[1])
        self.assertIn('http-request deny if !stats_source_allowed',candidate)
        self.assertEqual(stats.render(candidate,'192.0.2.0/24'),candidate)
        self.assertIn('bind 127.0.0.1:7000',stats.render(candidate,''))

    def test_foreign_layout_and_default_route_are_refused(self):
        for value in (CONFIG+CONFIG,CONFIG.replace('stats enable','http-request allow'),CONFIG.replace('stats uri /','stats uri /foreign')):
            with self.assertRaises(OperationError):stats.render(value,'')
        with self.assertRaises(OperationError):stats.render(CONFIG,'0.0.0.0/0')

    def test_failed_reload_restores_original_and_preserves_permissions(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None) as directory,ExitStack() as stack:
            base=Path(directory).resolve();file=base/'haproxy.cfg';file.write_text(CONFIG);file.chmod(0o600)
            state=base/'state.json';state.write_text(json.dumps(dict(vips=[],routes=[])));state.chmod(0o600)
            stack.enter_context(patch.object(stats,'HAPROXY',file));stack.enter_context(patch.object(stats,'MANAGED_STATE',state))
            stack.enter_context(patch.object(common,'JOURNAL_DIRECTORY',base/'journal'))
            reloads=[]
            def run(args,**kwargs):
                if args[0]=='ip':return 0,'[]'
                if args[:2]==['systemctl','reload']:
                    reloads.append(1)
                    if len(reloads)==1:raise OperationError('ops.host.command','systemctl')
                return 0,''
            stack.enter_context(patch.object(common,'run',run))
            with self.assertRaises(OperationError):stats.apply(dict(cidr='',expectedSha=common.digest(CONFIG.encode())),'a'*64)
            self.assertEqual(file.read_text(),CONFIG);self.assertEqual(file.stat().st_mode&0o777,0o600)
            self.assertEqual(len(reloads),2)
            self.assertEqual(json.loads((base/'journal'/('a'*64)/'state.json').read_text())['state'],'rolled-back')

    def test_stale_host_hash_is_refused_before_reload(self):
        with patch.object(stats,'observe',return_value=dict(configurationSha='a'*64,active=True)),patch.object(common,'run') as run:
            with self.assertRaises(OperationError):stats.apply(dict(cidr='',expectedSha='b'*64),'c'*64)
            run.assert_not_called()


class RuntimeTests(unittest.TestCase):
    def test_bundle_contains_only_own_fixed_runtime_modules(self):
        archive=zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle())))
        self.assertIn('scripts/operations/host_actions/entry.py',archive.namelist())
        self.assertTrue(all(name.startswith('scripts/') and name.endswith(('.py', '.sh', '.json')) for name in archive.namelist()))
        self.assertIn('scripts/operations/assets/stages/setup-k8s-node.sh', archive.namelist())
        self.assertFalse(any('kubeconfig' in name or '.ssh' in name for name in archive.namelist()))


if __name__=='__main__':unittest.main()
