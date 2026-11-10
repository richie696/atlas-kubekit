# SPDX-License-Identifier: MIT
"""Verified decrypted archives, scoped to a private temporary lifetime."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile

from . import backup
from .models import OperationError
from .reporting import private_directory


def _load_object(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise OperationError("ops.error.integrity")
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise ValueError()
        return value
    except (ValueError, OSError):
        raise OperationError("ops.error.integrity") from None


def validate_archive(archive, envelope):
    members = []
    for member in archive:
        members.append(member)
        if len(members) > backup.MAX_FILES + 2:
            raise OperationError("ops.error.limit")
    names = [m.name for m in members]
    if (
        len(members) > backup.MAX_FILES + 2
        or len(set(names)) != len(names)
        or "manifest.json" not in names
        or sum(m.size for m in members) > backup.MAX_BYTES
        or any(
            not m.isfile()
            or PurePosixPath(m.name).is_absolute()
            or ".." in PurePosixPath(m.name).parts
            for m in members
        )
    ):
        raise OperationError("ops.error.integrity")
    meta = archive.getmember("manifest.json")
    if meta.size > 1024 * 1024:
        raise OperationError("ops.error.integrity")
    manifest = json.load(archive.extractfile(meta))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != 1
        or any(
            manifest.get(k) != envelope.get(k)
            for k in ("runId", "scope", "sourceFingerprint", "coverage", "clusterIdentity")
        )
        or manifest.get("scope") not in ("configuration", "control-plane")
        or not isinstance(manifest.get("files"), list)
        or not manifest["files"]
    ):
        raise OperationError("ops.error.integrity")
    expected_coverage = dict(
        configuration=True,
        etcd=manifest["scope"] == "control-plane",
        volumes=False,
        applications=False,
    )
    if manifest.get("coverage") != expected_coverage:
        raise OperationError("ops.error.integrity")
    wanted = {"manifest.json"}
    for record in manifest["files"]:
        path = record["path"]
        if (
            not isinstance(path, str)
            or not isinstance(record.get("size"), int)
            or record["size"] < 0
        ):
            raise OperationError("ops.error.integrity")
        if path == "snapshot.db" and manifest["scope"] == "control-plane":
            name = path
        elif any(path == source or path.startswith(source + "/") for source in backup.SOURCES):
            name = "payload/" + path
        else:
            raise OperationError("ops.error.integrity")
        if name in wanted:
            raise OperationError("ops.error.integrity")
        wanted.add(name)
        member = archive.getmember(name)
        if member.size != record["size"]:
            raise OperationError("ops.error.integrity")
        digest = hashlib.sha256()
        with archive.extractfile(member) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != record["sha256"]:
            raise OperationError("ops.error.integrity")
    if wanted != set(names) or (
        manifest["scope"] == "control-plane" and "snapshot.db" not in wanted
    ):
        raise OperationError("ops.error.integrity")
    return manifest


@contextmanager
def open_verified_backup(directory, identity):
    backup.require_root()
    directory = Path(directory).expanduser().absolute()
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise OperationError("ops.error.path")
    envelope = _load_object(directory / "backup.json")
    encrypted = directory / "payload.tar.age"
    payload = envelope.get("payload", {})
    if (
        payload.get("name") != encrypted.name
        or encrypted.is_symlink()
        or not encrypted.is_file()
        or encrypted.stat().st_size > backup.MAX_BYTES + 1024 * 1024
        or payload.get("size") != encrypted.stat().st_size
        or payload.get("sha256") != backup.digest_file(encrypted)
    ):
        raise OperationError("ops.error.integrity")
    key = Path(identity).expanduser().absolute()
    if (
        any(p.is_symlink() for p in (key, *key.parents))
        or not key.is_file()
        or key.stat().st_uid != os.geteuid()
        or stat.S_IMODE(key.stat().st_mode) & 0o077
    ):
        raise OperationError("ops.error.identity")
    with tempfile.TemporaryDirectory(
        prefix=".verify-", dir=private_directory(directory)
    ) as temporary:
        decrypted = Path(temporary) / "payload.tar"
        backup.run_tool(
            ["age", "--decrypt", "--identity", str(key), "--output", str(decrypted), str(encrypted)]
        )
        decrypted.chmod(0o600)
        if decrypted.stat().st_size > backup.MAX_BYTES:
            raise OperationError("ops.error.limit")
        archive = None
        try:
            archive = tarfile.open(decrypted, "r:")
            manifest = validate_archive(archive, envelope)
        except (tarfile.TarError, KeyError, TypeError, ValueError, OSError):
            if archive is not None:
                archive.close()
            raise OperationError("ops.error.integrity") from None
        try:
            yield manifest, archive
        finally:
            archive.close()
