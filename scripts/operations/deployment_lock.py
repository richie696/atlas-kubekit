# SPDX-License-Identifier: MIT
"""A persistent ownership handle for the native, multi-process Shell deployment."""
import json
import os
from pathlib import Path
import re
import uuid

from .changes.coordinator import ClusterCoordinator
from .configuration import revision
from .host_actions import common
from .models import OperationError, fingerprint

HANDLE = Path("/var/lib/k8s-deploy/native-operation.json")


def begin(client, configuration):
    if os.geteuid() != 0:
        raise OperationError("ops.error.root")
    identity = client.cluster_identity()
    plan_id = fingerprint(
        dict(
            capability="cluster-deploy",
            clusterIdentity=identity,
            configurationRevision=revision(configuration),
            nonce=uuid.uuid4().hex,
        )
    )
    coordinator = ClusterCoordinator(client)
    with coordinator.hold(plan_id):
        common.replace(
            HANDLE,
            json.dumps(
                dict(
                    schemaVersion=1,
                    clusterIdentity=identity,
                    planId=plan_id,
                    owner=coordinator.owner,
                )
            ).encode(),
        )
        coordinator.record(plan_id, "running", "api-ready", capability="cluster-deploy")
        # Shell continues after this process exits. A crash must retain ownership.
        coordinator.retain = True


def owned(client):
    try:
        handle = json.loads(common.read_owned(HANDLE))
        if (
            set(handle) != {"schemaVersion", "clusterIdentity", "planId", "owner"}
            or handle["schemaVersion"] != 1
            or handle["clusterIdentity"] != client.cluster_identity()
            or not re.fullmatch(r"[a-f0-9]{64}", handle["planId"])
        ):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise OperationError("ops.change.lock_lost") from None
    coordinator = ClusterCoordinator(client)
    coordinator.owner = handle["owner"]
    coordinator.assert_owner()
    status = coordinator.status()
    if status.get("data", {}).get("planId") != handle["planId"]:
        raise OperationError("ops.change.lock_lost")
    return coordinator, handle


def fence(client):
    coordinator, handle = owned(client)
    coordinator.record(handle["planId"], "running", "deployment-stage")


def complete(client):
    coordinator, handle = owned(client)
    coordinator.record(handle["planId"], "succeeded", "complete")
    current = coordinator.status()
    current["data"] = {}
    if client.replace(current) is None:
        raise OperationError("ops.change.lock_lost")
    HANDLE.unlink()


def require_idle(client):
    current = ClusterCoordinator(client).status()
    if current and current.get("data", {}).get("owner"):
        raise OperationError("ops.change.locked")
