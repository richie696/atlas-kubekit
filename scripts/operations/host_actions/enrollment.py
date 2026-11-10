# SPDX-License-Identifier: MIT
"""Recognize and close only the toolkit's one-time enrollment service."""
from pathlib import Path

from ..models import OperationError
from . import common

UNIT = Path("/etc/systemd/system/k8s-enroll.service")
TOKEN = Path("/var/lib/k8s-deploy/enroll-token")
DONE = Path("/var/lib/k8s-deploy/enroll-done")
EXECUTABLE = "ExecStart=/usr/bin/bash /usr/local/lib/k8s-deploy/01-prepare-node.sh --enroll-server"


def state(unit=None):
    unit = UNIT if unit is None else Path(unit)
    exists = unit.exists()
    if exists and EXECUTABLE not in common.read_owned(unit).decode():
        raise OperationError("ops.host.layout")
    active, _ = common.run(["systemctl", "is-active", "k8s-enroll.service"], check=False)
    enabled, _ = common.run(["systemctl", "is-enabled", "k8s-enroll.service"], check=False)
    return dict(unit=exists, active=exists and active == 0, enabled=exists and enabled == 0)


def close():
    before = state()
    if before["unit"]:
        common.run(["systemctl", "disable", "--now", "k8s-enroll.service"])
    if any(path.is_symlink() for path in TOKEN.parents):
        raise OperationError("ops.error.path")
    TOKEN.unlink(missing_ok=True)
    common.replace(DONE, b"")
    after = state()
    if after["active"] or after["enabled"]:
        raise OperationError("ops.host.command", "systemctl")
    return dict(enrollmentClosed=True)
