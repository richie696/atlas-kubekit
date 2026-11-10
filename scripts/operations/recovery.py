# SPDX-License-Identifier: MIT
"""Recover verified data into a new isolated tree; never restore in place."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import re

from . import backup
from . import etcd_tools
from .backup_verification import open_verified_backup
from .models import OperationError, fingerprint
from .reporting import atomic_json, private_directory


ISOLATED_PEER = "https://127.0.0.1:12380"


def restore(
    directory,
    identity,
    destination,
    *,
    expected_source,
    confirmation,
    bump_revision=1000000000,
    require_etcd=False,
):
    backup.require_root()
    if confirmation != "RESTORE ISOLATED":
        raise OperationError("ops.error.confirm")
    if (
        not isinstance(expected_source, str)
        or len(expected_source) != 64
        or any(c not in "0123456789abcdef" for c in expected_source)
    ):
        raise OperationError("ops.restore.source")
    if type(bump_revision) is not int or not 1 <= bump_revision <= 2**62:
        raise OperationError("ops.restore.version")
    target = Path(destination).expanduser().absolute()
    # Reuse the original protected-path and nonexistence checks before creating anything.
    backup.restore_plan({"scope": "configuration"}, {}, target)
    parent = private_directory(target.parent)
    with backup.operation_lock(), open_verified_backup(directory, identity) as (manifest, archive):
        if require_etcd and manifest["scope"] != "control-plane":
            raise OperationError("ops.restore.rehearsal_scope")
        if manifest.get("sourceFingerprint") != expected_source:
            raise OperationError("ops.restore.source")
        backup.restore_plan(manifest, {}, target)
        expected = sum(r["size"] for r in manifest["files"])
        if shutil.disk_usage(parent).free < max(512 * 1024**2, expected * 3):
            raise OperationError("ops.error.space")
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=parent) as temporary:
            work = Path(temporary)
            publish = work / "publish"
            publish.mkdir(mode=0o700)
            for record in manifest["files"]:
                relative = PurePosixPath(record["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise OperationError("ops.error.integrity")
                name = (
                    record["path"]
                    if record["path"] == "snapshot.db"
                    else "payload/" + record["path"]
                )
                output = (
                    publish / "snapshot.db"
                    if record["path"] == "snapshot.db"
                    else publish / "configuration" / relative
                )
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                digest = hashlib.sha256()
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream, archive.extractfile(name) as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if (
                    digest.hexdigest() != record["sha256"]
                    or output.stat().st_size != record["size"]
                ):
                    raise OperationError("ops.error.integrity")
            verification = {
                "configuration": True,
                "etcdRestored": False,
                "etcdServing": False,
                "volumes": False,
                "applications": False,
            }
            if manifest["scope"] == "control-plane":
                restore_tool = etcd_tools.for_backup("etcdutl", manifest) or "etcdutl"
                tool = backup.run_tool([restore_tool, "version"])
                # The snapshot's recorded version must be known before a restore command.
                expected_version = manifest.get("etcdVersion")
                match = re.search(r"^etcdutl version: ([0-9]+\.[0-9]+\.[0-9]+)$", tool, re.M)
                if not expected_version or not match or match[1] != expected_version:
                    raise OperationError("ops.restore.version")
                status = json.loads(
                    backup.run_tool(
                        [
                            restore_tool,
                            "snapshot",
                            "status",
                            str(publish / "snapshot.db"),
                            "--write-out=json",
                        ]
                    )
                )
                if status.get("revision") != manifest.get("etcdStatus", {}).get("revision"):
                    raise OperationError("ops.error.integrity")
                backup.run_tool(
                    [
                        restore_tool,
                        "snapshot",
                        "restore",
                        str(publish / "snapshot.db"),
                        "--name=isolated-recovery",
                        "--data-dir=" + str(publish / "etcd"),
                        "--initial-cluster=isolated-recovery=" + ISOLATED_PEER,
                        "--initial-advertise-peer-urls=" + ISOLATED_PEER,
                        "--bump-revision=" + str(bump_revision),
                        "--mark-compacted",
                    ]
                )
                if not (publish / "etcd/member/snap/db").is_file():
                    raise OperationError("ops.error.integrity")
                verification.update(etcdRestored=True, bumpRevision=bump_revision)
            result = dict(
                schemaVersion=1,
                operation="isolated-restore",
                sourceFingerprint=expected_source,
                backupRunId=manifest["runId"],
                verification=verification,
                files=len(manifest["files"]),
                manifestFingerprint=fingerprint(manifest),
            )
            result["dependencies"] = {
                key: manifest.get(key)
                for key in ("etcdVersion", "etcdToolSource", "clusterIdentity")
            }
            atomic_json(publish / "restore.json", result)
            # rename is atomic; recheck and use an empty target reservation to avoid replacement races.
            if target.exists() or target.is_symlink():
                raise OperationError("ops.error.path")
            target.mkdir(mode=0o700)
            try:
                os.rename(publish, target)
            except BaseException:
                try:
                    target.rmdir()
                except OSError:
                    pass
                raise
            return result
