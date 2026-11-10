# SPDX-License-Identifier: MIT
"""Shared SSH trust/identity arguments, including explicit bastion configuration."""
import os
from pathlib import Path
import re

from ..models import OperationError


def arguments(target, identity=None, configuration=None):
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.-]{0,252}", target):
        raise OperationError("ops.error.target")
    args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
    ]
    if configuration:
        path = Path(configuration).expanduser()
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_uid != os.geteuid()
            or path.stat().st_mode & 0o022
        ):
            raise OperationError("ops.error.identity")
        args += ["-F", str(path)]
    if identity:
        key = Path(identity).expanduser()
        if not key.is_file():
            raise OperationError("ops.error.identity")
        args += ["-o", "IdentitiesOnly=yes", "-i", str(key)]
    return args
