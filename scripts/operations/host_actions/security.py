# SPDX-License-Identifier: MIT
"""Remove only toolkit bootstrap access, with an independently verified SSH handoff."""
import json
import os
from pathlib import Path
import pwd
import re
import select
import sys

from ..collector import identity_facts
from ..models import OperationError
from . import common

BOOTSTRAP = Path("/etc/sudoers.d/90-k8s-bootstrap")
ENROLL_UNIT = Path("/etc/systemd/system/k8s-enroll.service")
ENROLL_TOKEN = Path("/var/lib/k8s-deploy/enroll-token")
PUBLIC_STATE = Path("/etc/atlas-kubekit-operations.json")


def users(parameters):
    if not isinstance(parameters, dict) or not {"deploymentUser", "retainedUser"} <= set(
        parameters
    ):
        raise OperationError("ops.change.invalid_parameters")
    for key in ("deploymentUser", "retainedUser"):
        value = parameters[key]
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value)
            or value == "root"
        ):
            raise OperationError("ops.change.invalid_parameters")
    if parameters["deploymentUser"] == parameters["retainedUser"]:
        raise OperationError("ops.security.separate_identity")
    try:
        account = pwd.getpwnam(parameters["retainedUser"])
    except KeyError:
        raise OperationError("ops.security.account_missing") from None
    if account.pw_uid == 0:
        raise OperationError("ops.security.separate_identity")
    return parameters["deploymentUser"], parameters["retainedUser"]


def observe(parameters):
    if set(parameters) != {"deploymentUser", "retainedUser"}:
        raise OperationError("ops.change.invalid_parameters")
    deployed, retained = users(parameters)
    original = common.read_owned(BOOTSTRAP) if BOOTSTRAP.exists() else None
    if original is not None:
        pattern = re.escape(deployed) + r"\s+ALL=\(root\)\s+NOPASSWD:\s*ALL\s*"
        if not re.fullmatch(pattern, original.decode().strip()):
            raise OperationError("ops.security.foreign_grant")
    unit = ENROLL_UNIT.exists()
    if unit:
        text = common.read_owned(ENROLL_UNIT).decode()
        if (
            "ExecStart=/usr/bin/bash /usr/local/lib/k8s-deploy/01-prepare-node.sh --enroll-server"
            not in text
        ):
            raise OperationError("ops.host.layout")
    active, _ = common.run(["systemctl", "is-active", "k8s-enroll.service"], check=False)
    enabled, _ = common.run(["systemctl", "is-enabled", "k8s-enroll.service"], check=False)
    if PUBLIC_STATE.exists():
        value = json.loads(common.read_owned(PUBLIC_STATE))
        if (
            not isinstance(value, dict)
            or value.get("schemaVersion") != 1
            or set(value) - {"schemaVersion", "operationsUser", "planId"}
        ):
            raise OperationError("ops.host.layout")
    return dict(
        identity=identity_facts(),
        bootstrapSha=common.digest(original) if original is not None else None,
        enrollmentUnit=unit,
        enrollmentActive=unit and active == 0,
        enrollmentEnabled=unit and enabled == 0,
        retainedUser=retained,
    )


def decision():
    readable, _, _ = select.select([sys.stdin.buffer], [], [], 60)
    if not readable:
        return False
    try:
        value = json.loads(sys.stdin.buffer.readline(4097))
        return isinstance(value, dict) and value == {"decision": "commit"}
    except ValueError:
        return False


def finalize(parameters, run_id, decide=decision):
    if not isinstance(parameters, dict) or set(parameters) != {
        "deploymentUser",
        "retainedUser",
        "expectedSha",
    }:
        raise OperationError("ops.change.invalid_parameters")
    values = {k: parameters[k] for k in ("deploymentUser", "retainedUser")}
    before = observe(values)
    if parameters["expectedSha"] != before["bootstrapSha"]:
        raise OperationError("ops.change.stale")
    directory = common.journal(run_id, "security-finalize")
    original = common.read_owned(BOOTSTRAP) if BOOTSTRAP.exists() else None
    token = common.read_owned(ENROLL_TOKEN) if ENROLL_TOKEN.exists() else None
    public = common.read_owned(PUBLIC_STATE) if PUBLIC_STATE.exists() else None
    if original is not None:
        common.replace(directory / "bootstrap.before", original)
    if token is not None:
        common.replace(directory / "enrollment.before", token)
    try:
        if before["enrollmentUnit"]:
            common.run(["systemctl", "disable", "--now", "k8s-enroll.service"])
        if original is not None:
            if common.digest(common.read_owned(BOOTSTRAP)) != before["bootstrapSha"]:
                raise OperationError("ops.change.stale")
            BOOTSTRAP.unlink()
        common.run(["visudo", "-cf", "/etc/sudoers"])
        print(json.dumps(dict(ok=True, phase="prepared")), flush=True)
        if not decide():
            raise OperationError("ops.security.probe_failed")
        PUBLIC_STATE.parent.mkdir(mode=0o755, exist_ok=True)
        common.replace(
            PUBLIC_STATE,
            json.dumps(
                dict(schemaVersion=1, operationsUser=values["retainedUser"], planId=run_id)
            ).encode(),
            0o644,
        )
        ENROLL_TOKEN.unlink(missing_ok=True)
        common.record(
            directory, "security-finalize", "succeeded", retainedUser=values["retainedUser"]
        )
        return dict(
            committed=True,
            retainedUser=values["retainedUser"],
            bootstrapRemoved=original is not None,
        )
    except BaseException:
        try:
            if original is not None:
                common.replace(BOOTSTRAP, original, 0o440)
            if token is not None:
                common.replace(ENROLL_TOKEN, token)
            if public is not None:
                common.replace(PUBLIC_STATE, public, 0o644)
            elif PUBLIC_STATE.exists():
                PUBLIC_STATE.unlink()
            common.run(["visudo", "-cf", "/etc/sudoers"])
            if before["enrollmentEnabled"]:
                common.run(["systemctl", "enable", "k8s-enroll.service"])
            if before["enrollmentActive"]:
                common.run(["systemctl", "start", "k8s-enroll.service"])
            common.record(directory, "security-finalize", "rolled-back")
        except BaseException:
            common.record(directory, "security-finalize", "rollback-failed")
            raise OperationError("ops.host.rollback") from None
        raise
