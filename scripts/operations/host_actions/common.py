# SPDX-License-Identifier: MIT
"""Host-owned IO: fixed roots, private journals and quiet command failures."""
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from ..models import OperationError
from ..reporting import atomic_json, private_directory

LOCK_DIRECTORY = Path("/run/atlas-kubekit-operations")
JOURNAL_DIRECTORY = Path("/var/lib/atlas-kubekit/operations")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read_owned(path, limit=4 * 1024 * 1024):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise OperationError("ops.error.path")
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or info.st_size > limit
    ):
        raise OperationError("ops.error.path")
    return path.read_bytes()


def replace(path, data, mode=0o600):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise OperationError("ops.error.path")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        candidate = Path(stream.name)
        try:
            os.chmod(candidate, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
    try:
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def run(args, timeout=120, check=True, input=None):
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                args,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                **({"stdin": subprocess.DEVNULL} if input is None else {"input": input}),
            )
        except (OSError, subprocess.TimeoutExpired):
            raise OperationError("ops.host.command", Path(args[0]).name) from None
        if check and result.returncode:
            raise OperationError("ops.host.command", Path(args[0]).name)
        if output.tell() > 1024 * 1024:
            raise OperationError("ops.error.limit")
        output.seek(0)
        return result.returncode, output.read().decode("utf-8", errors="replace")


def journal(run_id, capability):
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{64}", run_id):
        raise OperationError("ops.change.invalid_parameters")
    directory = private_directory(JOURNAL_DIRECTORY) / run_id
    directory.mkdir(mode=0o700)
    atomic_json(
        directory / "state.json", dict(schemaVersion=1, capability=capability, state="prepared")
    )
    return directory


def record(directory, capability, state, **values):
    atomic_json(
        directory / "state.json",
        dict(schemaVersion=1, capability=capability, state=state, **values),
    )
