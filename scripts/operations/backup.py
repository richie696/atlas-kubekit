# SPDX-License-Identifier: MIT
"""Encrypted local node backups. Verification never extracts into live paths."""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile

from .models import OperationError, OperationPlan, fingerprint
from .reporting import atomic_json, private_directory
from .locking import exclusive_lock
from scripts.lib.host_lock import reservation, HostLockError
from . import etcd_tools

SOURCES = (
    "etc/netplan",
    "etc/cloud/cloud.cfg.d/99-k8s-static-identity.cfg",
    "etc/modules-load.d/k8s.conf",
    "etc/sysctl.d/99-kubernetes.conf",
    "var/lib/k8s-deploy/node.conf",
    "etc/hosts",
    "etc/fstab",
    "etc/kubernetes",
    "etc/containerd",
    "etc/ssh/sshd_config",
    "etc/ssh/sshd_config.d",
    "etc/haproxy",
    "etc/keepalived",
    "etc/atlas-kubekit",
    "etc/atlas-kubekit-operations.json",
    "var/lib/k8s-deploy/materials.json",
    "etc/sysctl.d/99-atlas-kubekit-lb.conf",
    "var/lib/k8s-deploy/cluster.conf",
)
MAX_BYTES = 4 * 1024**3
MAX_FILES = 10000
LOCK_DIRECTORY = Path("/run/atlas-kubekit-operations")


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_root():
    if os.geteuid() != 0:
        raise OperationError("ops.error.root")


@contextmanager
def operation_lock():
    """Hold the host-wide mutation lock without changing an existing inode."""
    require_root()
    with exclusive_lock(LOCK_DIRECTORY, "operation"):
        try:
            if reservation() is not None:
                raise OperationError("ops.host.reserved")
        except HostLockError as error:
            raise OperationError(str(error)) from None
        yield


def run_tool(args, *, timeout=180):
    """Do not publish tool output: failures may contain private source data."""
    args = list(args)
    if args[0] in etcd_tools.NAMES:
        args[0] = etcd_tools.resolve(args[0]) or args[0]
    if not shutil.which(args[0]):
        raise OperationError("ops.error.tool", args[0])
    with tempfile.TemporaryFile() as output:
        try:
            p = subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                env={**os.environ, "ETCDCTL_API": "3"},
            )
        except subprocess.TimeoutExpired:
            raise OperationError("ops.error.timeout", args[0]) from None
        if p.returncode:
            raise OperationError("ops.error.tool_failed", args[0])
        if output.tell() > 1024 * 1024:
            raise OperationError("ops.error.tool_failed", args[0])
        output.seek(0)
        return output.read().decode("utf-8", errors="replace")


def source_inventory(output):
    """Inventory fixed configuration roots; never walk through symlinks/devices."""
    output = Path(output).absolute()
    records = []
    for relative in SOURCES:
        top = Path("/") / relative
        if any(parent.is_symlink() for parent in top.parents):
            raise OperationError("ops.error.path")
        if output == top or top in output.parents or output in top.parents:
            raise OperationError("ops.error.path")
        try:
            top.lstat()
        except FileNotFoundError:
            continue
        pending = [top]
        while pending:
            path = pending.pop()
            info = path.lstat()
            if path.is_symlink():
                raise OperationError("ops.error.path")
            if stat.S_ISDIR(info.st_mode):
                pending.extend(sorted(path.iterdir(), reverse=True))
            elif stat.S_ISREG(info.st_mode):
                records.append(
                    dict(
                        path=path.relative_to("/").as_posix(),
                        size=info.st_size,
                        mode=stat.S_IMODE(info.st_mode),
                        uid=info.st_uid,
                        gid=info.st_gid,
                        mtimeNs=info.st_mtime_ns,
                        inode=info.st_ino,
                    )
                )
            else:
                raise OperationError("ops.error.path")
            if len(records) + len(pending) > MAX_FILES:
                raise OperationError("ops.error.limit")
    if not records or sum(r["size"] for r in records) > MAX_BYTES:
        raise OperationError("ops.error.limit")
    return tuple(sorted(records, key=lambda r: r["path"]))


def backup_plan(facts, scope, records):
    if scope not in ("configuration", "control-plane"):
        raise OperationError("ops.error.scope")
    if scope == "control-plane" and facts.get("role") != "cp":
        raise OperationError("ops.error.control_plane")
    actions = ("copy-configuration", "encrypt-age", "publish-manifest")
    if scope == "control-plane":
        actions = (
            "verify-local-image-tools",
            "verify-source-cluster",
            "etcd-snapshot",
            "validate-etcd-snapshot",
        ) + actions
    return OperationPlan(
        "backup-create",
        fingerprint(dict(node=facts, files=records)),
        actions,
        scope,
        resources=tuple(r["path"] for r in records),
    )


