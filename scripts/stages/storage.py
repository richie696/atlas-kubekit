#!/usr/bin/env python3
"""Discover existing Kubernetes storage and select disks without guessing names."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def tr(key, *values):
    """Resolve a user-facing storage message from the shared locale catalogs."""
    locale = os.environ.get('ATLAS_LANG', 'en')
    roots = []
    if os.environ.get('ATLAS_LOCALES_DIR'):
        roots.append(Path(os.environ['ATLAS_LOCALES_DIR']))
    roots.extend((Path(__file__).resolve().parents[2] / 'locales', Path('/usr/local/lib/k8s-deploy/locales')))
    message = None
    for root in roots:
        for code in (locale, 'en'):
            normalized = code.replace('-', '_').casefold()
            path = next((candidate for candidate in root.glob('*.json')
                         if candidate.stem.replace('-', '_').casefold() == normalized), root / f'{code}.json')
            try:
                message = json.loads(path.read_text(encoding='utf-8'))['messages'].get(key)
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                continue
            if message is not None:
                break
        if message is not None:
            break
    if message is None:
        message = key
    return message % values if values else message


def command(*args):
    r = subprocess.run(args, text=True, capture_output=True, timeout=15)
    if r.returncode:
        raise ValueError(tr('storage.error.command', args[0], r.stderr.strip()))
    return r.stdout


def component_paths(role):
    paths = {'containerd': '/var/lib/containerd', 'kubelet': '/var/lib/kubelet'}
    config = Path('/etc/containerd/config.toml')
    if config.exists():
        top = config.read_text().split('[', 1)[0]
        m = re.search(r'^\s*root\s*=\s*[\"\'](/[^\"\'\n]+)', top, re.M)
        if m:
            paths['containerd'] = m.group(1)
    files = [Path('/var/lib/kubelet/kubeadm-flags.env'), Path('/etc/default/kubelet')]
    files += list(Path('/etc/systemd/system/kubelet.service.d').glob('*.conf'))
    for config in files:
        if config.exists():
            m = re.search(r'--root-dir(?:=|\s+)(/[^\s\"\']+)', config.read_text())
            if m:
                paths['kubelet'] = m.group(1)
    if role == 'cp':
        paths['etcd'] = '/var/lib/etcd'
        manifest = Path('/etc/kubernetes/manifests/etcd.yaml')
        if manifest.exists():
            m = re.search(r'--data-dir(?:=|\s+)(/[^\s\"\']+)', manifest.read_text())
            if m:
                paths['etcd'] = m.group(1)
    if role == 'worker':
        paths['worker-data'] = '/os_data'
    return paths


def classify(blocks, mounts, paths, existing_paths):
    """Use the complete ancestor graph, including partitions, LVM and RAID."""
    devices, parents, by_id = {}, {}, {}

    def walk(entries, parent=None):
        for entry in entries:
            name = entry['name']
            devices[name] = entry
            parents.setdefault(name, set())
            if parent:
                parents[name].add(parent)
            if entry.get('pkname'):
                parents[name].add(entry['pkname'])
            if entry.get('maj:min'):
                by_id[entry['maj:min']] = name
            walk(entry.get('children', []), name)

    walk(blocks)

    def ancestors(name, seen=None):
        seen = set() if seen is None else seen
        if name in seen:
            return set()
        seen.add(name)
        if devices.get(name, {}).get('type') == 'disk':
            return {name}
        result = set()
        for parent in parents.get(name, ()):
            result.update(ancestors(parent, seen))
        return result

    def entry_for(mount):
        name = by_id.get(mount.get('maj:min'))
        if not name:
            name = mount.get('source', '').split('[', 1)[0]
        disks = ancestors(name)
        options = mount.get('options', '').split(',')
        return dict(device=name, mount=mount['target'], fstype=mount.get('fstype', ''),
                    fsroot=mount.get('fsroot', '/'),
                    disks=sorted(disks), writable='ro' not in options,
                    independent=bool(disks) and bool(system) and disks.isdisjoint(system),
                    size=devices.get(name, {}).get('size', 0), uuid=devices.get(name, {}).get('uuid'))

    system = set()
    for m in mounts:
        if m['target'] in ('/', '/boot', '/boot/efi', '/usr'):
            name = by_id.get(m.get('maj:min'), m.get('source', '').split('[', 1)[0])
            system.update(ancestors(name))
    assigned, used_ids = [], set()
    for component, path in paths.items():
        cover = [m for m in mounts if path == m['target'] or path.startswith(m['target'].rstrip('/') + '/')]
        if not cover:
            continue
        mount = max(cover, key=lambda m: len(m['target']))
        if path not in existing_paths and path != mount['target']:
            continue
        data = entry_for(mount)
        # A component in the root filesystem does not prove a dedicated disk.
        if mount['target'] == '/' and not data['independent']:
            continue
        if not data['disks']:
            continue
        assigned.append(dict(data, component=component, path=path))
        used_ids.add(mount.get('maj:min') or data['device'])
    candidates = []
    seen = set()
    for mount in mounts:
        data = entry_for(mount)
        identity = mount.get('maj:min') or data['device']
        target = mount['target']
        if identity in used_ids or identity in seen:
            continue
        if target in ('/', '/boot', '/boot/efi', '/usr', '/var', '/etc', '/tmp') or any(target.startswith(p + '/') or target == p for p in ('/proc', '/sys', '/dev', '/run')):
            continue
        if data['independent'] and data['writable'] and data['fstype'] in ('ext4', 'xfs'):
            candidates.append(data)
            seen.add(identity)
    blank = []
    for name, entry in devices.items():
        if (entry.get('type') == 'disk' and system and name not in system and not entry.get('ro')
                and not any(name in ancestors for ancestors in parents.values()) and not entry.get('fstype')
                and not any(entry.get('mountpoints') or [])):
            blank.append(name)
    return {'system_disks': sorted(system), 'assigned': assigned, 'mounted': candidates, 'blank': sorted(blank), 'paths': paths}


def probe(role):
    blocks = json.loads(command('lsblk', '-J', '-p', '-b', '-o', 'NAME,TYPE,FSTYPE,SIZE,PKNAME,MAJ:MIN,MOUNTPOINTS,RO,UUID'))['blockdevices']
    mounts = json.loads(command('findmnt', '-J', '-l', '-o', 'TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN,FSROOT'))['filesystems']
    paths = {name: str(Path(path).resolve()) for name, path in component_paths(role).items()} if role != 'lb' else {}
    existing = {p for p in paths.values() if Path(p).exists()}
    report = classify(blocks, mounts, paths, existing)
    confirmed = []
    for name in report['blank']:
        # Filesystem, RAID and other signatures must all be absent.
        try:
            signatures = json.loads(command('wipefs', '--no-act', '--json', name)).get('signatures', [])
            if not signatures:
                confirmed.append(name)
        except ValueError:
            pass
    report['blank'] = confirmed
    for volume in report['mounted'] + report['assigned']:
        try:
            usage = shutil.disk_usage(volume['mount'])
            volume['size'], volume['free'] = usage.total, usage.free
        except OSError:
            volume['free'] = 0
    return report


def select(report, phase1=False, selected=None):
    if report['assigned']:
        if any(not volume['writable'] for volume in report['assigned']):
            raise ValueError(tr('storage.error.assigned_read_only'))
        for volume in report['assigned']:
            print(tr('storage.message.assigned', volume['component'], volume['path'], volume['device'], volume['mount']), file=sys.stderr)
        return 'existing'
    if selected:
        if selected == 'existing':
            raise ValueError(tr('storage.error.existing_plan_stale'))
        if selected.startswith('reuse:'):
            validate_reuse(report, selected[6:])
            print(tr('storage.message.reuse_confirmed', selected), file=sys.stderr)
            return selected
        if selected == 'none' or selected in report['blank']:
            return selected
        raise ValueError(tr('storage.error.disk_not_blank'))
    for volume in report['mounted']:
        print(tr('storage.message.mounted', volume['device'], volume['mount'], volume['fstype'], volume['size']//(1024**3), volume.get('free',0)//(1024**3)), file=sys.stderr)
        print(tr('storage.prompt.use_mounted'), end='', file=sys.stderr, flush=True)
        answer = sys.stdin.readline().strip().lower()
        if answer in ('yes', 'y', '是', '是的'):
            if any(char in volume['mount'] for char in '\r\n\t'):
                raise ValueError(tr('storage.error.mount_control'))
            validate_reuse(report, volume['mount'])
            return 'reuse:' + volume['mount']
        if answer not in ('', 'no', 'n', '否', '不'):
            raise ValueError(tr('storage.error.answer_yes_no'))
    default = '' if phase1 else report['blank'][0] if len(report['blank']) == 1 else 'none'
    if len(report['blank']) > 1:
        print(tr('storage.message.multiple_blank', ', '.join(report['blank'])), file=sys.stderr)
    print(tr('storage.prompt.data_disk_phase1' if phase1 else 'storage.prompt.data_disk_phase2', default), end='', file=sys.stderr, flush=True)
    choice = sys.stdin.readline().strip() or default
    if choice and choice != 'none' and choice not in report['blank']:
        raise ValueError(tr('storage.error.invalid_disk_choice'))
    return choice


def validate_reuse(report, mount):
    matches = [v for v in report['mounted'] if v['mount'] == mount]
    if len(matches) != 1:
        raise ValueError(tr('storage.error.mount_not_reusable'))
    if report['assigned']:
        raise ValueError(tr('storage.error.storage_already_assigned'))
    volume = matches[0]
    if volume.get('free', 0) < 10 * 1024**3:
        raise ValueError(tr('storage.error.free_space'))
    return volume


def fstab_escape(value):
    return value.replace('\\', '\\134').replace(' ', '\\040').replace('\t', '\\011')


def apply_reuse(role, name, mount):
    if os.geteuid() != 0:
        raise ValueError(tr('storage.error.root_required'))
    if role not in ('cp', 'worker') or not re.fullmatch(r'(?:cp|w)[1-9][0-9]*', name):
        raise ValueError(tr('storage.error.role'))
    target = Path('/var/lib/etcd' if role == 'cp' else '/os_data')
    report = probe(role)
    if report['assigned']:
        print(tr('storage.message.keep_layout'))
        return
    volume = validate_reuse(report, mount)
    if target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
        raise ValueError(tr('storage.error.target_has_data', target))
    base = Path(mount) / '.atlas-kubekit'
    source = base / name / ('etcd' if role == 'cp' else 'worker-data')
    for path in (base, base / name, source):
        if path.is_symlink():
            raise ValueError(tr('storage.error.symlink'))
    if source.exists() and (not source.is_dir() or any(source.iterdir())):
        raise ValueError(tr('storage.error.source_has_data', source))
    fstab = Path('/etc/fstab')
    current = fstab.read_text()
    entries = [line.split() for line in current.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    escaped_target, escaped_mount = fstab_escape(str(target)), fstab_escape(mount)
    if any(len(e) > 1 and e[1] == escaped_target for e in entries):
        raise ValueError(tr('storage.error.fstab_unmounted', target))
    lines = []
    if not any(len(e) > 1 and e[1] == escaped_mount for e in entries):
        if not volume['uuid'] or volume.get('fsroot', '/') != '/':
            raise ValueError(tr('storage.error.uuid_required'))
        lines.append(f"UUID={volume['uuid']} {escaped_mount} {volume['fstype']} defaults,noatime 0 {'2' if volume['fstype']=='ext4' else '0'}")
    source.mkdir(mode=0o700 if role == 'cp' else 0o755, parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    if os.stat(source).st_dev != os.stat(mount).st_dev:
        raise ValueError(tr('storage.error.wrong_filesystem'))
    lines.append(f'{fstab_escape(str(source))} {escaped_target} none bind,x-systemd.requires-mounts-for={escaped_mount} 0 0')
    backup = Path('/etc/fstab.atlas-kubekit-before-reuse')
    if not backup.exists():
        shutil.copy2(fstab, backup)
    replacement = current.rstrip('\n') + '\n' + '\n'.join(lines) + '\n'
    try:
        fstab.write_text(replacement)
        command('systemctl', 'daemon-reload')
        command('mount', str(target))
        if not os.path.samefile(target, source):
            raise ValueError(tr('storage.error.bind_verification'))
    except (ValueError, OSError):
        if not os.path.ismount(target):
            fstab.write_text(current)
            command('systemctl', 'daemon-reload')
        raise
    print(tr('storage.message.reused', volume['device'], source, target))


def check_etcd(device):
    report = probe('cp')
    volumes = [v for v in report['assigned'] if v['component'] == 'etcd']
    if device not in ('auto', 'none', 'existing') and not device.startswith(('reuse:/', '/dev/')):
        raise ValueError(tr('storage.error.plan_format'))
    if volumes:
        volume = volumes[0]
        if not volume['writable']:
            raise ValueError(tr('storage.error.etcd_read_only'))
        if device == 'none':
            raise ValueError(tr('storage.error.etcd_conflict'))
        if device.startswith('/dev/') and os.path.realpath(device) != os.path.realpath(volume['device']):
            raise ValueError(tr('storage.error.etcd_device_mismatch'))
        if device.startswith('reuse:'):
            source = Path(device[6:]) / '.atlas-kubekit' / os.uname().nodename.split('.')[0] / 'etcd'
            if not source.is_dir() or not os.path.samefile(source, volume['path']):
                raise ValueError(tr('storage.error.etcd_reuse_mismatch'))
        print(tr('storage.message.etcd_existing', volume['path'], volume['device']))
        return
    target = report['paths']['etcd']
    if os.path.ismount(target):
        raise ValueError(tr('storage.error.etcd_mount_unknown'))
    if device.startswith(('/dev/', 'reuse:')):
        raise ValueError(tr('storage.error.etcd_not_mounted'))
    if device == 'existing' and not report['assigned']:
        raise ValueError(tr('storage.error.existing_plan_stale'))
    entries = [line.split() for line in Path('/etc/fstab').read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if any(len(e) > 1 and e[1] == fstab_escape(target) for e in entries):
        raise ValueError(tr('storage.error.etcd_fstab_unmounted'))
    parent = Path(target)
    while not parent.exists():
        parent = parent.parent
    if shutil.disk_usage(parent).free < 10 * 1024**3:
        raise ValueError(tr('storage.error.free_space_etcd'))
    print(tr('storage.message.etcd_system_disk', target))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('probe', 'select', 'apply-reuse', 'check-etcd'))
    parser.add_argument('--role', choices=('cp', 'worker', 'lb'), required=True)
    parser.add_argument('--report', help='JSON probe result gathered on another node')
    parser.add_argument('--phase1', action='store_true')
    parser.add_argument('--name')
    parser.add_argument('--mount')
    parser.add_argument('--selected')
    args = parser.parse_args()
    try:
        if args.action == 'check-etcd':
            check_etcd(args.selected or 'auto')
        elif args.action == 'apply-reuse':
            if not args.name or not args.mount:
                raise ValueError(tr('storage.error.apply_arguments'))
            apply_reuse(args.role, args.name, args.mount)
        else:
            report = json.loads(args.report) if args.report else probe(args.role)
            if args.action == 'probe':
                print(json.dumps(report))
            else:
                print(select(report, args.phase1, args.selected))
    except argparse.ArgumentError as exc:
        print(f"[{tr('common.label.error')}] {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"[{tr('common.label.error')}] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
