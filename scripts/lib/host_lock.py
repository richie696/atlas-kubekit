# SPDX-License-Identifier: MIT
"""Root-owned host mutex and persistent reservations across LB transaction RPCs."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile

DIRECTORY = Path("/run/atlas-kubekit-operations")
RESERVATION_DIRECTORY = Path("/var/lib/atlas-kubekit/host-operations")


class HostLockError(RuntimeError):
    pass


def token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9-]{16,80}", value):
        raise HostLockError("ops.change.invalid_parameters")
    return value


def _directory(path, create=True):
    if not create and not path.exists() and not path.is_symlink():
        return None
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise HostLockError("ops.error.path")
    if not path.exists() and not create:
        return None
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for item in reversed(missing):
        item.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise HostLockError("ops.error.path")
    return path


def directory():
    return _directory(DIRECTORY)


def _files():
    return [
        parent / "reservation.json"
        for root in (DIRECTORY, RESERVATION_DIRECTORY)
        if (parent := _directory(root, False)) is not None
        and ((parent / "reservation.json").exists() or (parent / "reservation.json").is_symlink())
    ]


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_reservation(file):
    if file.is_symlink():
        raise HostLockError("ops.error.path")
    if not file.exists():
        return None
    info = file.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or info.st_size > 2048
    ):
        raise HostLockError("ops.error.path")
    try:
        value = json.loads(file.read_text())
        if (
            set(value) != {"schemaVersion", "owner"}
            or type(value["schemaVersion"]) is not int
            or value["schemaVersion"] != 1
        ):
            raise ValueError()
        return token(value["owner"])
    except (ValueError, TypeError, KeyError):
        raise HostLockError("ops.error.path") from None


def reservation():
    owners = {_read_reservation(file) for file in _files()}
    if len(owners) > 1:
        raise HostLockError("ops.host.reserved")
    return next(iter(owners)) if owners else None


@contextmanager
def host_operation(owner=None):
    if owner is not None:
        token(owner)
    file = directory() / "operation.lock"
    fd = os.open(file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise HostLockError("ops.error.path")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HostLockError("ops.error.locked") from None
        active = reservation()
        if active is not None and active != owner:
            raise HostLockError("ops.host.reserved")
        yield fd
    finally:
        os.close(fd)


def reserve_operation(owner):
    """Caller must hold host_operation(owner); reservation has no automatic expiry."""
    token(owner)
    current = reservation()
    if current not in (None, owner):
        raise HostLockError("ops.host.reserved")
    if current == owner:
        return
    target = _directory(RESERVATION_DIRECTORY)
    fd, name = tempfile.mkstemp(prefix=".reservation-", dir=target)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(dict(schemaVersion=1, owner=owner), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target / "reservation.json")
        _sync(target)
    finally:
        Path(name).unlink(missing_ok=True)


def release_operation(owner):
    """Caller must hold host_operation(owner); never release a foreign owner."""
    if reservation() != token(owner):
        raise HostLockError("ops.host.reserved")
    for file in _files():
        file.unlink()
        _sync(file.parent)