def etcd_arguments():
    pki = Path("/etc/kubernetes/pki/etcd")
    for name in ("ca.crt", "healthcheck-client.crt", "healthcheck-client.key"):
        source = pki / name
        if not source.is_file() or source.is_symlink():
            raise OperationError("ops.error.control_plane")
    return [
        "etcdctl",
        "--endpoints=https://127.0.0.1:2379",
        "--cacert=" + str(pki / "ca.crt"),
        "--cert=" + str(pki / "healthcheck-client.crt"),
        "--key=" + str(pki / "healthcheck-client.key"),
    ]


def snapshot_size():
    try:
        rows = json.loads(run_tool(etcd_arguments() + ["endpoint", "status", "--write-out=json"]))
        size = rows[0]["Status"]["dbSize"]
        if not isinstance(size, int) or not 0 < size <= MAX_BYTES:
            raise ValueError()
        return size
    except (ValueError, TypeError, KeyError, IndexError):
        raise OperationError("ops.error.integrity") from None


def etcd_version():
    """Bind the archive to the source server, not just the installed restore utility."""
    try:
        rows = json.loads(run_tool(etcd_arguments() + ["endpoint", "status", "--write-out=json"]))
        server = rows[0]["Status"]["version"]
        if not isinstance(server, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", server):
            raise ValueError()
        for tool in ("etcdctl", "etcdutl"):
            output = run_tool([tool, "version"])
            match = re.search(r"^" + tool + r" version: ([0-9]+\.[0-9]+\.[0-9]+)$", output, re.M)
            if not match or match[1] != server:
                raise ValueError()
        return server
    except (ValueError, TypeError, KeyError, IndexError):
        raise OperationError("ops.restore.version") from None


def cluster_identity():
    value = run_tool(
        [
            "kubectl",
            "--kubeconfig=/etc/kubernetes/admin.conf",
            "--request-timeout=10s",
            "get",
            "namespace",
            "kube-system",
            "-o",
            "jsonpath={.metadata.uid}",
        ]
    ).strip()
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,128}", value):
        raise OperationError("ops.restore.cluster_identity")
    return value


def snapshot(path):
    run_tool(etcd_arguments() + ["snapshot", "save", str(path)])
    if path.stat().st_size > MAX_BYTES:
        raise OperationError("ops.error.limit")
    # etcdctl snapshot status is removed in etcd 3.6; offline checks use etcdutl.
    try:
        value = json.loads(
            run_tool(["etcdutl", "snapshot", "status", str(path), "--write-out=json"])
        )
        if (
            not isinstance(value, dict)
            or not {"hash", "revision", "totalKey", "totalSize"} <= value.keys()
        ):
            raise ValueError()
        return {k: value[k] for k in ("hash", "revision", "totalKey", "totalSize")}
    except (ValueError, TypeError):
        raise OperationError("ops.error.integrity") from None


