# SPDX-License-Identifier: MIT
"""Atomic publication of LB-owned configuration files; no path comes from requests."""
import os
from pathlib import Path
import stat
import tempfile


def atomic_copy(source, destination):
    source, destination = Path(source), Path(destination)
    for path in (source, destination):
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise RuntimeError("ops.error.path")
    original = source.stat()
    if (
        not stat.S_ISREG(original.st_mode)
        or original.st_uid != os.geteuid()
        or original.st_mode & 0o022
        or original.st_size > 4 * 1024 * 1024
    ):
        raise RuntimeError("ops.error.path")
    existing = destination.stat() if destination.exists() else None
    if existing and (
        not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.geteuid()
        or existing.st_mode & 0o022
    ):
        raise RuntimeError("ops.error.path")
    parent = destination.parent.stat()
    if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
        raise RuntimeError("ops.error.path")
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=".atlas-lb-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            metadata = existing or original
            if metadata.st_uid != os.geteuid() or metadata.st_gid != os.getegid():
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode))
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
