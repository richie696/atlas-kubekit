# SPDX-License-Identifier: MIT
"""CAS scheduling transitions and explicit ownership of toolkit maintenance."""
import re

from ..models import OperationError

OWNER = "atlas-kubekit.io/maintenance-plan"


def owner(node):
    return node.get("metadata", {}).get("annotations", {}).get(OWNER)


def change(client, name, uid, unschedulable, run_id=None):
    if run_id is not None and not re.fullmatch(r"[a-f0-9]{64}", run_id):
        raise OperationError("ops.change.invalid_parameters")
    for _ in range(3):
        node = client.get("node", name)
        if node["metadata"]["uid"] != uid:
            raise OperationError("ops.change.stale")
        was_cordoned = bool(node.get("spec", {}).get("unschedulable"))
        node.setdefault("spec", {})["unschedulable"] = unschedulable
        annotations = node["metadata"].setdefault("annotations", {})
        if unschedulable and not was_cordoned and run_id:
            annotations[OWNER] = run_id
        elif not unschedulable:
            annotations.pop(OWNER, None)
        if client.replace(node) is not None:
            return
    raise OperationError("ops.change.stale")
