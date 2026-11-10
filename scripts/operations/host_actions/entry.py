# SPDX-License-Identifier: MIT
"""Finite root action registry; all mutators share one host lock."""
import json
import os
import re

from ..collector import identity_facts
from ..locking import exclusive_lock
from scripts.lib.host_lock import host_operation, HostLockError
from ..models import OperationError
from . import (
    common,
    stats,
    security,
    upgrade,
    node,
    join_credentials,
    retirement,
    registry,
    component,
    component_images,
)


def probe(parameters, run_id):
    if parameters != {}:
        raise OperationError("ops.change.invalid_parameters")
    return dict(identity=identity_facts(), sudoUser=os.environ.get("SUDO_USER"), uid=os.geteuid())


def stats_observe(parameters, run_id):
    if parameters != {}:
        raise OperationError("ops.change.invalid_parameters")
    return stats.observe()


# Each function receives data and a run id. The boolean marks host mutation.
ACTIONS = {
    "stats-observe": (stats_observe, False),
    "security-probe": (probe, False),
    "security-observe": (lambda p, r: security.observe(p), False),
    "security-finalize": (security.finalize, True),
    "stats-apply": (stats.apply, True),
    "upgrade-observe": (lambda p, r: upgrade.observe(p), False),
    "upgrade-apply": (upgrade.apply, True),
    "node-observe": (lambda p, r: node.observe(p), False),
    "node-prepare": (node.prepare, True),
    "node-join": (node.join, True),
    "node-finalize": (node.finalize, True),
    "retirement-observe": (lambda p, r: retirement.observe(p), False),
    "retirement-apply": (retirement.apply, True),
    "registry-observe": (lambda p, r: registry.observe(p), False),
    "registry-probe": (lambda p, r: registry.probe(p), False),
    "registry-certificate": (lambda p, r: registry.certificate(p), False),
    "registry-apply": (registry.apply, True),
    "component-observe": (lambda p, r: component.observe(p), False),
    "component-apply": (component.apply, True),
    "component-complete": (component.complete, True),
    "component-images": (component_images.prepare, True),
    "join-issue": (lambda p, r: join_credentials.issue(p), True),
    "join-revoke": (lambda p, r: join_credentials.revoke(p), True),
}


def main(request):
    try:
        if os.geteuid() != 0:
            raise OperationError("ops.error.root")
        if (
            not isinstance(request, dict)
            or set(request) != {"schemaVersion", "action", "parameters", "runId"}
            or type(request["schemaVersion"]) is not int
            or request["schemaVersion"] != 1
            or not isinstance(request["action"], str)
            or request["action"] not in ACTIONS
            or not isinstance(request["runId"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", request["runId"])
        ):
            raise OperationError("ops.change.invalid_parameters")
        handler, mutates = ACTIONS[request["action"]]
        if mutates:
            with host_operation():
                result = handler(request["parameters"], request["runId"])
        else:
            result = handler(request["parameters"], request["runId"])
        print(json.dumps(dict(ok=True, result=result)), flush=True)
        return 0
    except HostLockError as error:
        print(json.dumps(dict(ok=False, error=str(error), values=[])), flush=True)
        return 1
    except OperationError as error:
        print(json.dumps(dict(ok=False, error=error.key, values=error.values)), flush=True)
        return 1
    except Exception:
        print(json.dumps(dict(ok=False, error="ops.host.failed", values=[])), flush=True)
        return 1
