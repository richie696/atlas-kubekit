#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Root-only, bounded operations for adopting an existing Kubernetes node."""
import datetime
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile

STORAGE_SOURCE = None


def run(*args, timeout=30):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError(f'{args[0]} failed: {result.stderr.strip() or result.stdout.strip()}')
    return result.stdout.strip()


def safe_path(path):
    path = Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError(f'Refusing a symlink in repair path: {path}')
    return path


def atomic_write(path, text, mode=0o600, uid=0, gid=0):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
            tmp = f.name
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
            os.chmod(f.name, mode)
            os.chown(f.name, uid, gid)
        os.replace(tmp, path)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


class Repair:
    def __init__(self, request):
        if os.geteuid() != 0:
            raise ValueError('Repair needs root; use sudo or --sudo on the initiating node')
        self.request = request
        self.name = request['name']
        self.ip = request['ip']
        self.user = request['user']
        if not re.fullmatch(r'(?:lb|cp|w)[1-9][0-9]*', self.name):
            raise ValueError('Invalid node name')
        ipaddress.IPv4Address(self.ip)
        if not re.fullmatch(r'[a-z_][a-z0-9_-]*', self.user) or self.user == 'root':
            raise ValueError('Use an existing non-root SSH administrator')
        if socket.gethostname().split('.')[0] != self.name:
            raise ValueError('Registered hostname does not match this node; no repair was applied')
        self.account = pwd.getpwnam(self.user)
        self.home = safe_path(self.account.pw_dir)
        self.ssh = safe_path(self.home / '.ssh')
        self.state = safe_path('/var/lib/k8s-deploy')
        self.role = 'lb' if self.name.startswith('lb') else 'cp' if self.name.startswith('cp') else 'worker'
        self.address()
        self.backup_dir = None
        self.changed = []

    def address(self):
        for iface in json.loads(run('ip', '-j', '-4', 'addr', 'show')):
            for address in iface.get('addr_info', []):
                if address.get('local') == self.ip:
                    return iface['ifname'], address['prefixlen']
        raise ValueError('Registered IPv4 is not active on this node')

    def storage(self):
        source = STORAGE_SOURCE or Path(__file__).with_name('storage.py').read_text()
        scope = {'__name__': 'atlas_storage'}
        exec(source, scope)
        return scope['probe'](self.role)

    def backup(self, path):
        path = safe_path(path)
        if self.backup_dir is None:
            root = safe_path(self.state / 'repair-backups')
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(root, 0o700)
            self.backup_dir = Path(tempfile.mkdtemp(prefix=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ-'), dir=root))
            os.chmod(self.backup_dir, 0o700)
        destination = self.backup_dir / str(path).lstrip('/')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if not path.is_file():
                raise ValueError(f'Expected a regular file: {path}')
            shutil.copy2(path, destination)
            # Backups of node private keys remain accessible only to root.
            os.chmod(destination, 0o600)
            os.chown(destination, 0, 0)
        else:
            destination.with_name(destination.name + '.absent').touch(mode=0o600)
        return destination

    def write(self, path, text, mode=0o600, user=False):
        path = safe_path(path)
        uid, gid = (self.account.pw_uid, self.account.pw_gid) if user else (0, 0)
        if path.is_file() and path.read_text() == text:
            info = path.stat()
            if stat.S_IMODE(info.st_mode) == mode and (info.st_uid, info.st_gid) == (uid, gid):
                return
        self.backup(path)
        atomic_write(path, text, mode, uid, gid)
        self.changed.append(str(path))

    def prepare_ssh_dir(self):
        self.ssh.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.ssh, 0o700)
        os.chown(self.ssh, self.account.pw_uid, self.account.pw_gid)

    def public(self, key):
        key = ' '.join(key.strip().split()[:2])
        if not re.fullmatch(r'(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+', key):
            raise ValueError('Invalid SSH public key')
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write(key + '\n'); f.flush()
            run('ssh-keygen', '-lf', f.name)
        return key

    def authorize(self, keys):
        self.prepare_ssh_dir()
        path = safe_path(self.ssh / 'authorized_keys')
        current = path.read_text() if path.exists() else ''
        # Retain all existing administrator keys, comments and restrictions.
        installed = set()
        for line in current.splitlines():
            try:
                fields = shlex.split(line, comments=True)
            except ValueError:
                continue
            # Respect an existing restricted authorization too; do not append
            # an unrestricted copy of the same administrator key.
            for position in (0, 1):
                if len(fields) > position + 1 and fields[position] in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256'):
                    installed.add(tuple(fields[position:position + 2]))
                    break
        missing = [self.public(k) for k in keys if tuple(self.public(k).split()) not in installed]
        if missing:
            current = current.rstrip('\n') + ('\n' if current else '') + '\n'.join(dict.fromkeys(missing)) + '\n'
        self.write(path, current, user=True)

    def hosts(self):
        inventory = self.request['nodes']
        aliases = {name for name, _ in inventory}
        path = Path('/etc/hosts')
        lines, block = [], False
        for line in path.read_text().splitlines():
            if line.strip() in ('# BEGIN K8S CLUSTER', '# BEGIN K8S NODE'):
                if block:
                    raise ValueError('Nested managed hosts block; inspect /etc/hosts manually')
                block = True
                continue
            if line.strip() in ('# END K8S CLUSTER', '# END K8S NODE'):
                block = False
                continue
            if block:
                continue
            body, separator, comment = line.partition('#')
            parts = body.split()
            if len(parts) > 1 and aliases.intersection(parts[1:]):
                remaining = [a for a in parts[1:] if a not in aliases]
                if remaining:
                    lines.append(parts[0] + '\t' + ' '.join(remaining) + (' #' + comment if separator else ''))
                elif separator:
                    lines.append('#' + comment)
            else:
                lines.append(line)
        if block:
            raise ValueError('Unterminated managed hosts block; inspect /etc/hosts manually')
        managed = ['# BEGIN K8S CLUSTER'] + [f'{ip} {name}' for name, ip in inventory] + ['# END K8S CLUSTER']
        self.write(path, '\n'.join(managed + lines) + '\n', 0o644)
        for name, ip in inventory:
            resolved = run('getent', 'ahostsv4', name).split()[0]
            if resolved != ip:
                raise ValueError(f'{name} resolves to {resolved}, expected {ip}; inspect NSS/DNS configuration')

    def node_keys(self):
        self.prepare_ssh_dir()
        key = safe_path(self.ssh / 'id_ed25519')
        pub = safe_path(str(key) + '.pub')
        if self.request.get('rotate') or not key.exists() or not pub.exists():
            self.backup(key); self.backup(pub)
            with tempfile.TemporaryDirectory(dir=self.ssh) as temp:
                generated = Path(temp) / 'key'
                run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', f'atlas-kubekit@{self.name}', '-f', str(generated))
                # Generate a complete pair before replacing either old file.
                for src, dest, mode in ((generated, key, 0o600), (Path(str(generated) + '.pub'), pub, 0o644)):
                    os.chown(src, self.account.pw_uid, self.account.pw_gid); os.chmod(src, mode)
                    os.replace(src, dest); self.changed.append(str(dest))
        derived = self.public(run('ssh-keygen', '-y', '-P', '', '-f', str(key)))
        if derived != self.public(pub.read_text()):
            raise ValueError('Node private/public key mismatch; select node-keys again to regenerate the pair')
        os.chmod(key, 0o600); os.chmod(pub, 0o644)
        os.chown(key, self.account.pw_uid, self.account.pw_gid)
        os.chown(pub, self.account.pw_uid, self.account.pw_gid)
        self.authorize([derived])
        return {'public_key': derived}

    def host_public(self):
        return self.public(Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text())

    def pins(self):
        inventory = self.request['nodes']
        keys = self.request['host_keys']
        for path, user in ((self.ssh / 'known_hosts', True), (Path('/root/.ssh/known_hosts'), False)):
            safe_path(path.parent).mkdir(mode=0o700, parents=True, exist_ok=True)
            current = path.read_text() if path.exists() else ''
            with tempfile.TemporaryDirectory() as temp:
                candidate = Path(temp) / 'known_hosts'
                candidate.write_text(current)
                for name, ip in inventory:
                    for alias in (name, ip):
                        subprocess.run(['ssh-keygen', '-R', alias, '-f', str(candidate)], capture_output=True, text=True)
                current = candidate.read_text().rstrip('\n')
            additions = [f'{ip},{name} {self.public(keys[name])}' for name, ip in inventory]
            self.write(path, current + ('\n' if current else '') + '\n'.join(additions) + '\n', user=user)

    def mesh(self):
        self.authorize(self.request['node_keys'])
        self.pins()

    def verify_mesh(self):
        key = self.ssh / 'id_ed25519'
        for name, _ in self.request['nodes']:
            run('sudo', '-H', '-u', self.user, 'ssh', '-i', str(key), '-o', 'IdentitiesOnly=yes',
                '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'HostKeyAlgorithms=ssh-ed25519', f'{self.user}@{name}', 'true', timeout=12)
        return {'mesh_verified': True}

    def sudo(self):
        path = Path('/etc/sudoers.d/90-k8s-bootstrap')
        content = f'{self.user} ALL=(root) NOPASSWD:ALL\n'
        old = path.read_text() if path.exists() else None
        with tempfile.NamedTemporaryFile(mode='w') as f:
            f.write(content); f.flush()
            run('visudo', '-cf', f.name)
        self.write(path, content, 0o440)
        try:
            run('visudo', '-cf', '/etc/sudoers')
            run('sudo', '-H', '-u', self.user, 'sudo', '-n', 'true')
        except ValueError:
            if old is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, old, 0o440)
            raise

    def ssh_policy(self):
        # Keep the initiating key usable before changing authentication policy.
        self.authorize([self.request['initiator_key']])
        path = Path('/etc/ssh/sshd_config.d/00-atlas-kubekit-key-only.conf')
        old = path.read_text() if path.exists() else None
        self.write(path, 'PubkeyAuthentication yes\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n', 0o644)
        safe_path('/run/sshd').mkdir(mode=0o755, exist_ok=True)
        try:
            run('/usr/sbin/sshd', '-t')
            effective = run('/usr/sbin/sshd', '-T').splitlines()
            admin = run('/usr/sbin/sshd', '-T', '-C', f'user={self.user},host={self.name},addr={self.request["initiator_ip"]}').splitlines()
            required = {'pubkeyauthentication yes', 'passwordauthentication no', 'kbdinteractiveauthentication no'}
            if not required.issubset(admin) or 'permitrootlogin no' not in effective:
                raise ValueError('An earlier directive or Match block overrides the requested SSH policy; inspect sshd configuration')
            run('systemctl', 'reload', 'ssh')
        except ValueError:
            if old is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, old, 0o644)
            run('/usr/sbin/sshd', '-t'); run('systemctl', 'reload', 'ssh')
            raise
        return {'policy_path': str(path), 'previous_policy': old}

    def rollback_policy(self):
        path = Path('/etc/ssh/sshd_config.d/00-atlas-kubekit-key-only.conf')
        previous = self.request['previous_policy']
        if previous is None:
            safe_path(path).unlink(missing_ok=True)
        else:
            atomic_write(path, previous, 0o644)
        run('/usr/sbin/sshd', '-t'); run('systemctl', 'reload', 'ssh')

    def host_keys(self):
        self.backup('/etc/ssh/ssh_host_ed25519_key')
        old_paths = list(Path('/etc/ssh').glob('ssh_host_*_key')) + list(Path('/etc/ssh').glob('ssh_host_*_key.pub'))
        for path in old_paths:
            self.backup(path)
        for kind in ('ed25519', 'ecdsa', 'rsa'):
            for suffix in ('', '.pub'):
                self.backup(f'/etc/ssh/ssh_host_{kind}_key{suffix}')
        try:
            with tempfile.TemporaryDirectory(dir=self.backup_dir) as temp:
                for kind in ('ed25519', 'ecdsa', 'rsa'):
                    generated = Path(temp) / f'ssh_host_{kind}_key'
                    run('ssh-keygen', '-q', '-t', kind, '-N', '', '-C', self.name, '-f', str(generated), timeout=60)
                for generated in Path(temp).iterdir():
                    destination = safe_path(Path('/etc/ssh') / generated.name)
                    shutil.copy2(generated, destination)
                    os.chmod(destination, 0o644 if destination.suffix == '.pub' else 0o600)
                    self.changed.append(str(destination))
            run('/usr/sbin/sshd', '-t')
            # HUP preserves existing sessions; the coordinator retains its SSH master.
            run('systemctl', 'reload', 'ssh')
        except (ValueError, OSError, subprocess.TimeoutExpired):
            self.restore_host_keys(str(self.backup_dir))
            raise
        return {'host_key': self.host_public(), 'host_backup': str(self.backup_dir)}

    def restore_host_keys(self, backup):
        backup = safe_path(backup)
        if backup.parent != self.state / 'repair-backups':
            raise ValueError('Host-key rollback must use a recorded repair backup')
        for path in (backup / 'etc/ssh').glob('ssh_host_*_key*'):
            if path.name.endswith('.absent'):
                safe_path(Path('/etc/ssh') / path.name[:-7]).unlink(missing_ok=True)
                continue
            destination = Path('/etc/ssh') / path.name
            shutil.copy2(path, destination)
            os.chmod(destination, 0o644 if destination.suffix == '.pub' else 0o600)
        run('/usr/sbin/sshd', '-t'); run('systemctl', 'reload', 'ssh')

    def adopt(self):
        # Coordinator supplies no ready flag: the node verifies its own baseline.
        import platform
        release = platform.freedesktop_os_release()
        if release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '24.04' or run('dpkg', '--print-architecture') != 'amd64':
            raise ValueError('Only Ubuntu 24.04 amd64 can be adopted')
        memory = int(re.search(r'^MemTotal:\s+(\d+)', Path('/proc/meminfo').read_text(), re.M).group(1))
        if memory < 1900000 or (self.role == 'cp' and (os.cpu_count() or 0) < 2):
            raise ValueError('At least 2 GiB RAM and 2 control-plane CPUs are required')
        if shutil.disk_usage('/').free < 10 * 1024**3:
            raise ValueError('At least 10 GiB free system storage is required')
        run('sudo', '-H', '-u', self.user, 'sudo', '-n', 'true')
        policy = run('sudo', '-n', '-l', '-U', self.user)
        if not re.search(r'(?m)^\s*\([^)]*\)\s*NOPASSWD:\s*ALL\s*$', policy):
            raise ValueError('Passwordless-sudo policy is not configured; no ready marker was written')
        hosts = Path('/etc/hosts').read_text()
        for name, ip in self.request['nodes']:
            if not re.search(rf'^\s*{re.escape(ip)}\s+[^#\n]*\b{re.escape(name)}\b', hosts, re.M) or run('getent', 'ahostsv4', name).split()[0] != ip:
                raise ValueError('Hostname mapping is incomplete or conflicts with NSS resolution')
        self.verify_mesh()
        effective = run('/usr/sbin/sshd', '-T').splitlines()
        if not {'pubkeyauthentication yes', 'passwordauthentication no', 'kbdinteractiveauthentication no', 'permitrootlogin no'}.issubset(effective):
            raise ValueError('SSH baseline is incomplete; no ready marker was written')
        storage = self.storage()
        if any(not v['writable'] or v.get('free', 0) < 10 * 1024**3 for v in storage['assigned']):
            raise ValueError('Existing component storage is read-only or lacks free space')
        if self.role != 'lb':
            for service in ('containerd', 'kubelet'):
                run('systemctl', 'is-active', service)
            if run('kubelet', '--version') != 'Kubernetes v1.36.5':
                raise ValueError('Kubelet version differs from the current deployment baseline')
            if run('swapon', '--noheadings'):
                raise ValueError('Swap is active; handle it in a maintenance window')
            for setting in ('net.ipv4.ip_forward', 'net.bridge.bridge-nf-call-iptables', 'net.bridge.bridge-nf-call-ip6tables'):
                if run('sysctl', '-n', setting) != '1':
                    raise ValueError(f'{setting} is not 1')
            for module in ('overlay', 'br_netfilter'):
                if not re.search(rf'^{module}\s', run('lsmod'), re.M) or module not in Path('/etc/modules-load.d/k8s.conf').read_text().splitlines():
                    raise ValueError(f'{module} is not loaded/persistent')
            config = Path('/etc/containerd/config.toml').read_text()
            sections = re.split(r'(?m)^\s*\[', config)
            if not any(re.search(r'runtimes\.runc\.options\]', section) and re.search(r'(?m)^\s*SystemdCgroup\s*=\s*true\s*$', section) for section in sections):
                raise ValueError('containerd systemd cgroup is not configured')
            self.storage_guard(storage)
        else:
            run('systemctl', 'is-active', 'haproxy'); run('systemctl', 'is-active', 'keepalived')
        public = self.public(run('ssh-keygen', '-y', '-P', '', '-f', str(self.ssh / 'id_ed25519')))
        if public != self.public((self.ssh / 'id_ed25519.pub').read_text()):
            raise ValueError('Node key pair does not match')
        iface, prefix = self.address()
        disk = 'existing' if storage['assigned'] else 'none'
        fields = dict(VERSION='1', PHASE1_READY='1', NAME=self.name, ROLE=self.role, IP=self.ip,
                      PREFIX=str(prefix), IFACE=iface, NETWORK_MODE='keep', SSH_USER=self.user,
                      SSH_HOME=str(self.home), NODE_PUBLIC_KEY=(self.ssh / 'id_ed25519.pub').read_text().strip(), DATA_DEVICE=disk,
                      OS_ID='ubuntu', OS_VERSION='24.04', ARCH='amd64')
        manifest_path = safe_path(self.home / '.k8s-deploy/node.conf')
        if manifest_path.is_file() and (self.state / 'ready').is_file() and (self.state / 'node.conf').is_file():
            current = dict(line.split('=', 1) for line in manifest_path.read_text().splitlines() if '=' in line)
            required = set(fields) - {'DATA_DEVICE', 'NETWORK_MODE'}
            registered_disk = current.get('DATA_DEVICE', '')
            disk_matches = registered_disk == disk or (disk == 'existing' and registered_disk.startswith('/dev/') and any(os.path.realpath(registered_disk) == os.path.realpath(v['device']) for v in storage['assigned']))
            if all(current.get(key) == fields[key] for key in required) and current.get('NETWORK_MODE') in ('keep', 'static') and disk_matches:
                return {'adopted': True, 'already_registered': True}
        manifest_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(manifest_path.parent, 0o700)
        os.chown(manifest_path.parent, self.account.pw_uid, self.account.pw_gid)
        self.write(self.home / '.k8s-deploy/node.conf', ''.join(f'{key}={value}\n' for key, value in fields.items()), user=True)
        config = {key: fields[key] for key in ('NAME', 'IP', 'PREFIX', 'IFACE', 'NETWORK_MODE', 'SSH_USER', 'DATA_DEVICE')}
        config.update(GATEWAY='', ADMIN_KEY='')
        # shlex.quote creates data-only assignments compatible with the Phase 1 Bash loader.
        self.write(self.state / 'node.conf', ''.join(f'{key}={shlex.quote(value)}\n' for key, value in config.items()))
        record = dict(source='verified-existing-node', tool_version=self.request['version'],
                      verified_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), node=self.name,
                      storage=storage, scope='host-baseline adoption; no package upgrades or cluster reinitialization')
        self.write(self.state / 'adopted-baseline.json', json.dumps(record, indent=2) + '\n')
        self.write(self.state / 'ssh-host-keys-unique', 'verified by cluster repair\n')
        self.write(self.state / 'ready', 'verified-existing-node\n')
        return {'adopted': True}

    def storage_guard(self, storage):
        if self.role == 'cp':
            source = STORAGE_SOURCE or Path(__file__).with_name('storage.py').read_text()
            scope = {'__name__': 'atlas_storage'}; exec(source, scope)
            scope['check_etcd']('existing' if storage['assigned'] else 'none')

    def execute(self, action):
        method = {'hosts': self.hosts, 'node-keys': self.node_keys, 'mesh': self.mesh,
                  'pins': self.pins, 'verify-mesh': self.verify_mesh, 'sudo': self.sudo,
                  'ssh': self.ssh_policy, 'rollback-ssh': self.rollback_policy,
                  'host-keys': self.host_keys, 'adopt': self.adopt,
                  'host-public': lambda: {'host_key': self.host_public()},
                  'authorize': lambda: self.authorize([self.request['initiator_key']]),
                  'rollback-host-keys': lambda: self.restore_host_keys(self.request['host_backup'])}.get(action)
        if method is None:
            raise ValueError('Unknown repair operation')
        # Helpers may print diagnostics. Keep the final JSON frame machine-readable.
        result = method() or {}
        return dict(result, changed=self.changed, backup=str(self.backup_dir) if self.backup_dir else None)


def main():
    try:
        request = json.loads(sys.argv[2])
        # Never accept inventory aliases or keys as shell code.
        for name, address in request.get('nodes', []):
            if not re.fullmatch(r'(?:lb|cp|w)[1-9][0-9]*', name):
                raise ValueError('Invalid inventory hostname')
            ipaddress.IPv4Address(address)
        print(json.dumps(Repair(request).execute(sys.argv[1])))
        return 0
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f'[ERROR] {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
