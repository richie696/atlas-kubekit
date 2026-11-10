# SPDX-License-Identifier: MIT
"""Private profile persistence with explicit revision checks and atomic writes."""
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .configuration import normalize, revision, _unique
from .locking import exclusive_lock
from .models import OperationError
from .reporting import private_directory, atomic_json


def profile_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value):
        raise OperationError("ops.config.invalid")
    return value


def read_profile(state, name):
    name = profile_name(name)
    base = private_directory(state)
    path = base / (name + ".json")
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > 1024 * 1024
        ):
            raise OperationError("ops.config.store_invalid")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
        if (
            not isinstance(value, dict)
            or set(value) != {"schemaVersion", "revision", "config"}
            or type(value["schemaVersion"]) is not int
            or value["schemaVersion"] != 1
        ):
            raise OperationError("ops.config.store_invalid")
        config = normalize(value["config"])
        if value["revision"] != revision(config):
            raise OperationError("ops.config.store_invalid")
        return config, value["revision"]
    except FileNotFoundError:
        raise OperationError("ops.config.not_found") from None
    except (OSError, ValueError, RecursionError):
        raise OperationError("ops.config.store_invalid") from None


def import_profile(state, name, config, expected_revision=None):
    name = profile_name(name)
    data = normalize(config)
    wanted = revision(data)
    base = private_directory(state)
    with exclusive_lock(base / "locks", name):
        path = base / (name + ".json")
        try:
            old, current = read_profile(base, name)
        except OperationError as error:
            if error.key != "ops.config.not_found":
                raise
            current = None
        if current == wanted:
            return wanted, False
        if current != expected_revision:
            raise OperationError("ops.config.conflict")
        atomic_json(path, dict(schemaVersion=1, revision=wanted, config=data))
        return wanted, True


def export_config(path, config):
    data = normalize(config)
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise OperationError("ops.error.path")
    base = private_directory(path.parent)
    # link publishes a completed inode without replacing any existing destination.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=base, delete=False) as stream:
        temp = Path(stream.name)
        try:
            os.chmod(temp, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        try:
            os.link(temp, path)
        except FileExistsError:
            raise OperationError("ops.config.export_exists") from None
    finally:
        temp.unlink(missing_ok=True)
    return path
