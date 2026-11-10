# SPDX-License-Identifier: MIT
"""Verify only age ciphertext bytes. Decryption and restore are separate evidence."""
import hashlib
import os
from pathlib import Path
import stat

from ..models import OperationError


def verify(artifact):
    file = Path(artifact["path"])
    if any(p.is_symlink() for p in (file, *file.parents)):
        raise OperationError("ops.protection.private_file")
    descriptor = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o077
            or before.st_size != artifact["size"]
        ):
            raise OperationError("ops.protection.private_file")
        header = stream.read(22)
        if header != b"age-encryption.org/v1\n":
            raise OperationError("ops.protection.ciphertext")
        digest = hashlib.sha256(header)
        count = len(header)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            count += len(block)
            if count > artifact["size"]:
                raise OperationError("ops.error.integrity")
            digest.update(block)
        after = os.fstat(stream.fileno())
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or digest.hexdigest() != artifact["sha256"]
        ):
            raise OperationError("ops.error.integrity")
    return dict(integrity="ciphertext-verified", decryption="unverified", restoration="unverified")
