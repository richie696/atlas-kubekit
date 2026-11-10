# SPDX-License-Identifier: MIT
"""Bounded Cilium chart input. Plans bind bytes, never executable command text."""
import base64
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import zlib

from ..models import OperationError
from scripts.lib.archive import gzip_tar,ArchiveLimitError

MAX_BYTES = 2 * 1024 * 1024


def inspect(raw, version):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise OperationError("ops.component.chart")
    try:
        metadata = None
        names = set()
        total = 0
        with gzip_tar(io.BytesIO(raw), 64 * 1024**2) as archive:
            for index, member in enumerate(archive):
                total += member.size
                if index >= 10000 or total > 64 * 1024**2 or member.name in names:
                    raise ValueError()
                names.add(member.name)
                name = PurePosixPath(member.name)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or not member.isfile()
                    and not member.isdir()
                ):
                    raise ValueError()
                if member.name != "cilium" and not member.name.startswith("cilium/"):
                    raise ValueError()
                if member.name == "cilium/Chart.yaml":
                    if member.size > 1024 * 1024:
                        raise ValueError()
                    metadata = archive.extractfile(member).read(1024 * 1024 + 1).decode("utf-8")
        if metadata is None:
            raise ValueError()
        for key, wanted in (("name", "cilium"), ("version", version), ("appVersion", version)):
            values = re.findall(r"(?m)^" + key + r":\s*([^\n]+)$", metadata)
            if len(values) != 1:
                raise ValueError()
            actual = values[0].strip().strip("\"'")
            if key == "appVersion" and actual.startswith("v"):
                actual = actual[1:]
            if actual != wanted:
                raise ValueError()
    except (ValueError, ArchiveLimitError, OSError, EOFError, KeyError, tarfile.TarError, UnicodeError, zlib.error):
        raise OperationError("ops.component.chart") from None
    return hashlib.sha256(raw).hexdigest()


def read(path, version, expected=None):
    file = Path(path).expanduser().absolute()
    if (
        any(part.is_symlink() for part in (file, *file.parents))
        or not file.is_file()
        or file.stat().st_uid != os.geteuid()
        or file.stat().st_mode & 0o022
        or file.stat().st_size > MAX_BYTES
    ):
        raise OperationError("ops.component.chart")
    with file.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    actual = inspect(raw, version)
    if expected is not None and actual != expected:
        raise OperationError("ops.error.integrity")
    return raw, actual
