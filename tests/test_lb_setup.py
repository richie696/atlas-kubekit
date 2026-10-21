#!/usr/bin/env python3
"""Run installer control-flow tests in a temporary filesystem, without root.

The real shell and embedded Python run unchanged apart from filesystem paths.
APT, systemd and daemon commands are doubles: these tests prove failure handling
and file/state transitions, not daemon syntax, package installation or VRRP HA.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
BASH = next((str(p) for p in (Path('/opt/homebrew/bin/bash'), Path('/usr/bin/bash')) if p.exists()), 'bash')
STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, signal, stat, sys
root = pathlib.Path(os.environ['LB_TEST_ROOT'])
command = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
db = root / 'commands.json'
state = json.loads(db.read_text())
state['calls'].append([command, *args])
def finish(code=0, output=''):
    db.write_text(json.dumps(state))
    if output: print(output)
    raise SystemExit(code)
if command == 'id': finish(output='0')
if command == 'sleep': finish()
if command == 'sysctl':
    if args[0] == '-n': finish(output=state.get('nonlocal_bind', '0'))
    if args[0] == '-w':
        state['nonlocal_bind'] = args[-1].split('=')[-1]
        if state.get('fail') == 'sysctl-write' and state['nonlocal_bind'] == '1': finish(1)
        finish()
    finish(1)
if command == 'ip': finish(output='2: enp6s18 inet 10.20.1.12/24 scope global enp6s18')
if command == 'apt-get':
    for d in ('etc/haproxy', 'etc/keepalived'): (root / d).mkdir(parents=True, exist_ok=True)
    finish()
if command == 'stat':
    path = pathlib.Path(args[-1])
    if not path.exists(): finish(1)
    finish(output='root:root:' + oct(stat.S_IMODE(path.stat().st_mode))[2:])
if command == 'install':
    paths = []; mode = 0o755; directory = False; i = 0
    while i < len(args):
        if args[i] == '-d': directory = True
        elif args[i] in ('-o', '-g', '-m'):
            if args[i] == '-m': mode = int(args[i+1], 8)
            i += 1
        else: paths.append(args[i])
        i += 1
    if state.get('fail') == 'install-state' and paths[-1].endswith('/lb-manager.json'): finish(1)
    if state.get('fail') == 'signal-commit' and paths[-1].endswith('/lb-manager.json'):
        os.kill(os.getppid(), signal.SIGTERM)
        finish()
    if directory:
        for p in paths:
            pathlib.Path(p).mkdir(parents=True, exist_ok=True); os.chmod(p, mode)
    else:
        shutil.copyfile(paths[0], paths[1]); os.chmod(paths[1], mode)
    finish()
if command in ('haproxy', 'keepalived'):
    path = args[-1]
    installed = path == str(root / ('etc/haproxy/haproxy.cfg' if command == 'haproxy' else 'etc/keepalived/keepalived.conf'))
    kind = 'installed' if installed else 'candidate'
    if state.get('fail') == kind + ':' + command: finish(1)
    finish()
if command == 'ss':
    finish(output='' if state.get('fail') == 'listener' else 'LISTEN 0 4096 0.0.0.0:6443 0.0.0.0:*')
if command == 'systemctl':
    action, service = args[0], args[-1]
    key = action + ':' + service
    count = state['attempts'].get(key, 0) + 1; state['attempts'][key] = count
    if state.get('fail') == key and count == (2 if action == 'is-enabled' else 1):
        if action == 'restart': state['active'][service] = False
        finish(1)
    if action == 'enable': state['enabled'][service] = True
    elif action == 'disable': state['enabled'][service] = False
    elif action == 'is-enabled': finish(0 if state['enabled'].get(service) else 1)
    elif action == 'is-active': finish(0 if state['active'].get(service) else 3)
    elif action == 'restart': state['active'][service] = True
    elif action == 'stop': state['active'][service] = False
    finish()
finish(1, 'unexpected command: ' + command)
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='atlas-lb-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        for name in ('id', 'sleep', 'sysctl', 'ip', 'apt-get', 'stat', 'install', 'haproxy', 'keepalived', 'ss', 'systemctl'):
            self.write('bin/' + name, STUB, 0o755)
        self.write('usr/bin/killall', '#!/bin/sh\nexit 0\n', 0o755)
        for d in ('etc/haproxy', 'etc/keepalived', 'tmp'): (self.root / d).mkdir(parents=True, exist_ok=True)
        self.set_state()
        source = (REPO / 'scripts/stages/setup-lb.sh').read_text()
        for path in ('/etc/', '/usr/local/libexec/', '/usr/bin/killall', '/run/haproxy', '/var/backups/atlas-kubekit/lb-setup'):
            source = source.replace(path, str(self.root) + path)
        self.write('setup-lb.sh', source)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        LB_TEST_ROOT=str(self.root), TMPDIR=str(self.root / 'tmp'),
                        AUTH_PASS='Shared01', IFACE='enp6s18', VIP='10.20.1.9',
                        API_PORT='6443', VRRP_ID='51', CP_BACKENDS='cp1=10.20.1.13',
                        LB_PRIORITY='250', APP_INGRESS_VIP='')

    def write(self, path, content, mode=0o600):
        p = self.root / path; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content); p.chmod(mode)
        return p

    def set_state(self, fail='', active=False):
        previous = self.state() if (self.root / 'commands.json').exists() else {}
        (self.root / 'commands.json').write_text(json.dumps(dict(calls=[], attempts={}, fail=fail,
            active=dict(haproxy=active, keepalived=active), enabled={}, nonlocal_bind=previous.get('nonlocal_bind','0'))))

    def state(self): return json.loads((self.root / 'commands.json').read_text())

    def run_setup(self, success=True, *args, **env):
        result = subprocess.run([BASH, str(self.root / 'setup-lb.sh'), '10.20.1.12', '--master', *args],
                                env=dict(self.env, **env), text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        self.assertEqual(list((self.root / 'tmp').iterdir()), [], 'temporary files leaked')
        return result

    def old_files(self):
        return {p: self.write(p, value).read_bytes() for p, value in {
            'etc/haproxy/haproxy.cfg': '# previous haproxy\n',
            'etc/keepalived/keepalived.conf': '# previous keepalived\n',
            'etc/atlas-kubekit/lb-manager.json': '{"vips": [], "routes": []}\n',
            'etc/sysctl.d/99-atlas-kubekit-lb.conf': 'net.ipv4.ip_nonlocal_bind = 0\n',
        }.items()}

    def assert_files(self, snapshot):
        for p, content in snapshot.items(): self.assertEqual((self.root / p).read_bytes(), content, p)

    def test_first_install_with_application_vip_and_backup_role(self):
        self.run_setup(APP_INGRESS_VIP='10.20.1.20')
        cfg = (self.root / 'etc/keepalived/keepalived.conf').read_text()
        self.assertIn('state MASTER', cfg)
        self.assertIn('enable_script_security', cfg)
        self.assertIn('10.20.1.20', cfg)
        self.assertEqual(self.state()['nonlocal_bind'], '1')
        self.assertIn('net.ipv4.ip_nonlocal_bind = 1', (self.root / 'etc/sysctl.d/99-atlas-kubekit-lb.conf').read_text())
        self.assertEqual(self.state()['enabled'], dict(haproxy=True, keepalived=True))
        self.run_setup(True, '--slave', APP_INGRESS_VIP='10.20.1.20')
        self.assertIn('state BACKUP', (self.root / 'etc/keepalived/keepalived.conf').read_text())

    def test_repeat_does_not_restart_and_preserves_routes(self):
        self.run_setup(APP_INGRESS_VIP='10.20.1.20')
        path = self.root / 'etc/haproxy/haproxy.cfg'
        block = '# BEGIN ATLAS KUBEKIT MANAGED ROUTES\nfrontend ak_demo\n    bind *:443\n# END ATLAS KUBEKIT MANAGED ROUTES\n'
        path.write_text(path.read_text().rstrip() + '\n\n' + block)
        before = path.read_bytes()
        # The manager writes compact JSON; setup may normalize its formatting.
        # Idempotency preserves data and running services, not JSON whitespace.
        state_path = self.root / 'etc/atlas-kubekit/lb-manager.json'
        managed_state = json.loads(state_path.read_text())
        state_path.write_text(json.dumps(managed_state, separators=(',', ':')))
        self.set_state(active=True)
        self.run_setup(APP_INGRESS_VIP='10.20.1.20')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads(state_path.read_text()), managed_state)
        self.assertFalse(any(c[0:2] == ['systemctl', 'restart'] for c in self.state()['calls']))
        self.run_setup(APP_INGRESS_VIP='10.20.1.21')
        self.assertEqual(json.loads((self.root / 'etc/atlas-kubekit/lb-manager.json').read_text())['vips'],
                         ['10.20.1.20', '10.20.1.21'])

    def test_same_health_script_with_bad_permissions_is_repaired(self):
        self.run_setup()
        path = self.root / 'usr/local/libexec/atlas-kubekit/check-haproxy'
        path.chmod(0o700)
        self.run_setup()
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)
        for name in ('etc/keepalived/keepalived.conf', 'etc/atlas-kubekit/lb-manager.json'):
            (self.root / name).chmod(0o666)
        self.run_setup()
        self.assertEqual((self.root / 'etc/keepalived/keepalived.conf').stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.root / 'etc/atlas-kubekit/lb-manager.json').stat().st_mode & 0o777, 0o600)

    def test_failures_restore_all_files_and_running_services(self):
        for failure in ('restart:haproxy', 'restart:keepalived', 'installed:keepalived', 'installed:haproxy', 'install-state', 'listener', 'signal-commit', 'sysctl-write'):
            with self.subTest(failure=failure):
                snapshot = self.old_files(); self.set_state(fail=failure, active=True)
                self.run_setup(False)
                self.assert_files(snapshot)
                self.assertTrue(all(self.state()['active'].values()))
                self.assertFalse(any(self.state()['enabled'].values()))
                self.assertEqual(self.state()['nonlocal_bind'], '0')

    def test_fresh_restart_failure_removes_new_files_and_stops_services(self):
        self.set_state(fail='restart:keepalived')
        self.run_setup(False)
        for p in ('etc/haproxy/haproxy.cfg', 'etc/keepalived/keepalived.conf', 'etc/atlas-kubekit/lb-manager.json', 'etc/sysctl.d/99-atlas-kubekit-lb.conf'):
            self.assertFalse((self.root / p).exists(), p)
        self.assertFalse(any(self.state()['active'].values()))

    def test_enable_and_candidate_failures_do_not_replace_configuration(self):
        for failure in ('enable:haproxy', 'enable:keepalived', 'is-enabled:keepalived', 'candidate:keepalived'):
            with self.subTest(failure=failure):
                snapshot = self.old_files(); self.set_state(fail=failure, active=True)
                self.run_setup(False)
                self.assert_files(snapshot)
                self.assertFalse(any(c[0:2] == ['systemctl', 'restart'] for c in self.state()['calls']))
                self.assertFalse(any(self.state()['enabled'].values()))

    def test_check_only_leaves_configuration_and_services_unchanged(self):
        self.run_setup(APP_INGRESS_VIP='10.20.1.20')
        paths = ('etc/haproxy/haproxy.cfg', 'etc/keepalived/keepalived.conf', 'etc/atlas-kubekit/lb-manager.json')
        snapshot = {p: (self.root / p).read_bytes() for p in paths}
        self.set_state(active=True)
        self.run_setup(True, '--check-only', APP_INGRESS_VIP='10.20.1.20')
        self.assert_files(snapshot)
        self.assertFalse(any(c[0] in ('install', 'apt-get') or c[0:2] in (['systemctl', 'enable'], ['systemctl', 'restart'])
                             for c in self.state()['calls']))

    def test_bad_input_is_rejected_before_apt(self):
        for env in (dict(VIP='999.1.2.3'), dict(API_PORT='7000'), dict(CP_BACKENDS='cp1=10.20.1.999'), dict(IFACE='bad;name')):
            with self.subTest(env=env):
                self.set_state(); self.run_setup(False, **env)
                self.assertFalse(any(c[0] == 'apt-get' for c in self.state()['calls']))

    def test_inconsistent_vip_state_is_rejected_before_config_apply(self):
        snapshot = self.old_files()
        self.write('etc/atlas-kubekit/lb-manager.json', '{"vips":["10.20.1.20"],"routes":[]}')
        snapshot['etc/atlas-kubekit/lb-manager.json'] = (self.root / 'etc/atlas-kubekit/lb-manager.json').read_bytes()
        self.run_setup(False, APP_INGRESS_VIP='10.20.1.21')
        self.assert_files(snapshot)

    def test_reversed_markers_or_missing_routes_are_rejected(self):
        for kind in ('haproxy', 'keepalived', 'missing-routes'):
            with self.subTest(kind=kind):
                snapshot = self.old_files()
                if kind == 'missing-routes':
                    self.write('etc/atlas-kubekit/lb-manager.json', '{"vips":[],"routes":[{"name":"demo"}]}')
                else:
                    path = 'etc/haproxy/haproxy.cfg' if kind == 'haproxy' else 'etc/keepalived/keepalived.conf'
                    marker = 'ROUTES' if kind == 'haproxy' else 'VIPS'
                    self.write(path, '# END ATLAS KUBEKIT MANAGED ' + marker + '\n# BEGIN ATLAS KUBEKIT MANAGED ' + marker + '\n')
                snapshot = {p: (self.root / p).read_bytes() for p in snapshot}
                self.run_setup(False)
                self.assert_files(snapshot)


class PasswordTests(unittest.TestCase):
    def test_reuse_generation_and_disagreement(self):
        source = (REPO / '02-deploy-cluster.sh').read_text()
        function = source[source.index('reuse_lb_password() {'):source.index('pin_host_key() {')]
        step = source[source.index('\nreuse_lb_password\n'):source.index('case "$PROFILE" in standard)')]
        cases = (
            ('fresh', None, None, '', True, None),
            ('reuse', 'Shared01', 'Shared01', '', True, 'Shared01'),
            ('partial', 'Shared01', None, '', True, 'Shared01'),
            ('mismatch', 'Shared01', 'Other001', '', False, None),
            ('saved-mismatch', 'Shared01', 'Shared01', 'Other001', False, None),
            ('invalid-existing', 'password-too-long', None, '', False, None),
        )
        for name, first, second, saved, success, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix='atlas-vrrp-test-') as d:
                root = Path(d)
                for node, password in (('lb1', first), ('lb2', second)):
                    if password is not None:
                        (root / (node + '.conf')).write_text('vrrp_instance OTHER { authentication { auth_pass Other123 } }\n'
                            'vrrp_instance VI_KUBE_APISERVER {\n authentication {\n auth_pass ' + password + '\n }\n}\n')
                harness = r'''set -euo pipefail
LBS=(lb1 lb2)
die() { echo "$*" >&2; exit 1; }
atlas_t() { echo "$1"; }
atlas_printf() { echo "$1"; }
run() { local cmd="${2//\/etc\/keepalived\/keepalived.conf/$LB_TEST_ROOT\/$1.conf}"; bash -c "$cmd"; }
'''
                result = subprocess.run([BASH, '-c', harness + function + step + '\nprintf "RESULT:%s\\n" "$AUTH_PASS"'],
                    env=dict(os.environ, LB_TEST_ROOT=d, AUTH_PASS=saved), text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                if success:
                    value = result.stdout.split('RESULT:')[-1].strip()
                    self.assertEqual(len(value), 8)
                    if expected: self.assertEqual(value, expected)
                else:
                    self.assertNotIn('Shared01', result.stdout + result.stderr)
                    self.assertNotIn('Other001', result.stdout + result.stderr)


class JoinArgumentTests(unittest.TestCase):
    def test_control_planes_use_the_lb_port_and_registered_address(self):
        source = (REPO / 'scripts/stages/join-nodes.sh').read_text()
        fragment = source[source.index('# Match the LB backend port'):source.index('# certificate-key')]
        for role in ('cp', 'worker'):
            with self.subTest(role=role):
                result = subprocess.run([BASH, '-c', 'JOIN_ARGS=(kubeadm join endpoint);\n' + fragment
                    + '\nprintf "%s\\n" "${JOIN_ARGS[@]}"'], env=dict(os.environ, ROLE=role,
                    SELF_IP='10.20.1.14', API_PORT='16443'), text=True, capture_output=True, check=True)
                args = result.stdout.splitlines()
                if role == 'cp':
                    self.assertEqual(args[-4:], ['--apiserver-advertise-address', '10.20.1.14', '--apiserver-bind-port', '16443'])
                else: self.assertEqual(args, ['kubeadm', 'join', 'endpoint'])


class VipOwnershipTests(unittest.TestCase):
    def test_only_one_stable_owner_can_pass(self):
        source = (REPO / '02-deploy-cluster.sh').read_text()
        function = source[source.index('wait_lb_vip_owner() {'):source.index('storage_report() {')]
        for scenario, success, owner in (('single', True, 'lb1'), ('converge', True, 'lb2'),
                ('both', False, ''), ('none', False, ''), ('flapping', False, ''), ('unreachable', False, '')):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory(prefix='atlas-vip-test-') as d:
                harness = r'''set -euo pipefail
LBS=(lb1 lb2)
declare -A NODE_IFACE=([lb1]=enp6s18 [lb2]=enp6s18)
q() { printf '%q' "$1"; }
die() { echo "$*" >&2; exit 1; }
atlas_printf() { echo "$*"; }
sleep() { :; }
run() {
  local count=0
  if [ -f "$LB_TEST_ROOT/count" ]; then read -r count < "$LB_TEST_ROOT/count"; fi
  echo $((count+1)) > "$LB_TEST_ROOT/count"
  case "$SCENARIO" in
    single) [ "$1" != lb1 ] && echo 0 || echo 1 ;;
    converge) if [ "$count" -lt 4 ] || [ "$1" = lb2 ]; then echo 1; else echo 0; fi ;;
    both) echo 1 ;;
    none) echo 0 ;;
    flapping) if [ "$((count/2%2))" -eq 0 ]; then [ "$1" != lb1 ] && echo 0 || echo 1; else [ "$1" != lb2 ] && echo 0 || echo 1; fi ;;
    unreachable) return 255 ;;
  esac
}
'''
                result = subprocess.run([BASH, '-c', harness + function + '\nwait_lb_vip_owner 10.20.1.9'],
                    env=dict(os.environ, LB_TEST_ROOT=d, SCENARIO=scenario), text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
                if success: self.assertIn(owner, result.stdout)


if __name__ == '__main__': unittest.main()
