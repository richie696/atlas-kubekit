#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Plan and apply selected host-baseline repairs to an existing cluster."""
import argparse
from collections import defaultdict
from contextlib import ExitStack
import getpass
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('atlas_audit', HERE / 'check-cluster.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
NODE_SOURCE = (HERE / 'repair-node.py').read_text().replace('STORAGE_SOURCE = None', 'STORAGE_SOURCE = ' + repr(audit.STORAGE_SOURCE), 1)
ITEMS = {
    'hosts': 'Repair all hostname/private-IP mappings; preserve unrelated hosts entries',
    'node-keys': 'Create/repair unique node keys and mutual public-key trust; retain administrator keys',
    'ssh': 'Enforce public-key login and PermitRootLogin=no; validate fresh SSH access',
    'sudo': 'Grant the SSH administrator NOPASSWD:ALL as root for cluster automation',
    'adopt': 'Verify the existing baseline, then register node.conf and readiness',
    'host-keys': 'Rotate missing/duplicated SSH host identities; refresh cluster pins; external clients must refresh theirs',
}
BASIC = ['hosts', 'node-keys', 'ssh', 'sudo', 'adopt']


def version():
    for path in (HERE.parent.parent / 'VERSION', HERE / 'VERSION'):
        if path.is_file():
            return path.read_text().strip()
    for path in (HERE / '02-deploy-cluster.sh', HERE.parent.parent / '02-deploy-cluster.sh'):
        if path.is_file():
            match = re.search(r"ATLAS_KUBEKIT_VERSION='([^']+)'", path.read_text())
            if match:
                return match.group(1)
    return 'unknown'


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', default=os.environ.get('SUDO_USER') or os.environ.get('USER') or 'ubuntu')
    parser.add_argument('--identity', help='local SSH private key; defaults to the saved audit key')
    parser.add_argument('--node', action='append', default=[], metavar='NAME=IP')
    parser.add_argument('--pair', action='append', nargs='?', const='', default=[], metavar='CODE')
    parser.add_argument('--sudo', action='store_true', help='ask once for a shared remote sudo password; never saved')
    parser.add_argument('--items', help='comma-separated hosts,node-keys,ssh,sudo,adopt,host-keys; or all')
    parser.add_argument('--dry-run', action='store_true', help='collect and display the plan; no pairing or baseline writes')
    return parser.parse_args()


def duplicates(records, field):
    groups = defaultdict(list)
    for name, record in records.items():
        value = record.get(field)
        if value:
            groups[value].append(name)
    return [names for names in groups.values() if len(names) > 1]


def choose_items(value, dry_run):
    if value is None and dry_run:
        return BASIC
    if value is None:
        print('\nChoose repairs (comma-separated numbers; 0 cancels):')
        for number, (name, description) in enumerate(ITEMS.items(), 1):
            print(f'  {number}. {name}: {description}')
        value = input('Items [1,2,3,4,5]: ').strip() or '1,2,3,4,5'
    if value.strip() == '0':
        raise ValueError('Repair cancelled')
    selected = list(ITEMS) if value.strip().lower() == 'all' else []
    for entry in ([] if selected else value.split(',')):
        entry = entry.strip()
        if entry.isdigit():
            number = int(entry)
            if number < 1 or number > len(ITEMS):
                raise ValueError(f'Unknown repair item: {entry}')
            entry = list(ITEMS)[number - 1]
        if entry not in ITEMS:
            raise ValueError(f'Unknown repair item: {entry}')
        if entry not in selected:
            selected.append(entry)
    if not selected:
        raise ValueError('Choose at least one repair item')
    return selected


class Cluster:
    def __init__(self, args, nodes, identity, password, directory):
        self.args, self.nodes, self.identity, self.password = args, nodes, identity, password
        self.directory = Path(directory)
        self.kh = self.directory / 'known_hosts'
        self.pins_file = audit.invoking_home() / '.k8s-deploy/audit-known_hosts'
        self.kh.write_text(self.pins_file.read_text() if self.pins_file.exists() else '')
        os.chmod(self.kh, 0o600)
        self.local_name, self.local_addresses, self.local_ip = audit.local_identity()
        self.initiator_key = None
        self.version = version()
        self.verified_mesh = False

    def local(self, name, ip):
        return name == self.local_name and ip in self.local_addresses

    def ssh(self, name, ip, fresh=False):
        cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=' + ('yes' if fresh else 'accept-new'),
               '-o', f'UserKnownHostsFile={self.kh}', '-o', 'GlobalKnownHostsFile=/dev/null',
               '-o', 'HostKeyAlgorithms=ssh-ed25519']
        if fresh:
            cmd += ['-o', 'ControlMaster=no', '-o', 'ControlPath=none']
        else:
            cmd += ['-o', 'ControlMaster=auto', '-o', 'ControlPersist=300', '-o', f'ControlPath={self.directory}/ssh-{name}']
        if self.identity:
            cmd += ['-o', 'IdentitiesOnly=yes', '-i', self.identity]
        config = audit.invoking_home() / '.ssh/config'
        if config.is_file():
            cmd += ['-F', str(config)]
        return cmd + [f'{self.args.user}@{ip}']

    def collect(self):
        records = {}
        for name, ip in self.nodes:
            if self.local(name, ip):
                record, error = audit.local_collect(self.args.user)
            else:
                record, error = audit.ssh_collect(name, ip, self.args.user, self.identity, str(self.kh), self.password)
            if error:
                raise ValueError(f'{name}: collection failed: {error}. Repair requires all inventory nodes to be reachable; use --pair or --identity and --sudo.')
            if record['privileged'] != 'yes':
                raise ValueError(f'{name}: root settings unavailable; rerun --repair --sudo')
            if record['hostname'] != name or ip not in record['ips'].split(','):
                raise ValueError(f'{name}: hostname/IP does not match inventory; correct registration before repair')
            if 'Ubuntu 24.04' not in record['os'] or record['arch'] != 'amd64':
                raise ValueError(f'{name}: unsupported OS/architecture; no repair was applied')
            if record.get('storage', {}).get('error'):
                raise ValueError(f'{name}: storage cannot be inspected; fix access before repair')
            records[name] = record
            if self.local(name, ip) and record.get('host_pub'):
                with self.kh.open('a') as f:
                    f.write(f'{ip},{name} {record["host_pub"]}\n')
        return records

    def operation(self, name, action, extra=None):
        ip = dict(self.nodes)[name]
        request = dict(name=name, ip=ip, user=self.args.user, nodes=self.nodes, version=self.version)
        request.update(extra or {})
        command = 'python3 -c ' + shlex.quote(NODE_SOURCE) + ' ' + shlex.quote(action) + ' ' + shlex.quote(json.dumps(request))
        if self.local(name, ip):
            result = subprocess.run(['bash', '-c', command], text=True, capture_output=True, timeout=max(120, 15 * len(self.nodes)))
        else:
            root_command = ('sudo -S -p "" ' if self.password is not None else 'sudo -n ') + command
            result = subprocess.run(self.ssh(name, ip) + [root_command], input=(self.password + '\n') if self.password is not None else '',
                                    text=True, capture_output=True, timeout=max(120, 15 * len(self.nodes)))
        record, error = audit.parse_collection(result)
        if error:
            raise ValueError(f'{name} {action}: {error}; inspect root-only /var/lib/k8s-deploy/repair-backups, then rerun the selected items')
        if record.get('backup'):
            print(f'  {name} {action}: backup {record["backup"]}')
        print(f'  {name} {action}: {"updated" if record.get("changed") else "verified"}')
        return record

    def fresh_access(self, name):
        ip = dict(self.nodes)[name]
        r = subprocess.run(self.ssh(name, ip, fresh=True) + ['true'], text=True, capture_output=True, timeout=15)
        if r.returncode:
            raise ValueError(f'{name}: fresh SSH login failed: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown error"}')

    def pin(self, name, key):
        ip = dict(self.nodes)[name]
        # Replace matching literal/hashed aliases, preserving other clients/peers.
        for alias in (ip, name):
            subprocess.run(['ssh-keygen', '-R', alias, '-f', str(self.kh)], capture_output=True, text=True)
        with self.kh.open('a') as f:
            f.write(f'{ip},{name} {key}\n')
        audit.save_user_file(self.pins_file, self.kh.read_text())

    def apply_mesh(self, records, authorize):
        host_keys = {name: self.operation(name, 'host-public')['host_key'] for name, _ in self.nodes}
        keys = [record['node_pub'] for record in records.values() if record.get('node_pub')]
        for name, _ in self.nodes:
            self.operation(name, 'mesh' if authorize else 'pins', dict(host_keys=host_keys, node_keys=keys))

    def rotate_hosts(self, records):
        duplicated = {name for group in duplicates(records, 'hostkey') for name in group}
        targets = [name for name, record in records.items() if not record['hostkey'] or name in duplicated]
        if not targets:
            print('  SKIP host-keys: all ED25519 host identities are unique')
            return
        if self.initiator_key is None:
            raise ValueError('Host-key rotation requires an explicit outgoing SSH private key')
        print('External SSH clients (including DBX) will need the new fingerprints for rotated nodes.')
        for name in targets:
            old_key = self.operation(name, 'host-public')['host_key'] if records[name]['hostkey'] else None
            self.operation(name, 'authorize', {'initiator_key': self.initiator_key})
            self.fresh_access(name)
            # Open/retain a transport authenticated by the old key for rollback.
            if not self.local(name, dict(self.nodes)[name]):
                r = subprocess.run(self.ssh(name, dict(self.nodes)[name]) + ['true'], text=True, capture_output=True, timeout=15)
                if r.returncode:
                    raise ValueError(f'{name}: could not retain an authenticated SSH session for rotation')
            result = self.operation(name, 'host-keys')
            try:
                self.pin(name, result['host_key'])
                self.fresh_access(name)
            except (ValueError, OSError, subprocess.TimeoutExpired):
                self.operation(name, 'rollback-host-keys', {'host_backup': result['host_backup']})
                if old_key:
                    self.pin(name, old_key)
                raise ValueError(f'{name}: fresh login failed after rotation; host keys were restored. Inspect the backup before retrying.') from None
            with tempfile.NamedTemporaryFile(mode='w') as pub:
                pub.write(result['host_key'] + '\n'); pub.flush()
                fingerprint = subprocess.run(['ssh-keygen', '-lf', pub.name], text=True, capture_output=True).stdout.strip()
            print(f'  New host identity {name}={dict(self.nodes)[name]}: {fingerprint}')
            # Persist cluster pins after each node, before attempting the next one.
            self.apply_mesh(records, authorize=False)

    def adopt(self, records):
        for field in ('machine_id', 'product_uuid', 'primary_mac', 'hostkey', 'nodekey'):
            groups = duplicates(records, field)
            if groups or any(not record.get(field) for record in records.values()):
                print(f'  BLOCKED adoption: {field} missing or duplicated: {groups}; no new ready markers were written')
                return False
        for name, record in records.items():
            if record['sudo'] != 'yes':
                print(f'  BLOCKED adoption: {name} lacks the configured passwordless-sudo policy')
                return False
        if not self.verified_mesh:
            for name, _ in self.nodes:
                self.operation(name, 'verify-mesh')
            self.verified_mesh = True
        for name, _ in self.nodes:
            self.operation(name, 'adopt')
        return True

    def close(self):
        for name, ip in self.nodes:
            if (self.directory / f'ssh-{name}').exists():
                subprocess.run(self.ssh(name, ip)[:-1] + ['-O', 'exit', f'{self.args.user}@{ip}'], text=True, capture_output=True, timeout=10)


