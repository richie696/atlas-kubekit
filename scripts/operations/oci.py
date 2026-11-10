# SPDX-License-Identifier: MIT
"""Read OCI exports by digest. No archive member is extracted as a filesystem path."""
import hashlib
import json
from pathlib import PurePosixPath
import re
import tarfile
from contextlib import contextmanager
from scripts.lib.archive import indexed_tar, ArchiveFormatError, ArchiveLimitError

from .models import OperationError

MAX_IMAGE_BYTES = 2 * 1024**3


@contextmanager
def open_archive(path):
    try:
        with indexed_tar(path, MAX_IMAGE_BYTES) as archive:
            yield archive
    except ArchiveLimitError:
        raise OperationError("ops.error.limit") from None
    except ArchiveFormatError:
        raise OperationError("ops.error.integrity") from None


def blob_name(digest):
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise OperationError("ops.error.integrity")
    return "blobs/sha256/" + digest.split(":")[1]


def verified_blob(archive, digest):
    name = blob_name(digest)
    try:
        member = archive.getmember(name)
    except KeyError:
        raise OperationError("ops.error.integrity") from None
    if not member.isfile() or not 0 < member.size <= MAX_IMAGE_BYTES:
        raise OperationError("ops.error.integrity")
    result = hashlib.sha256()
    with archive.extractfile(member) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    if result.hexdigest() != digest.split(":")[1]:
        raise OperationError("ops.error.integrity")
    return name


def document(archive, digest):
    name = verified_blob(archive, digest)
    if archive.getmember(name).size > 1024 * 1024:
        raise OperationError("ops.error.limit")
    with archive.extractfile(name) as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise OperationError("ops.error.integrity")
    return value


def manifest(archive, digest):
    value = document(archive, digest)
    if "manifests" in value:
        wanted = [
            item
            for item in value["manifests"]
            if item.get("platform", {}).get("os") == "linux"
            and item.get("platform", {}).get("architecture") == "amd64"
        ]
        if len(wanted) != 1:
            raise OperationError("ops.error.integrity")
        value = document(archive, wanted[0]["digest"])
    config = document(archive, value["config"]["digest"])
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        raise OperationError("ops.error.integrity")
    return value


def selected_files(path, digest, wanted, max_file_bytes=128 * 1024**2):
    """Resolve overlays and whiteouts, returning only explicitly requested regular files."""
    result = {}
    with open_archive(path) as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if (
            len(names) > 20000
            or len(set(names)) != len(names)
            or sum(member.size for member in members) > MAX_IMAGE_BYTES
        ):
            raise OperationError("ops.error.limit")
        image = manifest(archive, digest)
        for layer in image["layers"]:
            name = verified_blob(archive, layer["digest"])
            with (
                archive.extractfile(name) as source,
                tarfile.open(fileobj=source, mode="r|*") as filesystem,
            ):
                for index, member in enumerate(filesystem):
                    if index > 100000:
                        raise OperationError("ops.error.limit")
                    relative = PurePosixPath(member.name)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise OperationError("ops.error.integrity")
                    normalized = relative.as_posix()
                    if relative.name == ".wh..wh..opq":
                        for key in list(result):
                            if str(PurePosixPath(key).parent) == str(relative.parent):
                                del result[key]
                    elif relative.name.startswith(".wh."):
                        result.pop(str(relative.with_name(relative.name[4:])), None)
                    elif normalized in wanted:
                        if not member.isfile() or not 0 < member.size <= max_file_bytes:
                            raise OperationError("ops.error.integrity")
                        with filesystem.extractfile(member) as stream:
                            raw = stream.read(max_file_bytes + 1)
                        if len(raw) != member.size:
                            raise OperationError("ops.error.integrity")
                        result[normalized] = raw
    if set(result) != set(wanted):
        raise OperationError("ops.error.integrity")
    return result
