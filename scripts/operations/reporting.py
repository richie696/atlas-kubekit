# SPDX-License-Identifier: MIT
"""Versioned, private operation reports and atomic filesystem publication."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import stat
import tempfile

from .models import OperationError


def private_directory(path):
    path = Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise OperationError("ops.error.path")
    # parents=True uses default permissions for intermediate directories.
    # A first plan may create output/plans before its report creates output.
    missing = []
    candidate = path
    while not candidate.exists():
        missing.append(candidate)
        candidate = candidate.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        created = directory.stat()
        if (
            directory.is_symlink()
            or not stat.S_ISDIR(created.st_mode)
            or created.st_uid != os.geteuid()
            or stat.S_IMODE(created.st_mode) & 0o077
        ):
            raise OperationError("ops.error.private_directory")
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise OperationError("ops.error.private_directory")
    return path


def atomic_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        tmp = Path(stream.name)
        try:
            os.chmod(tmp, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_report(
    output,
    *,
    run_id,
    version,
    created_at,
    target,
    findings,
    translate,
    plan=None,
    operation="report",
    execution=None,
):
    base = private_directory(output)
    directory = base / run_id
    directory.mkdir(mode=0o700)  # An existing run is never overwritten.
    data = dict(
        schemaVersion=1,
        runId=run_id,
        toolVersion=version,
        createdAt=created_at,
        target=target,
        operation=operation,
        findings=[asdict(f) for f in findings],
        result=(
            "failed"
            if any(f.status == "FAIL" for f in findings)
            else (
                "attention"
                if any(f.status in ("WARN", "UNVERIFIED") for f in findings)
                else "passed"
            )
        ),
    )
    if plan is not None:
        data["plan"] = plan.record()
        completed = {"backup-create", "config-import", "config-export", "config-template"}
        data["planExecution"] = (
            "completed"
            if plan.operation == operation and operation in completed
            else "not-executed"
        )
    if execution is not None:
        data["execution"] = execution
        if execution.get("planId"):
            data["planExecution"] = (
                "completed"
                if execution.get("state") in ("succeeded", "already-succeeded")
                else "not-executed"
            )
    atomic_json(directory / "report.json", data)
    rows = [
        translate("ops.report.title"),
        f"runId: {run_id}",
        f"version: {version}",
        f"target: {target}",
    ]
    for f in findings:
        rows.append(f"[{f.status}] {f.check}: " + translate(f.message_key, *f.values))
    if plan is not None:
        rows += [
            translate(
                "ops.report.plan_summary", plan.operation, plan.scope or "-", len(plan.resources)
            ),
            "actions: " + ", ".join(plan.actions),
        ]
        rows += [name + ": " + value for name, value in plan.parameters]
        rows += list(plan.resources)
    text = directory / "report.txt"
    fd = os.open(text, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(rows) + "\n")
    return directory, data