def plan(records, selected, nodes):
    print('\nAtlas KubeKit existing-cluster repair plan')
    for name in selected:
        print(f'  {name}: {ITEMS[name]}')
    for name, _ in nodes:
        r = records[name]
        missing = []
        if not r['manifest']: missing.append('node.conf')
        if r['ready'] != 'yes': missing.append('ready')
        if r['node_pair'] != 'yes': missing.append('node-key-pair')
        if r['sudo'] != 'yes': missing.append('passwordless-sudo')
        if 'permitrootlogin no' not in r['sshd'].split(','): missing.append('SSH-policy')
        mappings = dict(nodes)
        if any(not re.search(rf'^\s*{re.escape(ip)}\s+[^#\n]*\b{re.escape(peer)}\b', r['hosts'], re.M) for peer, ip in mappings.items()): missing.append('hosts')
        print(f'  {name}: {", ".join(missing) or "recorded baseline present"}')
    for field in ('machine_id', 'product_uuid', 'primary_mac', 'hostkey', 'nodekey'):
        for names in duplicates(records, field):
            print(f'  Duplicate {field}: {", ".join(names)}')
    print('Backups: /var/lib/k8s-deploy/repair-backups on each changed node (root-only).')
    print('Adoption needs verified identities, SSH mesh, host policy, sudo, runtime and storage; failed prerequisites keep it blocked.')
    print('This plan does not run package installation, change networking/machine-id, restart Kubernetes, reinitialize kubeadm, or alter data mounts.')


