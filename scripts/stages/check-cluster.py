#!/usr/bin/env python3
"""Phase 1/2 read-only audit, with explicit one-time SSH access pairing."""

import argparse
import getpass
import ipaddress
import json
import os
import pwd
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from enrollment import PairingError, decode_pairing_code, enroll

STORAGE_SOURCE = Path(__file__).with_name('storage.py').read_text()

REMOTE = r'''python3 - <<'PY'
import json, os, pathlib, pwd, re, shlex, shutil, subprocess
def out(cmd):
    try: return subprocess.run(cmd, text=True, capture_output=True, timeout=5).stdout.strip()
    except Exception: return ''
u=os.environ.get('AUDIT_USER','ubuntu')
def passwordless_sudo():
    if not shutil.which('sudo'): return 'missing'
    cmd=['sudo','-n','-l'] + (['-U',u] if os.geteuid()==0 else [])
    # Inspect policy rather than accepting a recently cached sudo password.
    policy=out(cmd)
    return 'yes' if re.search(r'(?m)^\s*\([^)]*\)\s*NOPASSWD:\s*ALL\s*$',policy) else 'no'
try: home=pwd.getpwnam(u).pw_dir
except KeyError: home=''
manifest=pathlib.Path(home)/'.k8s-deploy/node.conf'
data={
 'hostname':out(['hostname','-s']), 'arch':out(['dpkg','--print-architecture']),
 'os':'',
 'ips':out(['bash','-lc',"ip -o -4 addr show scope global | awk '{print $4}' | cut -d/ -f1 | paste -sd, -"]),
 'primary_mac':out(['bash','-lc',"dev=$(ip route show default | awk 'NR==1 {for(i=1;i<=NF;i++) if($i==\"dev\") {print $(i+1); exit}}'); [ -n \"$dev\" ] && cat /sys/class/net/\"$dev\"/address"]),
 'machine_id':out(['cat','/etc/machine-id']),
 'product_uuid':out(['bash','-lc','cat /sys/class/dmi/id/product_uuid 2>/dev/null || sudo -n cat /sys/class/dmi/id/product_uuid 2>/dev/null']),
 'macs':out(['bash','-lc',"ip -o link show | awk -F'link/ether ' 'NF>1 {split($2,a,\" \"); print a[1]}' | paste -sd, -"]),
 'hostkey':out(['bash','-lc',"ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub 2>/dev/null | awk '{print $2}'"]),
 'host_pub':out(['cat','/etc/ssh/ssh_host_ed25519_key.pub']),
 'nodekey':out(['bash','-lc',f"ssh-keygen -lf {shlex.quote(home+'/.ssh/id_ed25519.pub')} 2>/dev/null | awk '{{print $2}}'"]),
 'node_pub':out(['bash','-lc',f"cat {shlex.quote(home+'/.ssh/id_ed25519.pub')} 2>/dev/null"]),
 'node_pair':out(['bash','-lc',f"pub=$(cut -d ' ' -f1,2 {shlex.quote(home+'/.ssh/id_ed25519.pub')} 2>/dev/null); derived=$(ssh-keygen -y -P '' -f {shlex.quote(home+'/.ssh/id_ed25519')} 2>/dev/null | cut -d ' ' -f1,2); [ -n \"$pub\" ] && [ \"$pub\" = \"$derived\" ] && echo yes || echo no"]),
 'authorized':out(['bash','-lc',f"ssh-keygen -lf {shlex.quote(home+'/.ssh/authorized_keys')} 2>/dev/null | awk '{{print $2}}' | sort -u | paste -sd, -"]),
 'manifest':manifest.read_text(errors='replace') if manifest.is_file() else '',
 'sudo':passwordless_sudo(),
 'privileged':'yes' if os.geteuid()==0 or out(['bash','-lc','sudo -n true >/dev/null 2>&1 && echo yes'])=='yes' else 'no',
 'ready':out(['bash','-lc','sudo -n test -f /var/lib/k8s-deploy/ready && echo yes || echo no']) if shutil.which('sudo') else '',
 'containerd':out(['systemctl','is-active','containerd']),
 'kubelet':out(['systemctl','is-active','kubelet']),
 'haproxy':out(['systemctl','is-active','haproxy']),
 'keepalived':out(['systemctl','is-active','keepalived']),
 'kubelet_version':out(['kubelet','--version']),
 'containerd_version':out(['containerd','--version']),
 'swap':out(['bash','-lc',"swapon --noheadings | wc -l"]),
 'hosts':pathlib.Path('/etc/hosts').read_text(errors='replace'),
 'cgroup':out(['bash','-lc',"grep -E 'SystemdCgroup[[:space:]]*=[[:space:]]*true' /etc/containerd/config.toml | head -1"]),
 'sysctl':out(['bash','-lc',"test \"$(sysctl -n net.ipv4.ip_forward 2>/dev/null)\" = 1 && test \"$(sysctl -n net.bridge.bridge-nf-call-iptables 2>/dev/null)\" = 1 && echo yes || echo no"]),
 'modules':out(['bash','-lc',"grep -qs '^overlay$' /etc/modules-load.d/k8s.conf && grep -qs '^br_netfilter$' /etc/modules-load.d/k8s.conf && lsmod | grep -q '^overlay' && lsmod | grep -q '^br_netfilter' && echo yes || echo no"]),
 'sshd':out(['bash','-lc',"if sudo -n true >/dev/null 2>&1; then sudo -n /usr/sbin/sshd -T 2>/dev/null | awk '/^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) / {print}' | sort | paste -sd, -; else echo UNVERIFIED_SUDO; fi"]),
}
try:
    import platform
    data['os']=platform.freedesktop_os_release().get('PRETTY_NAME','')
except Exception: pass
if data['sudo']=='': data['sudo']='unavailable'
storage_scope={'__name__':'atlas_storage_probe'}
exec(STORAGE_SOURCE, storage_scope)
try:
    role='cp' if data['hostname'].startswith('cp') else 'worker' if data['hostname'].startswith('w') else 'lb'
    data['storage']=storage_scope['probe'](role)
except Exception as exc:
    data['storage']={'error':str(exc)}
print(json.dumps(data))
PY'''
REMOTE = REMOTE.replace('exec(STORAGE_SOURCE, storage_scope)', 'exec(' + repr(STORAGE_SOURCE) + ', storage_scope)')


