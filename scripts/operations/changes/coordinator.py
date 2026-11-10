# SPDX-License-Identifier: MIT
"""CAS cluster ownership without unsafe timeout takeover or lock-file deletion."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import re
import uuid

from ..models import OperationError
from ..adapters.kubernetes import resource_name

OWNER_LABEL = "atlas-kubekit.io/managed-by"


def managed_map(name, namespace, data):
    return dict(
        apiVersion="v1",
        kind="ConfigMap",
        metadata=dict(
            name=resource_name(name),
            namespace=resource_name(namespace),
            labels={OWNER_LABEL: "operations"},
        ),
        data=data,
    )


def require_owned(value):
    if (
        not isinstance(value, dict)
        or value.get("metadata", {}).get("labels", {}).get(OWNER_LABEL) != "operations"
    ):
        raise OperationError("ops.change.foreign_resource")
    return value


class ClusterCoordinator:
    def __init__(self, client, namespace="kube-system"):
        self.client = client
        if namespace != "kube-system":
            raise OperationError("ops.change.lock_namespace")
        self.namespace = resource_name(namespace)
        self.name = "atlas-kubekit-operation-lock"
        self.owner = None
        self.retain = False

    @contextmanager
    def hold(self, plan_id):
        resource_name(plan_id)
        owner = str(uuid.uuid4())
        old = self.client.get("configmap", self.name, self.namespace, optional=True)
        data = dict(owner=owner, planId=plan_id, startedAt=datetime.now(timezone.utc).isoformat())
        if old is None:
            value = self.client.create(managed_map(self.name, self.namespace, data))
        else:
            require_owned(old)
            if old.get("data", {}).get("owner"):
                raise OperationError("ops.change.locked")
            old["data"] = data
            value = self.client.replace(old)
        if value is None:
            raise OperationError("ops.change.locked")
        self.owner = owner
        self.retain = False
        try:
            self.assert_owner()
            yield self
        finally:
            # Never clear a different owner's lock after a partition/manual change.
            if not self.retain:
                current = self.client.get("configmap", self.name, self.namespace, optional=True)
                if current is not None and current.get("data", {}).get("owner") == owner:
                    require_owned(current)
                    current["data"] = {}
                    if self.client.replace(current) is None:
                        raise OperationError("ops.change.lock_lost")
            self.owner = None

    def assert_owner(self):
        value = self.client.get("configmap", self.name, self.namespace)
        require_owned(value)
        if not self.owner or value.get("data", {}).get("owner") != self.owner:
            raise OperationError("ops.change.lock_lost")

    def status(self):
        value = self.client.get("configmap", self.name, self.namespace, optional=True)
        if value is not None:
            require_owned(value)
        return value

    def recover(self, owner, resource_version, confirmation):
        if confirmation != "OLD COORDINATOR AND REMOTE JOBS STOPPED":
            raise OperationError("ops.error.confirm")
        value = self.status()
        if value is None:
            raise OperationError("ops.change.lock_lost")
        require_owned(value)
        if (
            value.get("data", {}).get("owner") != owner
            or value["metadata"].get("resourceVersion") != resource_version
        ):
            raise OperationError("ops.change.lock_lost")
        # Fence the old execution before clearing its lock. Repeating recovery after
        # a crash is safe; no new coordinator can acquire the still-owned lock.
        plan_id = value.get("data", {}).get("planId")
        if plan_id:
            previous = self.journal(plan_id)
            if previous and previous["state"] == "running":
                self.owner = owner
                try:
                    self.record(plan_id, "cancelled", error="ops.change.recovered")
                finally:
                    self.owner = None
        value["data"] = {}
        if self.client.replace(value) is None:
            raise OperationError("ops.change.lock_lost")

    def journal(self, plan_id):
        value = self.client.get(
            "configmap", "atlas-op-" + plan_id[:32], self.namespace, optional=True
        )
        if value is None:
            return None
        require_owned(value)
        try:
            record = json.loads(value["data"]["record"])
        except (KeyError, ValueError):
            raise OperationError("ops.change.journal") from None
        if (
            not isinstance(record, dict)
            or set(record)
            - {"planId", "state", "step", "error", "owner", "updatedAt", "capability"}
            or not {"planId", "state", "step", "error", "owner", "updatedAt"} <= set(record)
            or record.get("planId") != plan_id
            or record.get("state") not in ("running", "succeeded", "failed", "cancelled")
        ):
            raise OperationError("ops.change.journal")
        return record

    def record(self, plan_id, state, step=None, error=None, capability=None):
        if state not in ("running", "succeeded", "failed", "cancelled") or (
            step and not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,128}", step)
        ):
            raise OperationError("ops.change.journal")
        self.assert_owner()
        name = "atlas-op-" + plan_id[:32]
        old = self.client.get("configmap", name, self.namespace, optional=True)
        previous = {}
        if old is not None:
            require_owned(old)
            previous = json.loads(old["data"]["record"])
            if previous.get("planId") != plan_id:
                raise OperationError("ops.change.journal")
        value = dict(
            planId=plan_id,
            state=state,
            step=step if step is not None else previous.get("step"),
            error=error,
            owner=self.owner,
            updatedAt=datetime.now(timezone.utc).isoformat(),
        )
        capability = capability or previous.get("capability")
        if capability is not None:
            from .plans import CAPABILITIES

            if capability not in CAPABILITIES:
                raise OperationError("ops.change.journal")
            value["capability"] = capability
        data = {"record": json.dumps(value, sort_keys=True)}
        if old is None:
            result = self.client.create(managed_map(name, self.namespace, data))
        else:
            old["data"] = data
            result = self.client.replace(old)
        if result is None:
            raise OperationError("ops.change.journal")
        return value
