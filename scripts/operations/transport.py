# SPDX-License-Identifier: MIT
"""Bounded local/SSH fact acquisition; no policy or mutation decisions."""
import json
from pathlib import Path
import re
import shlex
import subprocess

from .collector import collect
from .models import OperationError
from .adapters.ssh import arguments


def node_facts(target=None, privileged=False, identity=None, ssh_config=None):
    if not target:
        return collect()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.-]{0,252}", target):
        raise OperationError("ops.error.target")
    source = Path(__file__).with_name("collector.py").read_text()
    # Only this read-only source is executed. Caller input cannot select a command.
    remote = ("sudo -n " if privileged else "") + "python3 -c " + shlex.quote(source)
    args = arguments(target, identity, ssh_config)
    try:
        p = subprocess.run(args + [target, remote], capture_output=True, text=True, timeout=80)
        if p.returncode != 0 or len(p.stdout) > 65536:
            raise OperationError("ops.error.collect")
        data = json.loads(p.stdout)
        if not isinstance(data, dict) or data.get("schemaVersion") != 1:
            raise OperationError("ops.error.collect")
        return data
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        if isinstance(e, OperationError):
            raise
        raise OperationError("ops.error.collect") from None
