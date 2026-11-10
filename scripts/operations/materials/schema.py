# SPDX-License-Identifier: MIT
"""Public content lock and bounded filesystem verification, without extraction."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from ..configuration import normalize, _unique
from ..models import fingerprint, OperationError

PLATFORM = dict(os="ubuntu", release="24.04", architecture="amd64")
MAX_FILE = 8 * 1024**3
MAX_TOTAL = 32 * 1024**3
KINDS = frozenset(("apt", "apt-index", "apt-signature", "apt-key", "image", "chart", "tool"))


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def relative(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[a-zA-Z0-9_./+@:%~-]{1,240}", value)
        or PurePosixPath(value).is_absolute()
        or ".." in PurePosixPath(value).parts
        or str(PurePosixPath(value)) != value
    ):
        raise OperationError("ops.materials.invalid")
    return value


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schemaVersion", "id", "toolVersion", "platform", "configuration", "artifacts"}
        or value["schemaVersion"] != 1
        or type(value["schemaVersion"]) is not int
        or value["platform"] != PLATFORM
        or not isinstance(value["artifacts"], list)
        or not 1 <= len(value["artifacts"]) <= 10000
    ):
        raise OperationError("ops.materials.invalid")
    if normalize(value["configuration"]) != value["configuration"]:
        raise OperationError("ops.materials.invalid")
    if not isinstance(value["toolVersion"], str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?", value["toolVersion"]
    ):
        raise OperationError("ops.materials.invalid")
    if value["id"] != fingerprint({key: item for key, item in value.items() if key != "id"}):
        raise OperationError("ops.materials.invalid")
    paths = set()
    total = 0
    for item in value["artifacts"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {"path", "kind", "name", "version", "source", "size", "sha256", "metadata"}
            or not isinstance(item["kind"], str)
            or item["kind"] not in KINDS
            or type(item["size"]) is not int
            or not 0 < item["size"] <= MAX_FILE
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"])
            or not isinstance(item["metadata"], dict)
        ):
            raise OperationError("ops.materials.invalid")
        path = relative(item["path"])
        if path in paths:
            raise OperationError("ops.materials.invalid")
        paths.add(path)
        total += item["size"]
        from ..changes.plans import safe_parameters

        safe_parameters(item["metadata"])
        for field in ("name", "version", "source"):
            if (
                not isinstance(item[field], str)
                or not 1 <= len(item[field]) <= 512
                or any(ord(c) < 32 for c in item[field])
            ):
                raise OperationError("ops.materials.invalid")
    if total > MAX_TOTAL:
        raise OperationError("ops.error.limit")
    from .closure import require_complete

    require_complete(value)
    return value


def read(directory):
    file = Path(directory) / "materials.lock.json"
    if file.is_symlink() or not file.is_file() or file.stat().st_size > 8 * 1024 * 1024:
        raise OperationError("ops.materials.invalid")
    try:
        return validate(json.loads(file.read_text(), object_pairs_hook=_unique))
    except (ValueError, TypeError, KeyError, RecursionError):
        raise OperationError("ops.materials.invalid") from None


def verify(directory, expected=None):
    base = Path(directory).expanduser().absolute()
    if any(path.is_symlink() for path in (base, *base.parents)):
        raise OperationError("ops.error.path")
    manifest = read(base)
    if expected is not None and manifest["id"] != expected:
        raise OperationError("ops.error.integrity")
    for item in manifest["artifacts"]:
        file = base / item["path"]
        if (
            any(path.is_symlink() for path in (file, *file.parents))
            or not file.is_file()
            or file.stat().st_size != item["size"]
            or digest(file) != item["sha256"]
        ):
            raise OperationError("ops.error.integrity")
    actual = {path.relative_to(base).as_posix() for path in base.rglob("*") if path.is_file()}
    if any(path.is_symlink() for path in base.rglob("*")):
        raise OperationError("ops.error.path")
    if actual != {item["path"] for item in manifest["artifacts"]} | {"materials.lock.json"}:
        raise OperationError("ops.materials.invalid")
    return manifest


def artifact(base, path, kind, name, version, source, metadata=None):
    file = Path(base) / relative(path)
    return dict(
        path=path,
        kind=kind,
        name=name,
        version=version,
        source=source,
        size=file.stat().st_size,
        sha256=digest(file),
        metadata=metadata or {},
    )