def create_backup(output, *, run_id, version, created_at, facts, scope, recipient):
    require_root()
    if not re.fullmatch(r"age1[0-9a-z]{20,150}", recipient or ""):
        raise OperationError("ops.error.recipient")
    for tool in ("age",):
        if not shutil.which(tool):
            raise OperationError("ops.error.tool", tool)
    base = private_directory(output)
    with operation_lock():
        tool_source = etcd_tools.ensure() if scope == "control-plane" else None
        records = source_inventory(base)
        plan = backup_plan(facts, scope, records)
        source_etcd_version = etcd_version() if scope == "control-plane" else None
        source_cluster_identity = cluster_identity() if scope == "control-plane" else None
        snapshot_bytes = snapshot_size() if scope == "control-plane" else 0
        expected_bytes = sum(r["size"] for r in records) + snapshot_bytes
        if expected_bytes > MAX_BYTES:
            raise OperationError("ops.error.limit")
        if shutil.disk_usage(base).free < max(512 * 1024**2, expected_bytes * 3):
            raise OperationError("ops.error.space")
        with tempfile.TemporaryDirectory(prefix=".partial-", dir=base) as temporary:
            work = Path(temporary)
            payload = work / "payload.tar"
            manifest = dict(
                schemaVersion=1,
                runId=run_id,
                toolVersion=version,
                createdAt=created_at,
                sourceFingerprint=facts["identityFingerprint"],
                scope=scope,
                files=[],
                coverage=dict(
                    configuration=True,
                    etcd=scope == "control-plane",
                    volumes=False,
                    applications=False,
                ),
            )
            with tarfile.open(payload, "w") as archive:
                for record in records:
                    source = Path("/") / record["path"]
                    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as stream:
                        before = os.fstat(stream.fileno())
                        if (
                            before.st_ino != record["inode"]
                            or before.st_mtime_ns != record["mtimeNs"]
                            or before.st_size != record["size"]
                        ):
                            raise OperationError("ops.error.changed")
                        info = tarfile.TarInfo("payload/" + record["path"])
                        info.size, info.mode = record["size"], record["mode"]

                        # Hash the exact bytes delivered to the archive, not a second file read.
                        class HashingReader:
                            def __init__(self, source):
                                self.source, self.hash = source, hashlib.sha256()

                            def read(self, n=-1):
                                data = self.source.read(n)
                                self.hash.update(data)
                                return data

                        reader = HashingReader(stream)
                        archive.addfile(info, reader)
                        after = os.fstat(stream.fileno())
                        if (
                            after.st_mtime_ns != before.st_mtime_ns
                            or after.st_size != before.st_size
                        ):
                            raise OperationError("ops.error.changed")
                        manifest["files"].append({**record, "sha256": reader.hash.hexdigest()})
                if scope == "control-plane":
                    snap = work / "snapshot.db"
                    if etcd_version() != source_etcd_version:
                        raise OperationError("ops.error.changed")
                    manifest["etcdVersion"] = source_etcd_version
                    manifest["etcdToolSource"] = tool_source
                    manifest["clusterIdentity"] = source_cluster_identity
                    if cluster_identity() != source_cluster_identity:
                        raise OperationError("ops.error.changed")
                    manifest["etcdStatus"] = snapshot(snap)
                    info = tarfile.TarInfo("snapshot.db")
                    info.size = snap.stat().st_size
                    info.mode = 0o600
                    with snap.open("rb") as stream:
                        archive.addfile(info, stream)
                    manifest["files"].append(
                        dict(
                            path="snapshot.db", size=info.size, mode=0o600, sha256=digest_file(snap)
                        )
                    )
                    snap.unlink()
                if payload.stat().st_size > MAX_BYTES:
                    raise OperationError("ops.error.limit")
                encoded = json.dumps(manifest, sort_keys=True).encode()
                if len(encoded) > 1024 * 1024:
                    raise OperationError("ops.error.limit")
                info = tarfile.TarInfo("manifest.json")
                info.size = len(encoded)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(encoded))
            if source_inventory(base) != records:
                raise OperationError("ops.error.changed")
            publish = work / "publish"
            publish.mkdir(mode=0o700)
            encrypted = publish / "payload.tar.age"
            run_tool(
                [
                    "age",
                    "--encrypt",
                    "--recipient",
                    recipient,
                    "--output",
                    str(encrypted),
                    str(payload),
                ]
            )
            encrypted.chmod(0o600)
            envelope = dict(
                schemaVersion=1,
                runId=run_id,
                toolVersion=version,
                createdAt=created_at,
                sourceFingerprint=facts["identityFingerprint"],
                scope=scope,
                payload=dict(
                    name="payload.tar.age",
                    size=encrypted.stat().st_size,
                    sha256=digest_file(encrypted),
                ),
                coverage=manifest["coverage"],
            )
            if scope == "control-plane":
                envelope["clusterIdentity"] = source_cluster_identity
            atomic_json(publish / "backup.json", envelope)
            destination = base / ("backup-" + run_id)
            if destination.exists():
                raise OperationError("ops.error.path")
            os.rename(publish, destination)
            return destination, plan


def verify_backup(directory, identity):
    # Keep the public contract while recovery reuses the same validated archive.
    from .backup_verification import open_verified_backup

    with open_verified_backup(directory, identity) as (manifest, archive):
        return manifest


def restore_plan(manifest, facts, destination):
    target = Path(destination).expanduser().absolute()
    protected = ("/etc", "/var/lib", "/usr", "/boot", "/proc", "/sys", "/dev", "/run")
    if str(target) == "/" or any(target == Path(p) or Path(p) in target.parents for p in protected):
        raise OperationError("ops.error.path")
    # Reject live roots before filesystem access, including for read-only review.
    try:
        if target.exists() or any(p.is_symlink() for p in (target, *target.parents)):
            raise OperationError("ops.error.path")
    except OSError:
        raise OperationError("ops.error.path") from None
    return OperationPlan(
        "restore-review",
        fingerprint(dict(manifest=manifest, target=facts, destination=str(target))),
        (
            "verify-backup",
            "review-node-identity",
            "review-etcd-version",
            "review-application-coverage",
            "isolation-rehearsal-required",
        ),
        manifest["scope"],
        resources=tuple(r["path"] for r in manifest.get("files", [])),
        parameters=(
            ("isolatedDestination", str(target)),
            ("sourceFingerprint", manifest.get("sourceFingerprint", "unknown")),
        ),
    )