def parse_args():
    p = argparse.ArgumentParser(description="Audit Phase 1/2 compliance without changing nodes")
    p.add_argument('--user', default=os.environ.get('SUDO_USER') or os.environ.get('USER') or 'ubuntu')
    p.add_argument('--identity', help='local SSH private key; auto-detects audit key and standard keys in the invoking account home')
    p.add_argument('--node', action='append', default=[], metavar='NAME=IP', help='repeat for each cluster node')
    p.add_argument('--pair', action='append', nargs='?', const='', default=[], metavar='CODE', help='prompt for a fresh inventory, or repeat --pair CODE to register nodes')
    p.add_argument('--sudo', action='store_true', help='ask once for a shared remote sudo password to read privileged settings')
    return p.parse_args()


def parse_node_line(line):
    """Parse a node entry written as NAME=IP, NAME IP, or NAME:IP."""
    line = re.sub(r'^\s*Nodes?:\s*', '', line.strip(), flags=re.IGNORECASE)
    if not line or line.lower() == 'done' or line.startswith('```'):
        return None
    match = re.fullmatch(r'((?:lb|cp|w)[1-9][0-9]*)\s*(?:=|:|\s)\s*([0-9.]+)', line)
    if not match:
        raise ValueError(f'Invalid node entry {line!r}; expected NAME=IPv4, for example cp1=10.20.1.13')
    ipaddress.IPv4Address(match.group(2))
    return match.group(1), match.group(2)


