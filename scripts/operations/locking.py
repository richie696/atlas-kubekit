# SPDX-License-Identifier: MIT
"""Filesystem locks for explicitly scoped single-coordinator operations."""
from contextlib import contextmanager
import fcntl
import os
import re
import stat

from .models import OperationError
from .reporting import private_directory


@contextmanager
def exclusive_lock(directory, name):
    if not re.fullmatch(r"[a-z0-9-]{1,80}", name):
        raise OperationError("ops.error.path")
    directory = private_directory(directory)
    fd = os.open(directory / (name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (
            info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or not stat.S_ISREG(info.st_mode)
        ):
            raise OperationError("ops.error.path")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OperationError("ops.error.locked") from None
        yield
    finally:
        os.close(fd)
