# SPDX-License-Identifier: MIT
"""Verify complete material before copying to an owned immutable installation."""
import json
import os
from pathlib import Path
import shutil
import tempfile

from ..host_actions import common
from ..locking import exclusive_lock
from ..models import OperationError
from . import schema
from .verification import payloads
from .builder import require_platform

DESTINATION = Path("/opt/atlas-kubekit/materials")
STATE = Path("/var/lib/k8s-deploy/materials.json")


def owned_directory(path, mode, *, create=True):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise OperationError("ops.error.path")
    if not create and not path.is_dir():
        raise OperationError("ops.error.path")
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        item.mkdir(mode=mode)
    info = path.stat()
    if not path.is_dir() or info.st_uid != 0 or info.st_mode & 0o022:
        raise OperationError("ops.error.path")
    return path


def activate(directory, expected):
    require_platform()
    manifest = schema.verify(directory, expected)
    payloads(directory, manifest, signature=True)
    if not isinstance(expected, str) or expected != manifest["id"]:
        raise OperationError("ops.error.confirm")
    with exclusive_lock("/run/atlas-kubekit-materials", "import"):
        if STATE.exists():
            current = json.loads(common.read_owned(STATE))
            if current.get("id") != expected:
                raise OperationError("ops.config.conflict")
        base = owned_directory(DESTINATION, 0o755)
        target = base / expected
        if target.exists():
            schema.verify(target, expected)
        else:
            with tempfile.TemporaryDirectory(prefix=".material-import-", dir=base) as temporary:
                work = Path(temporary)
                for item in manifest["artifacts"]:
                    file = work / item["path"]
                    file.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    shutil.copyfile(Path(directory) / item["path"], file)
                    file.chmod(0o644)
                shutil.copyfile(
                    Path(directory) / "materials.lock.json", work / "materials.lock.json"
                )
                (work / "materials.lock.json").chmod(0o644)
                schema.verify(work, expected)
                work.chmod(0o755)
                target.mkdir(mode=0o755)
                try:
                    os.rename(work, target)
                except BaseException:
                    target.rmdir()
                    raise
        owned_directory(STATE.parent, 0o700)
        # Install only our Python runtime; phase one can now use offline APT even
        # before phase two has copied its installed modules.
        from ..deployment_runtime import install

        install()
        common.replace(
            STATE, json.dumps(dict(schemaVersion=1, id=expected, root=str(target))).encode()
        )
    return target, manifest