def invoking_account():
    return pwd.getpwnam(os.environ['SUDO_USER']) if os.environ.get('SUDO_USER') else pwd.getpwuid(os.getuid())


def invoking_home():
    return Path(invoking_account().pw_dir)


def validate_inventory(items):
    names = [n for n, _ in items]
    addresses = [ip for _, ip in items]
    if len(names) < 2 or not {'lb1', 'cp1'}.issubset(names):
        raise ValueError('Provide the full inventory including lb1 and cp1, using NAME=IPv4 entries or pairing codes.')
    if len(set(names)) != len(names):
        raise ValueError('Duplicate node names in inventory')
    if len(set(addresses)) != len(addresses):
        raise ValueError('Duplicate node IP addresses in inventory')
    for name, ip in items:
        parse_node_line(f'{name}={ip}')
    return items


def inventory(args):
    items = []
    args.pairings = []
    force_input = '' in args.pair
    for code in args.pair:
        if code:
            args.pairings.append(decode_pairing_code(code))
    for spec in args.node:
        node = parse_node_line(spec)
        if not node:
            raise ValueError('--node requires NAME=IPv4')
        items.append(node)
    if not items and not args.pairings:
        saved = invoking_home() / '.k8s-deploy' / 'audit-nodes.conf'
        if not force_input and saved.is_file():
            for line in saved.read_text().splitlines():
                node = parse_node_line(line)
                if node:
                    items.append(node)
            validate_inventory(items)
        hosts = Path('/etc/hosts')
        if not force_input and not items and hosts.exists():
            for line in hosts.read_text(errors='replace').splitlines():
                parts = line.split('#', 1)[0].split()
                if parts:
                    for alias in parts[1:]:
                        if re.fullmatch(r'(?:lb|cp|w)[1-9][0-9]*', alias) and not parts[0].startswith('127.'):
                            node = (alias, parts[0])
                            if node not in items:
                                items.append(node)
            if not {'lb1', 'cp1'}.issubset(n for n, _ in items):
                items = []
        if not items:
            if sys.stdin.isatty():
                print('Paste NAME=IPv4 entries or pairing codes, one per line.')
                print('Accepted separators: =, space, or :. End the list with a blank line or done.')
                print('Nodes:')
            invalid = 0
            for value in sys.stdin:
                line = value.strip()
                if line.lower() == 'done' or (not line and (items or args.pairings)):
                    break
                if not line or line.startswith('```'):
                    continue
                try:
                    if re.match(r'^(?:Nodes?:\s*)?(?:lb|cp|w)[0-9]+[\s=:]', line, re.I):
                        node = parse_node_line(line)
                        items.append(node)
                    else:
                        args.pairings.append(decode_pairing_code(line))
                except ValueError as exc:
                    invalid += 1
                    print(f'[WARN] {exc}')
            if invalid:
                raise ValueError(f'{invalid} invalid entry(s); correct the list and retry. No partial inventory was audited.')
    if args.pairings:
        addresses = [ip for _, ip in items] + [ip for ip, _ in args.pairings]
        if len(set(addresses)) != len(addresses):
            raise ValueError('Duplicate node IP addresses in inventory/pairing codes')
        return items
    return validate_inventory(items)


def resolve_identity(args):
    home = invoking_home()
    if args.identity:
        value = args.identity
        path = home / value[2:] if value.startswith('~/') else Path(value).expanduser()
        if not path.is_file() or not os.access(path, os.R_OK):
            raise ValueError(f'SSH private key is missing or unreadable: {path}; --identity must point to a local private key')
        return str(path)
    for filename in ('id_ed25519_atlas_audit', 'id_ed25519', 'id_rsa', 'id_ecdsa'):
        path = home / '.ssh' / filename
        if path.is_file() and os.access(path, os.R_OK):
            return str(path)
    return None


