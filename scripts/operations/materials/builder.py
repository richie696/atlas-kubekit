# SPDX-License-Identifier: MIT
"""Publish complete material only after each independent builder succeeds."""
import json
import os
from pathlib import Path
import platform
import tempfile
import uuid

from ..configuration import normalize
from ..models import fingerprint, OperationError
from ..reporting import private_directory, atomic_json
from . import apt_repository, images, charts, schema, source_package


def require_platform():
    if (
        os.geteuid() != 0
        or platform.system() != "Linux"
        or platform.machine() not in ("x86_64", "amd64")
    ):
        raise OperationError("ops.materials.platform")
    fields = {
        row.partition("=")[0]: row.partition("=")[2].strip('"')
        for row in Path("/etc/os-release").read_text().splitlines()
        if "=" in row
    }
    if fields.get("ID") != "ubuntu" or fields.get("VERSION_ID") != "24.04":
        raise OperationError("ops.materials.platform")


def prepare(output, configuration):
    require_platform()
    config = normalize(configuration)
    base = private_directory(output)
    with tempfile.TemporaryDirectory(prefix=".material-build-", dir=base) as temporary:
        work = Path(temporary)
        payload = work / "payload"
        payload.mkdir(mode=0o700)
        logs = work / "logs"
        logs.mkdir(mode=0o700)
        try:
            artifacts = apt_repository.build(payload, config, logs / "apt.log")
            chart_artifacts = charts.collect(payload, config, logs)
            artifacts += chart_artifacts
            chart = next(
                row for row in chart_artifacts if row["kind"] == "chart" and row["name"] == "cilium"
            )
            artifacts += images.collect(payload, config, logs, chart["metadata"]["imageReferences"])
            sources = source_package.collect(payload)
            artifacts.append(sources)
            manifest = dict(
                schemaVersion=1,
                toolVersion=sources["version"],
                platform=dict(schema.PLATFORM),
                configuration=config,
                artifacts=artifacts,
            )
            manifest["id"] = fingerprint(manifest)
            atomic_json(payload / "materials.lock.json", manifest)
            schema.verify(payload, manifest["id"])
            from .verification import payloads

            payloads(payload, manifest, signature=True)
            target = base / ("materials-" + manifest["id"])
            target.mkdir(mode=0o700)
            try:
                os.rename(payload, target)
            except BaseException:
                target.rmdir()
                raise
        except BaseException:
            failed = base / ("failed-material-" + uuid.uuid4().hex)
            os.rename(work, failed)
            raise
    return target, manifest
