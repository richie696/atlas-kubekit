# SPDX-License-Identifier: MIT
"""Private immutable plan files; no arbitrary effect is loaded from a plan."""
import json
import os
from pathlib import Path
import tempfile

from ..configuration import _unique
from ..models import OperationError
from ..reporting import private_directory
from .plans import load


def save(path, plan):
    path = Path(path).expanduser().absolute()
    base = private_directory(path.parent)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=base, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(plan.record(), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise OperationError("ops.config.export_exists") from None
    finally:
        temporary.unlink(missing_ok=True)
    return path


def read(path, *, history=False):
    path = Path(path).expanduser().absolute()
    if (
        any(p.is_symlink() for p in (path, *path.parents))
        or not path.is_file()
        or path.stat().st_size > 1024 * 1024
        or path.stat().st_uid != os.geteuid()
        or path.stat().st_mode & 0o077
    ):
        raise OperationError("ops.change.invalid_plan")
    try:
        return load(
            json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique), history=history
        )
    except (ValueError, OSError, RecursionError) as error:
        if isinstance(error, OperationError):
            raise
        raise OperationError("ops.change.invalid_plan") from None
