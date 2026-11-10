# SPDX-License-Identifier: MIT
"""Cache tools from the running node's digest-verified etcd image, without downloads."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from . import oci
from .host_actions import common
from .models import OperationError
from .reporting import atomic_json, private_directory

ROOT = Path("/var/lib/atlas-kubekit/etcd-tools")
MANIFEST = Path("/etc/kubernetes/manifests/etcd.yaml")
NAMES = ("etcd", "etcdctl", "etcdutl")


def read_record(file):
    value = json.loads(common.read_owned(file))
    if (
        not isinstance(value, dict)
        or set(value) != {"schemaVersion", "version", "image", "digest", "files"}
        or value["schemaVersion"] != 1
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value["version"])
        or set(value["files"]) != set(NAMES)
    ):
        raise OperationError("ops.backup.tools")
    oci.blob_name(value["digest"])
    return value


def current():
    file = ROOT / "current.json"
    return read_record(file) if file.exists() else None


def directory(record):
    return ROOT / (record["version"] + "-" + record["digest"].split(":")[1][:16])


def resolve(name, record=None):
    if name not in NAMES:
        raise OperationError("ops.backup.tools")
    record = current() if record is None else record
    if record is None:
        return shutil.which(name)
    path = directory(record) / name
    data = common.read_owned(path, limit=128 * 1024**2)
    if hashlib.sha256(data).hexdigest() != record["files"][name] or not os.access(path, os.X_OK):
        raise OperationError("ops.backup.tools")
    return str(path)


def for_backup(name, manifest):
    source = manifest.get("etcdToolSource")
    if source is None:
        return resolve(name)
    if (
        not isinstance(source, dict)
        or set(source) != {"version", "image", "digest"}
        or source["version"] != manifest.get("etcdVersion")
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", source["version"])
    ):
        raise OperationError("ops.backup.tools")
    oci.blob_name(source["digest"])
    record = read_record(directory(source) / "tools.json")
    if any(record[key] != source[key] for key in source):
        raise OperationError("ops.backup.tools")
    return resolve(name, record)


def ensure():
    """Caller holds the host mutation lock. Never retag or pull a container image."""
    if os.geteuid() != 0:
        raise OperationError("ops.error.root")
    text = common.read_owned(MANIFEST).decode()
    rows = re.findall(r"^\s*image:\s*([^\s]+)\s*$", text, re.M)
    if len(rows) != 1:
        raise OperationError("ops.backup.tools")
    image = rows[0]
    version = re.search(
        r":([0-9]+\.[0-9]+\.[0-9]+)(?:-[A-Za-z0-9.-]+)?(?:@sha256:[a-f0-9]{64})?$", image
    )
    if not version:
        raise OperationError("ops.backup.tools")
    _, listing = common.run(["ctr", "-n", "k8s.io", "images", "list"])
    candidates = [
        line.split()[2]
        for line in listing.splitlines()
        if len(line.split()) >= 3 and line.split()[0] == image
    ]
    if len(candidates) != 1:
        raise OperationError("ops.backup.tools")
    digest = candidates[0]
    oci.blob_name(digest)
    existing = current()
    if existing and existing["version"] == version[1] and existing["digest"] == digest:
        for name in NAMES:
            resolve(name)
        if not (directory(existing) / "tools.json").exists():
            atomic_json(directory(existing) / "tools.json", existing)
        return {key: existing[key] for key in ("version", "image", "digest")}
    root = private_directory(ROOT)
    if shutil.disk_usage(root).free < 1024**3:
        raise OperationError("ops.error.space")
    record = dict(schemaVersion=1, version=version[1], image=image, digest=digest, files={})
    destination = directory(record)
    with tempfile.TemporaryDirectory(prefix=".etcd-tools-", dir=root) as temporary:
        work = Path(temporary)
        export = work / "image.tar"
        common.run(
            [
                "ctr",
                "-n",
                "k8s.io",
                "images",
                "export",
                "--platform",
                "linux/amd64",
                str(export),
                image,
            ],
            timeout=180,
        )
        if export.stat().st_size > oci.MAX_IMAGE_BYTES:
            raise OperationError("ops.error.limit")
        wanted = ["usr/local/bin/" + name for name in NAMES]
        binaries = oci.selected_files(export, digest, wanted)
        publish = work / "tools"
        publish.mkdir(mode=0o700)
        for name in NAMES:
            raw = binaries["usr/local/bin/" + name]
            common.replace(publish / name, raw, 0o755)
            _, output = common.run(
                [str(publish / name), "--version" if name == "etcd" else "version"]
            )
            if not re.search(
                r"^" + name + r" [Vv]ersion: " + re.escape(version[1]) + r"$", output, re.M
            ):
                raise OperationError("ops.backup.tools")
            record["files"][name] = hashlib.sha256(raw).hexdigest()
        atomic_json(publish / "tools.json", record)
        if destination.exists():
            if any(resolve(name, record) is None for name in NAMES):
                raise OperationError("ops.backup.tools")
            atomic_json(destination / "tools.json", record)
        else:
            destination.mkdir(mode=0o700)
            try:
                os.rename(publish, destination)
            except BaseException:
                destination.rmdir()
                raise
        atomic_json(root / "current.json", record)
    return {key: record[key] for key in ("version", "image", "digest")}