def save_user_file(path, contents):
    account = invoking_account()
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError(f'Unsafe symlink in audit state: {path}')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as tmp:
        tmp.write(contents)
        os.chmod(tmp.name, 0o600)
        if os.geteuid() == 0:
            os.chown(tmp.name, account.pw_uid, account.pw_gid)
            os.chown(path.parent, account.pw_uid, account.pw_gid)
    os.replace(tmp.name, path)


def audit_identity():
    account = invoking_account()
    key = invoking_home() / '.ssh' / 'id_ed25519_atlas_audit'
    public = Path(str(key) + '.pub')
    if key.parent.is_symlink() or key.is_symlink() or public.is_symlink():
        raise ValueError('Audit key paths must not be symlinks')
    if key.exists() != public.exists():
        raise ValueError(f'Audit key pair is incomplete: {key}; restore the missing file before pairing')
    if not key.exists():
        key.parent.mkdir(mode=0o700, exist_ok=True)
        r = subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C',
                            f'atlas-kubekit-audit@{socket.gethostname()}', '-f', str(key)],
                           text=True, capture_output=True, timeout=15)
        if r.returncode:
            raise ValueError('Could not generate the local audit key; check ~/.ssh permissions')
        os.chmod(key, 0o600)
        if os.geteuid() == 0:
            for path in (key.parent, key, public):
                os.chown(path, account.pw_uid, account.pw_gid)
    return str(key)


def local_identity():
    name = socket.gethostname().split('.')[0]
    try:
        r = subprocess.run(['ip', '-o', '-4', 'addr', 'show', 'scope', 'global'], text=True, capture_output=True, timeout=5)
        addresses = [line.split()[3].split('/')[0] for line in r.stdout.splitlines()]
        route = subprocess.run(['ip', '-j', 'route', 'show', 'default'], text=True, capture_output=True, timeout=5)
        dev = json.loads(route.stdout)[0]['dev']
        preferred = next(line.split()[3].split('/')[0] for line in r.stdout.splitlines() if line.split()[1] == dev)
        return name, set(addresses), preferred
    except (OSError, ValueError, KeyError, IndexError, StopIteration, subprocess.TimeoutExpired):
        return name, set(), None


def pair_inventory(args, items):
    identity = resolve_identity(args) if args.identity else audit_identity()
    public = Path(identity + '.pub')
    if not public.is_file() or not public.read_text().startswith('ssh-ed25519 '):
        raise ValueError('Pairing requires a matching ED25519 .pub file; omit --identity to use a dedicated audit key')
    derived = subprocess.run(['ssh-keygen', '-y', '-P', '', '-f', identity], text=True, capture_output=True, timeout=10)
    if derived.returncode or derived.stdout.strip().split()[:2] != public.read_text().strip().split()[:2]:
        raise ValueError('Pairing key files do not match or require a passphrase; omit --identity to use a dedicated audit key')
    state = invoking_home() / '.k8s-deploy'
    pins = state / 'audit-known_hosts'
    print('Pairing authorizes this initiating node for SSH access; the baseline audit remains read-only.')
    for address, code in args.pairings:
        while True:
            try:
                name, host_key = enroll(address, code, public.read_text())
                break
            except PairingError as exc:
                print(f'[WARN] {address}: {exc}')
                if not sys.stdin.isatty():
                    raise ValueError(f'Pairing failed for {address}; regenerate/check its code and retry') from None
                replacement = input('Replacement pairing code (visible; cancel to stop): ').strip()
                if replacement.lower() == 'cancel':
                    raise ValueError('Pairing cancelled') from None
                try:
                    retry_address, retry_code = decode_pairing_code(replacement)
                    if retry_address != address:
                        print(f'[WARN] The replacement must target {address}')
                        continue
                    code = retry_code
                except PairingError as retry_error:
                    print(f'[WARN] {retry_error}')
        lines = pins.read_text().splitlines() if pins.exists() else []
        lines = [line for line in lines if line.split() and line.split()[0] != address]
        save_user_file(pins, '\n'.join(lines + [f'{address} {host_key}']) + '\n')
        items.append((name, address))
        print(f'  Paired {name}={address}')
    local_name, addresses, preferred = local_identity()
    if re.fullmatch(r'(?:lb|cp|w)[1-9][0-9]*', local_name) and local_name not in (name for name, _ in items):
        if preferred and preferred not in (address for _, address in items):
            items.insert(0, (local_name, preferred))
    validate_inventory(items)
    save_user_file(state / 'audit-nodes.conf', ''.join(f'{name}={address}\n' for name, address in items))
    return items, identity


