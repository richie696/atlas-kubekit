# SPDX-License-Identifier: MIT
"""Replan failed intent from current facts; never execute an expired old plan."""
from ..models import OperationError


def parameters(previous, coordinator):
    if previous.cluster_identity != coordinator.client.cluster_identity():
        raise OperationError("ops.change.wrong_cluster")
    active = coordinator.status()
    if active and active.get("data", {}).get("owner"):
        raise OperationError("ops.change.locked")
    record = coordinator.journal(previous.id)
    if not record or record["state"] not in ("failed", "cancelled"):
        raise OperationError("ops.change.incomplete")
    if record.get("capability") != previous.capability:
        raise OperationError("ops.change.journal")
    values = previous.parameters
    if previous.capability == "cluster-upgrade":
        values["resumeOwners"] = [*values.get("resumeOwners", []), previous.id]
    if previous.capability=='component-upgrade':
        values['resumeOf']=previous.id
        values['rolloutOwner']=values.get('rolloutOwner') or previous.id
    return values
