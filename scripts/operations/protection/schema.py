# SPDX-License-Identifier: MIT
"""Public, bounded protection declarations; no credentials or executable commands."""
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re

from ..models import OperationError, fingerprint

MAX_METADATA = 128 * 1024


def invalid():
    raise OperationError("ops.protection.invalid")


def identifier(value, uid=False):
    pattern = r"[a-zA-Z0-9-]{8,128}" if uid else r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        invalid()
    return value


def normalize(value):
    if not isinstance(value, dict) or set(value) != {
        "schemaVersion",
        "clusterIdentity",
        "provider",
        "createdAt",
        "objects",
        "artifact",
    }:
        invalid()
    if (
        type(value["schemaVersion"]) is not int
        or value["schemaVersion"] != 1
        or value["provider"] != "age-archive"
    ):
        invalid()
    identifier(value["clusterIdentity"], True)
    try:
        created = datetime.fromisoformat(value["createdAt"])
        if created.tzinfo is None:
            invalid()
    except (TypeError, ValueError):
        invalid()
    rows = value["objects"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 256:
        invalid()
    keys = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"kind", "namespace", "name", "uid"}
            or row["kind"] not in ("persistentvolumeclaim", "deployment", "statefulset")
        ):
            invalid()
        for field in ("namespace", "name", "uid"):
            identifier(row[field], field == "uid")
        key = (row["kind"], row["namespace"], row["name"])
        if key in keys:
            invalid()
        keys.add(key)
    artifact = value["artifact"]
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"path", "size", "sha256"}
        or not isinstance(artifact["path"], str)
        or not artifact["path"].startswith("/")
        or len(artifact["path"]) > 2048
        or any(ord(c) < 32 for c in artifact["path"])
        or type(artifact["size"]) is not int
        or not 32 <= artifact["size"] <= 8 * 1024**3
        or not isinstance(artifact["sha256"], str)
        or not re.fullmatch("[a-f0-9]{64}", artifact["sha256"])
    ):
        invalid()
    return copy.deepcopy(value)


def load(path):
    file = Path(path).expanduser().absolute()
    if (
        any(p.is_symlink() for p in (file, *file.parents))
        or not file.is_file()
        or file.stat().st_uid != os.geteuid()
        or file.stat().st_mode & 0o077
        or file.stat().st_size > MAX_METADATA
    ):
        raise OperationError("ops.protection.private_file")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                invalid()
            result[key] = value
        return result

    try:
        return normalize(json.loads(file.read_text(), object_pairs_hook=unique))
    except (UnicodeError, ValueError):
        invalid()


def public(value):
    result = normalize(value)
    result["artifact"].pop("path")
    return result


def record_id(value):
    return fingerprint(public(value))


def validate_public(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("artifact"), dict)
        or "path" in value["artifact"]
    ):
        invalid()
    candidate = copy.deepcopy(value)
    candidate["artifact"]["path"] = "/external-reference"
    return public(candidate)