def parse_collection(result):
    if result.returncode:
        return None, result.stderr.strip().splitlines()[-1] if result.stderr.strip() else f'collector exit {result.returncode}'
    try:
        return json.loads(result.stdout.strip().splitlines()[-1]), None
    except (json.JSONDecodeError, IndexError):
        return None, 'Could not parse audit response'


def local_collect(user):
    env = dict(os.environ, AUDIT_USER=user)
    try:
        return parse_collection(subprocess.run(['bash', '-c', REMOTE], env=env, text=True, capture_output=True, timeout=25))
    except subprocess.TimeoutExpired:
        return None, 'Local collection timed out'


def ssh_collect(name, ip, user, identity, kh, sudo_password=None):
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=6', '-o', 'StrictHostKeyChecking=accept-new',
           '-o', f'UserKnownHostsFile={kh}', '-o', 'GlobalKnownHostsFile=/dev/null']
    if identity:
        cmd += ['-o', 'IdentitiesOnly=yes', '-i', identity]
    config = invoking_home() / '.ssh' / 'config'
    if config.is_file():
        cmd += ['-F', str(config)]
    payload = REMOTE
    remote = f'AUDIT_USER={shlex.quote(user)} bash -s'
    if sudo_password is not None:
        remote = f'sudo -S -p "" env AUDIT_USER={shlex.quote(user)} bash -c {shlex.quote(REMOTE)}'
        payload = sudo_password + '\n'
    cmd += [f'{user}@{ip}', remote]
    try:
        r = subprocess.run(cmd, input=payload, text=True, capture_output=True, timeout=30)
        return parse_collection(r)
    except subprocess.TimeoutExpired:
        return None, 'SSH timed out'