def main():
    args = parse_args()
    cluster = None
    try:
        if os.geteuid() != 0:
            raise ValueError('Run with sudo; repair collects and changes privileged host settings')
        if args.dry_run and args.pair:
            raise ValueError('--dry-run does not create pairing authorization. Pair first with --check --pair, then preview --repair --dry-run.')
        if not args.dry_run and not sys.stdin.isatty():
            raise ValueError('Repair requires an interactive terminal for plan confirmation; use --dry-run for a read-only preview')
        identity = audit.resolve_identity(args)
        nodes = audit.inventory(args)
        if args.pairings:
            nodes, identity = audit.pair_inventory(args, nodes)
        if not identity:
            raise ValueError('Repair requires an outgoing private key; complete --check --pair or provide --identity')
        password = getpass.getpass('Shared remote sudo password (not saved): ') if args.sudo else None
        with tempfile.TemporaryDirectory(prefix='atlas-repair-', dir='/tmp') as directory, ExitStack() as cleanup:
            cluster = Cluster(args, nodes, identity, password, directory)
            cleanup.callback(cluster.close)
            records = cluster.collect()
            selected = choose_items(args.items, args.dry_run)
            plan(records, selected, nodes)
            if args.dry_run:
                print('\nPreview complete; no node configuration or saved inventory was changed.')
                return 0
            if input('Type REPAIR to apply this plan [cancel]: ').strip() != 'REPAIR':
                print('Cancelled; no baseline repair was applied.')
                return 0
            if 'host-keys' in selected and input('Type ROTATE HOST KEYS to authorize changed SSH host fingerprints [cancel]: ').strip() != 'ROTATE HOST KEYS':
                print('Cancelled; no baseline repair was applied.')
                return 0
            key = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', identity], text=True, capture_output=True, timeout=15)
            if key.returncode:
                raise ValueError('Repair needs an unencrypted key file; use the dedicated audit key generated by pairing')
            cluster.initiator_key = key.stdout.strip()
            # A selected node-key repair may replace the caller's standard key.
            # Keep the original transport credential only on this initiating node.
            transport_key = cluster.directory / 'transport-key'
            transport_key.write_bytes(Path(identity).read_bytes())
            os.chmod(transport_key, 0o600)
            cluster.identity = str(transport_key)
            # Save an explicit list for subsequent rechecks, using original caller ownership.
            audit.save_user_file(audit.invoking_home() / '.k8s-deploy/audit-nodes.conf', ''.join(f'{n}={ip}\n' for n, ip in nodes))
            if 'hosts' in selected:
                for name, _ in nodes: cluster.operation(name, 'hosts')
            if 'node-keys' in selected:
                duplicate_keys = {name for group in duplicates(records, 'nodekey') for name in group}
                for name, _ in nodes:
                    cluster.operation(name, 'node-keys', {'rotate': name in duplicate_keys or records[name]['node_pair'] != 'yes'})
                records = cluster.collect()
                cluster.apply_mesh(records, authorize=True)
            if 'sudo' in selected:
                for name, _ in nodes:
                    if records[name]['sudo'] != 'yes': cluster.operation(name, 'sudo')
            if 'ssh' in selected:
                for name, _ in nodes:
                    cluster.operation(name, 'authorize', {'initiator_key': cluster.initiator_key})
                    cluster.fresh_access(name)
                    result = cluster.operation(name, 'ssh', {'initiator_key': cluster.initiator_key, 'initiator_ip': cluster.local_ip or dict(nodes).get(cluster.local_name, '127.0.0.1')})
                    try:
                        cluster.fresh_access(name)
                    except (ValueError, subprocess.TimeoutExpired):
                        cluster.operation(name, 'rollback-ssh', {'previous_policy': result['previous_policy']})
                        raise ValueError(f'{name}: fresh login failed; the previous SSH policy was restored') from None
            if 'host-keys' in selected:
                cluster.rotate_hosts(records)
            records = cluster.collect()
            if 'node-keys' in selected:
                cluster.apply_mesh(records, authorize=True)
                for name, _ in nodes: cluster.operation(name, 'verify-mesh')
                cluster.verified_mesh = True
            adopted = cluster.adopt(records) if 'adopt' in selected else None
            print('\nSelected repairs finished. Running the read-only compliance audit again.')
            command = [sys.executable, str(HERE / 'check-cluster.py'), '--user', args.user, '--identity', identity]
            for name, ip in nodes: command += ['--node', f'{name}={ip}']
            # Reuse the password in memory; never place it in argv, a file, or env.
            original_argv, original_getpass = sys.argv, audit.getpass.getpass
            if password is not None:
                command += ['--sudo']
                audit.getpass.getpass = lambda prompt: password
            try:
                sys.argv = command[1:]
                audit_result = audit.main()
            finally:
                sys.argv, audit.getpass.getpass = original_argv, original_getpass
            if adopted is False:
                print('Registration was blocked by unmet prerequisites. Repair those findings and rerun --items adopt.')
            return audit_result or (1 if adopted is False else 0)
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired, EOFError, KeyboardInterrupt) as exc:
        print(f'[ERROR] {exc or "Repair interrupted"}', file=sys.stderr)
        print('Completed items remain in place with backups; rerun the selected items after resolving the reported issue.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
