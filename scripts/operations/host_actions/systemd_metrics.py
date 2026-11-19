# SPDX-License-Identifier: MIT
"""Observe and install the fixed Atlas node systemd metrics collector."""
import json
import re
import subprocess
import sys

from ..collector import identity_facts
from ..models import OperationError
from . import stage_assets


def _run_helper(arguments):
    with stage_assets.installed() as assets:
        helper = assets / "stages/node-systemd-metrics.py"
        try:
            result = subprocess.run([sys.executable, str(helper), *arguments],
                                    check=False, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            raise OperationError("ops.host.failed") from None
        if result.returncode != 0:
            raise OperationError("ops.host.failed")
        return result.stdout


def observe(parameters, run_id):
    if parameters != {}:
        raise OperationError("ops.change.invalid_parameters")
    output = _run_helper(["--observe"])
    try:
        facts = json.loads(output)
    except (ValueError, TypeError):
        raise OperationError("ops.host.failed") from None
    if (not isinstance(facts, dict) or set(facts) != {
            "collectorInstalled", "timerActive", "timerEnabled", "metricsAgeSeconds", "units"}):
        raise OperationError("ops.host.failed")
    return {"identity": identity_facts(), "metrics": facts}


def apply(parameters, run_id):
    if parameters != {}:
        raise OperationError("ops.change.invalid_parameters")
    _run_helper(["--install"])
    result = observe({}, run_id)
    if not all(result["metrics"][key] for key in ("collectorInstalled", "timerActive", "timerEnabled")):
        raise OperationError("ops.host.failed")
    return result


def _removal_files(arguments):
    try:
        value = json.loads(_run_helper(arguments))
    except (TypeError, ValueError):
        raise OperationError("ops.host.failed") from None
    if not valid_removal_files(value):
        raise OperationError("ops.host.failed")
    return value


def valid_removal_files(value):
    return (isinstance(value, dict) and set(value) == {"collector", "service", "timer"} and
            all(item is None or isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
                for item in value.values()))


def observe_removal(parameters, run_id):
    if parameters != {}:
        raise OperationError("ops.change.invalid_parameters")
    return {"identity": identity_facts(), "files": _removal_files(["--removal-snapshot"])}


def remove(parameters, run_id):
    if not isinstance(parameters, dict) or set(parameters) != {"expected", "identity"} or not valid_removal_files(
            parameters["expected"]):
        raise OperationError("ops.change.invalid_parameters")
    identity = identity_facts()
    if (identity.get("identityState") != "ok" or
            parameters["identity"] != {key: identity.get(key) for key in ("hostname", "identityFingerprint")}):
        raise OperationError("ops.change.wrong_identity")
    files = _removal_files(["--uninstall", json.dumps(parameters["expected"], sort_keys=True)])
    if any(files.values()):
        raise OperationError("ops.host.failed")
    return {"identity": identity, "files": files}
