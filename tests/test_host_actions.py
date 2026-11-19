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

from scripts.operations.host_actions import common,stats,lb_vip_metrics
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
        self.assertIn('frontend prometheus-metrics', candidate)
        self.assertIn('bind *:8405', candidate)
        self.assertIn('acl metrics_source_allowed src 192.0.2.0/24', candidate)
        self.assertIn('http-request use-service prometheus-exporter if { path /metrics }', candidate)
        self.assertIn('use_backend atlas-vip-metrics if { path /vip-metrics }', candidate)
        self.assertIn('server local-vip-metrics 127.0.0.1:8406 check inter 5s fall 2 rise 2', candidate)
        self.assertIn('http-request set-path /metrics', candidate)
        self.assertEqual(stats.render(candidate,'192.0.2.0/24'),candidate)
        self.assertIn('bind 127.0.0.1:7000',stats.render(candidate,''))
        self.assertIn('bind 127.0.0.1:8405',stats.render(candidate,''))

    def test_foreign_layout_and_default_route_are_refused(self):
        for value in (CONFIG+CONFIG,CONFIG.replace('stats enable','http-request allow'),CONFIG.replace('stats uri /','stats uri /foreign'),
                      CONFIG.replace('frontend stats', 'frontend stats\n    bind *:8405')):
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
        self.assertIn('scripts/operations/host_actions/lb_vip_metrics.py', archive.namelist())
        self.assertIn('scripts/operations/assets/stages/lb-vip-metrics.py', archive.namelist())
        self.assertFalse(any('kubeconfig' in name or '.ssh' in name for name in archive.namelist()))


class LbVipMetricsHostActionTest(unittest.TestCase):
    def test_configuration_is_bounded_and_unit_is_loopback_hardened(self):
        value = lb_vip_metrics._validated({"node": "lb1", "interface": "enp6s18",
                                           "vips": ["10.20.1.31"]})
        desired = lb_vip_metrics._desired(value, b"# Managed by Atlas KubeKit\n# Expose local Keepalived state")
        self.assertEqual(set(desired), {lb_vip_metrics.SCRIPT, lb_vip_metrics.ENV, lb_vip_metrics.UNIT})
        self.assertIn(b"ATLAS_LB_VIPS=10.20.1.31", desired[lb_vip_metrics.ENV])
        self.assertIn(lb_vip_metrics.MARKER.encode(), desired[lb_vip_metrics.ENV])
        self.assertIn(lb_vip_metrics.MARKER.encode(), desired[lb_vip_metrics.UNIT])
        self.assertIn(lb_vip_metrics.MARKER.encode(), desired[lb_vip_metrics.SCRIPT])
        self.assertIn(b"User=nobody", desired[lb_vip_metrics.UNIT])
        self.assertIn(b"RestrictAddressFamilies=AF_INET AF_UNIX AF_NETLINK", desired[lb_vip_metrics.UNIT])
        for invalid in (
            {"node": "lb0", "interface": "enp6s18", "vips": ["10.20.1.31"]},
            {"node": "lb1", "interface": "enp6s18;id", "vips": ["10.20.1.31"]},
            {"node": "lb1", "interface": "enp6s18", "vips": ["0.0.0.0"]},
        ):
            with self.assertRaises(OperationError):
                lb_vip_metrics._validated(invalid)

    def test_known_setup_lb_layout_without_markers_is_supported_for_safe_migration(self):
        legacy_script = (Path(__file__).resolve().parents[1] / "scripts/stages/lb-vip-metrics.py").read_bytes()
        legacy_script = legacy_script.replace(
            b"# Managed by Atlas KubeKit: LB VIP metrics collector.\n", b"", 1
        )
        legacy_env = ("ATLAS_LB_NAME=lb1\nATLAS_LB_VIPS=10.20.1.39\n"
                      "ATLAS_LB_INTERFACE=enp6s18\n").encode()
        legacy = {lb_vip_metrics.SCRIPT: legacy_script,
                  lb_vip_metrics.ENV: legacy_env,
                  lb_vip_metrics.UNIT: lb_vip_metrics.LEGACY_UNIT}
        with patch.object(lb_vip_metrics.subprocess, "run", return_value=unittest.mock.Mock(returncode=0)):
            state = lb_vip_metrics._state(legacy)
        self.assertTrue(state["installed"])
        self.assertEqual(state["digest"], lb_vip_metrics.hashlib.sha256(
            b"\0".join(legacy[path] for path in (lb_vip_metrics.SCRIPT,
                                                   lb_vip_metrics.ENV,
                                                   lb_vip_metrics.UNIT))).hexdigest())

        foreign = dict(legacy)
        foreign[lb_vip_metrics.UNIT] = b"[Service]\nExecStart=/bin/false\n"
        with patch.object(lb_vip_metrics.subprocess, "run", return_value=unittest.mock.Mock(returncode=0)):
            with self.assertRaises(OperationError):
                lb_vip_metrics._state(foreign)

    def test_entry_exposes_observe_and_apply_as_fixed_actions(self):
        from scripts.operations.host_actions.entry import ACTIONS
        self.assertIn("lb-vip-metrics-observe", ACTIONS)
        self.assertIn("lb-vip-metrics-apply", ACTIONS)


if __name__=='__main__':unittest.main()