def main():
    args = parse_args()
    try:
        identity = resolve_identity(args)
        nodes = inventory(args)
        if args.pairings:
            nodes, identity = pair_inventory(args, nodes)
    except Exception as e:
        print(f'[ERROR] {e}', file=sys.stderr)
        return 2
    agent = None
    if os.environ.get('SSH_AUTH_SOCK'):
        agent = subprocess.run(['ssh-add', '-l'], capture_output=True, text=True).returncode == 0
    missing_auth = not identity and not agent and not (invoking_home() / '.ssh' / 'config').is_file()
    print('Atlas KubeKit Phase 1/2 read-only compliance audit')
    print(f'Nodes: {len(nodes)} | SSH user: {args.user} | identity: {identity or ("SSH agent" if agent else "SSH configuration" if not missing_auth else "NONE")}')
    if missing_auth:
        print('[WARN] No outgoing SSH private key or loaded agent was found. authorized_keys only allows incoming logins.')
        print('       On each remote node run sudo ./01-prepare-node.sh --pair, then paste its code into --check, or provide --identity FILE.')
        print('       The local node will be checked directly; remote nodes will be marked unverified.')
    sudo_password = getpass.getpass('Shared remote sudo password (not saved): ') if args.sudo and not missing_auth else None
    local_name, local_addresses, _ = local_identity()
    records = {}
    failures = 0
    unverified = 0
    with tempfile.NamedTemporaryFile(prefix='atlas-kubekit-audit-known-hosts-') as kh:
        pins = invoking_home() / '.k8s-deploy' / 'audit-known_hosts'
        if pins.is_file():
            kh.write(pins.read_bytes())
            kh.flush()
        for name, ip in nodes:
            print(f'\n[{name} {ip}]')
            if name == local_name and ip in local_addresses:
                print('  INFO  Collecting this node locally')
                d, error = local_collect(args.user)
            elif missing_auth:
                print('  UNVERIFIED remote access: complete one-time pairing or provide a private key')
                continue
            else:
                d, error = ssh_collect(name, ip, args.user, identity, kh.name, sudo_password)
            if error:
                print(f'  FAIL  Node collection: {error}')
                print('        Repair: verify routing, SSH pairing/identity, and sudo credentials if --sudo was used.')
                failures += 1
                continue
            records[name] = d
            role = 'lb' if name.startswith('lb') else ('cp' if name.startswith('cp') else 'worker')
            checks = [
                ('hostname', d['hostname'] == name, f"reported {d['hostname'] or 'unknown'}", 'set hostname to match lbN/cpN/wN, then verify /etc/hostname and cloud-init settings'),
                ('registered IP', ip in d['ips'].split(','), f"active IPv4: {d['ips'] or 'unavailable'}", 'correct the inventory or Phase 1 network configuration so the registered IP is assigned to this node'),
                ('Ubuntu release', 'Ubuntu 24.04' in d['os'], d['os'] or 'unavailable', 'reinstall or upgrade through the supported Ubuntu 24.04 preparation path'),
                ('architecture', d['arch'] == 'amd64', d['arch'] or 'unavailable', 'use Ubuntu amd64 image'),
                ('node.conf', bool(d['manifest']), 'present' if d['manifest'] else 'missing', 'on an existing cluster use 02 --repair --items adopt after verifying the baseline; --config-only requires completed Phase 1 state'),
                ('machine-id', bool(d['machine_id']), 'present' if d['machine_id'] else 'unavailable', 'repair only if duplicated; on joined clusters coordinate before changing'),
                ('primary network MAC', bool(d['primary_mac']), d['primary_mac'] or 'unavailable', 'ensure the node has a unique virtual NIC MAC in the hypervisor'),
                ('SSH host key', bool(d['hostkey']), d['hostkey'] or 'unavailable', 'regenerate cloned SSH host keys and refresh known_hosts on peers/clients'),
                ('node SSH key pair', bool(d['nodekey']) and d['node_pair'] == 'yes', d['nodekey'] or 'missing or private/public key mismatch', 'use 02 --repair --items node-keys to establish unique keys and public-key trust'),
                ('passwordless sudo', d['sudo'] == 'yes', 'available' if d['sudo'] == 'yes' else 'unavailable', 'complete Phase 1 sudoers setup and validate with sudo -n true'),
            ]
            for label, ok, detail, repair in checks:
                status = 'PASS' if ok else 'FAIL'
                print(f'  {status:4}  {label}: {detail}')
                if not ok:
                    print(f'        Repair: {repair}')
                    failures += 1
            if d['privileged'] == 'yes':
                ready_ok = d['ready'] == 'yes'
                print(f"  {'PASS' if ready_ok else 'FAIL':4}  Phase 1 ready marker: {'present' if ready_ok else 'missing'}")
                if not ready_ok:
                    print('        Repair: new nodes must complete Phase 1; existing clusters can use 02 --repair --items adopt after all baseline prerequisites pass.')
                    failures += 1
            else:
                print('  UNVERIFIED Phase 1 ready marker: root access unavailable; use --sudo if needed')
                unverified += 1
            if not d['product_uuid']:
                print('  UNVERIFIED product_uuid: unreadable to the SSH user; root access is needed to confirm uniqueness')
                unverified += 1
            try:
                manifest = dict(line.split('=', 1) for line in d['manifest'].splitlines() if '=' in line)
            except Exception:
                manifest = {}
            expected_role = {'lb': 'lb', 'cp': 'cp', 'worker': 'worker'}[role]
            if d['manifest']:
                manifest_checks = [
                    ('manifest hostname/IP/role', manifest.get('NAME') == name and manifest.get('IP') == ip and manifest.get('ROLE') == expected_role,
                     f"NAME={manifest.get('NAME','?')} IP={manifest.get('IP','?')} ROLE={manifest.get('ROLE','?')}",
                     'rerun Phase 1 with the correct node identity/IP, then regenerate node.conf'),
                    ('manifest node SSH public key', bool(d['node_pub']) and manifest.get('NODE_PUBLIC_KEY', '') == d['node_pub'],
                     'matches the installed node key' if d['node_pub'] and manifest.get('NODE_PUBLIC_KEY', '') == d['node_pub'] else 'missing or differs from id_ed25519.pub',
                     'update the node key and regenerate node.conf before establishing the cluster SSH mesh'),
                ]
                for label, ok, detail, repair in manifest_checks:
                    status = 'PASS' if ok else 'FAIL'
                    print(f'  {status:4}  {label}: {detail}')
                    if not ok:
                        print(f'        Repair: {repair}')
                        failures += 1
            else:
                print('  SKIP  node.conf field checks: manifest is missing (reported above)')
            if d['sshd'] == 'UNVERIFIED_SUDO':
                print('  UNVERIFIED SSH hardening: root access is required to read the effective sshd configuration')
                unverified += 1
            else:
                sshd_ok = all(setting in d['sshd'].split(',') for setting in ('passwordauthentication no', 'kbdinteractiveauthentication no', 'permitrootlogin no'))
                print(f"  {'PASS' if sshd_ok else 'FAIL':4}  SSH hardening: {d['sshd'] or 'effective settings unavailable'}")
                if not sshd_ok:
                    print('        Repair: restore the Phase 1 sshd drop-in, run sshd -t, and confirm the effective settings before disconnecting.')
                    failures += 1
            if role == 'lb':
                role_checks = [
                    ('LB services active', d['haproxy'] == 'active' and d['keepalived'] == 'active', f"haproxy={d['haproxy'] or '?'} keepalived={d['keepalived'] or '?'}", 'repair the LB setup, then verify VIP/backend health'),
                ]
            else:
                role_checks = [
                    ('containerd active', d['containerd'] == 'active', d['containerd'] or 'not installed', 'rerun 02 base node setup and inspect containerd'),
                    ('kubelet active', d['kubelet'] == 'active', d['kubelet'] or 'not installed', 'rerun 02 Kubernetes node setup and inspect kubelet'),
                    ('Kubernetes v1.36.5', 'v1.36.5' in d['kubelet_version'], d['kubelet_version'] or 'not installed', 'align kubelet with the v0.1.0 Kubernetes version and inspect package pins'),
                    ('containerd systemd cgroup', bool(d['cgroup']), 'SystemdCgroup=true' if d['cgroup'] else 'not verified', 'repair /etc/containerd/config.toml and restart containerd in a maintenance window'),
                    ('Kubernetes kernel setup', d['sysctl'] == 'yes' and d['modules'] == 'yes',
                     'sysctl and modules are ready' if d['sysctl'] == 'yes' and d['modules'] == 'yes' else f"sysctl={d['sysctl']} modules={d['modules']}",
                     'rerun the 02 base node setup stage and verify required kernel modules/sysctl values'),
                ]
            for label, ok, detail, repair in role_checks:
                status = 'PASS' if ok else 'FAIL'
                print(f'  {status:4}  {label}: {detail}')
                if not ok:
                    print(f'        Repair: {repair}')
                    failures += 1
            if d['swap'] and d['swap'].isdigit() and int(d['swap']) > 0 and role != 'lb':
                print(f'  FAIL  swap: {d["swap"]} active swap device(s); disable swap and make it persistent')
                failures += 1
            if role != 'lb':
                storage = d.get('storage', {})
                if storage.get('error') or not storage:
                    print(f'  UNVERIFIED storage: {storage.get("error", "no storage report")}')
                    unverified += 1
                elif storage['assigned']:
                    for volume in storage['assigned']:
                        ok = volume['writable'] and volume.get('free', 0) >= 10 * 1024**3
                        print(f'  {"PASS" if ok else "FAIL":4}  storage: {volume["component"]} {volume["path"]} -> {volume["device"]} via {volume["mount"]}; free={volume.get("free",0)//1024**3} GiB; preserve current layout')
                        if not ok:
                            failures += 1
                            print('        Repair: restore a writable mount and at least 10 GiB free; do not replace or format an existing data filesystem.')
                else:
                    print('  INFO  No dedicated component filesystem detected; system storage is allowed.')
                for volume in storage.get('mounted', []):
                    print(f'  INFO  Unassigned independent storage: {volume["device"]} -> {volume["mount"]}; deployment must ask before using it.')

    for key, label in [('hostkey', 'SSH host key'), ('nodekey', 'node SSH public key'), ('machine_id', 'machine-id'), ('product_uuid', 'product_uuid'), ('primary_mac', 'primary network MAC')]:
        groups = defaultdict(list)
        for name, d in records.items():
            value = d.get(key, '').strip()
            if value and value not in ('unavailable', 'None'):
                groups[value].append(name)
        for value, names in groups.items():
            if len(names) > 1:
                print(f'\nFAIL  duplicate {label}: {", ".join(names)}')
                if key == 'hostkey':
                    print('      Repair: use 02 --repair --items host-keys with explicit rotation confirmation; refresh external clients using the printed fingerprints. Do not run full 02 deployment.')
                elif key == 'machine_id':
                    print('      Repair: changing machine-id on a joined cluster needs a maintenance plan; do not auto-regenerate it.')
                elif key == 'product_uuid':
                    print('      Repair: assign unique virtual hardware UUIDs in the hypervisor, then validate etcd/node identity.')
                else:
                    print('      Repair: regenerate a unique node SSH key, update its manifest, then redistribute public keys.')
                failures += 1
    fingerprints = {name: d.get('nodekey', '') for name, d in records.items() if d.get('nodekey')}
    all_fingerprints = set(fingerprints.values())
    for name, d in records.items():
        auth = set(filter(None, d.get('authorized', '').split(',')))
        missing = sorted(all_fingerprints - auth)
        if missing:
            print(f'\nFAIL  {name} SSH authorized_keys: missing {len(missing)} registered node key(s)')
            print('      Repair: append the missing node public keys to authorized_keys, then validate SSH access by hostname in both directions.')
            failures += 1
        missing_hosts = [(peer, ip) for peer, ip in nodes
                         if not re.search(rf'^\s*{re.escape(ip)}\s+[^#\n]*\b{re.escape(peer)}\b', d.get('hosts', ''), re.M)]
        if missing_hosts:
            mappings = ', '.join(f'{peer}={ip}' for peer, ip in missing_hosts)
            print(f'\nFAIL  {name} /etc/hosts: {len(missing_hosts)} mapping(s) missing or incorrect: {mappings}')
            print('      Repair: restore these name-to-IP entries in /etc/hosts on every node; do not rerun cluster installation only for this item.')
            failures += len(missing_hosts)
    if len(records) != len(nodes):
        unverified += len(nodes)-len(records)
        print(f'\nUNVERIFIED {len(nodes)-len(records)} node(s) could not be audited over SSH.')
    print(f'\nAudit complete: {failures} finding(s), {unverified} unverified item(s); no node configuration was changed.')
    print('Use --sudo to read privileged settings when the SSH account does not have passwordless sudo.')
    return 1 if failures or unverified else 0


if __name__ == '__main__':
    raise SystemExit(main())
