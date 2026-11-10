#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Fresh-clone identity preparation. Sourcing defines functions only.

atlas_prepare_machine_id() {
  local state="$1"
  python3 - "$state" <<'PY'
import os
from pathlib import Path
import stat
import tempfile
import sys
import uuid

state = Path(sys.argv[1])
protected = ('/etc/kubernetes/admin.conf', '/etc/kubernetes/kubelet.conf', '/var/lib/etcd/member')
if os.geteuid() != 0 or (state / 'ready').exists() or any(Path(p).exists() for p in protected):
    raise SystemExit(1)
source = Path('/etc/machine-id')
if source.is_symlink() or not source.is_file() or source.stat().st_uid != 0:
    raise SystemExit(1)
if any(p.is_symlink() for p in (state, *state.parents)):
    raise SystemExit(1)
backup = state / 'machine-id-before-prepare'
if not backup.exists():
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(source.read_bytes())
new_id = (uuid.uuid4().hex + '\n').encode()
for path in (source, Path('/var/lib/dbus/machine-id')):
    if path.is_symlink() or (path != source and not path.exists()):
        continue
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o444)
            stream.write(new_id)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
PY
}
