# SPDX-License-Identifier: MIT
"""Collect independent, bounded API views and publish a private diagnostic archive."""
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile

from ..models import Finding, OperationError
from ..reporting import atomic_json, private_directory
from .views import collection

KINDS = (
    "node",
    "pod",
    "deployment",
    "daemonset",
    "statefulset",
    "pdb",
    "persistentvolume",
    "persistentvolumeclaim",
    "event",
)
MAX_BYTES = 16 * 1024 * 1024


def collect(client):
    result = {}
    findings = []
    for kind in KINDS:
        try:
            result[kind] = collection(kind, client.get(kind))
            findings.append(
                Finding("diagnostics-" + kind, "PASS", "ops.diagnostics.collected", (kind,))
            )
        except (OperationError, KeyError, TypeError, ValueError):
            result[kind] = None
            findings.append(
                Finding("diagnostics-" + kind, "UNVERIFIED", "ops.diagnostics.unverified", (kind,))
            )
    return result, tuple(findings)


def publish(output, run_id, version, created_at, client):
    base = private_directory(output)
    target = base / ("diagnostics-" + run_id)
    if target.exists():
        raise OperationError("ops.error.path")
    cluster_identity = client.cluster_identity()
    views, findings = collect(client)
    manifest = dict(
        schemaVersion=1,
        runId=run_id,
        toolVersion=version,
        createdAt=created_at,
        clusterIdentity=cluster_identity,
        files=[],
        omissions=[
            "secrets",
            "configmaps",
            "annotations",
            "environment",
            "logs",
            "event-messages",
            "kubeconfig",
            "private-keys",
        ],
    )
    with tempfile.TemporaryDirectory(prefix=".diagnostics-", dir=base) as temporary:
        work = Path(temporary)
        payload = work / "diagnostics.tar.gz"
        total = 0
        with tarfile.open(payload, "w:gz") as archive:
            for kind, rows in views.items():
                raw = json.dumps(dict(kind=kind, items=rows), sort_keys=True).encode()
                total += len(raw)
                if total > MAX_BYTES:
                    raise OperationError("ops.error.limit")
                name = kind + ".json"
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(raw), 0o600
                archive.addfile(info, io.BytesIO(raw))
                manifest["files"].append(
                    dict(name=name, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                )
        payload.chmod(0o600)
        manifest["payload"] = dict(
            name=payload.name,
            size=payload.stat().st_size,
            sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),
        )
        atomic_json(work / "manifest.json", manifest)
        # New destination reservation prevents replacing another run's artifacts.
        target.mkdir(mode=0o700)
        try:
            os.rename(work, target)
        except BaseException:
            target.rmdir()
            raise
    return target, findings
