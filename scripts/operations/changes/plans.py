# SPDX-License-Identifier: MIT
"""Immutable execution intent. No commands, filesystem or cluster access."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re

from ..models import OperationError, fingerprint

CAPABILITIES = frozenset(
    (
        "lb-stats",
        "registry-configure",
        "security-finalize",
        "node-maintenance",
        "node-join",
        "node-retire",
        "node-replace",
        "cluster-upgrade",
        "component-upgrade",
        "protection-register",
        "cluster-deploy",
    )
)
FIELDS = {
    "schemaVersion",
    "id",
    "capability",
    "clusterIdentity",
    "createdAt",
    "expiresAt",
    "observationFingerprint",
    "parameters",
}


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise OperationError("ops.change.invalid_plan") from None


def safe_parameters(value):
    """Parameters are data only; capability-specific validation follows loading."""
    if not isinstance(value, dict):
        raise OperationError("ops.change.invalid_plan")

    def visit(part, depth=0):
        if depth > 12:
            raise OperationError("ops.change.invalid_plan")
        if isinstance(part, dict):
            for key, item in part.items():
                if (
                    not isinstance(key, str)
                    or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,252}", key)
                    or key.casefold()
                    in {
                        "password",
                        "token",
                        "privatekey",
                        "certificatekey",
                        "kubeconfig",
                        "shell",
                        "command",
                        "script",
                    }
                ):
                    raise OperationError("ops.change.invalid_plan")
                visit(item, depth + 1)
        elif isinstance(part, list):
            if len(part) > 1024:
                raise OperationError("ops.change.invalid_plan")
            for item in part:
                visit(item, depth + 1)
        elif isinstance(part, str):
            if len(part) > 2048 or any(ord(c) < 32 for c in part):
                raise OperationError("ops.change.invalid_plan")
        elif part is not None and type(part) not in (int, bool):
            raise OperationError("ops.change.invalid_plan")

    visit(value)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 512 * 1024:
        raise OperationError("ops.change.invalid_plan")
    return encoded


@dataclass(frozen=True)
class ChangePlan:
    id: str
    capability: str
    cluster_identity: str
    created_at: str
    expires_at: str
    observation_fingerprint: str
    parameters_json: str

    @property
    def parameters(self):
        return json.loads(self.parameters_json)

    def record(self):
        return dict(
            schemaVersion=1,
            id=self.id,
            capability=self.capability,
            clusterIdentity=self.cluster_identity,
            createdAt=self.created_at,
            expiresAt=self.expires_at,
            observationFingerprint=self.observation_fingerprint,
            parameters=self.parameters,
        )


def make(capability, cluster_identity, observation, parameters, now=None, lifetime=900):
    now = now or datetime.now(timezone.utc)
    if (
        capability not in CAPABILITIES
        or not isinstance(cluster_identity, str)
        or not re.fullmatch(r"[a-zA-Z0-9-]{8,128}", cluster_identity)
        or type(lifetime) is not int
        or not 60 <= lifetime <= 3600
        or now.tzinfo is None
    ):
        raise OperationError("ops.change.invalid_plan")
    parameters_json = safe_parameters(parameters)
    created = now.astimezone(timezone.utc).isoformat()
    expires = (now + timedelta(seconds=lifetime)).astimezone(timezone.utc).isoformat()
    body = dict(
        schemaVersion=1,
        capability=capability,
        clusterIdentity=cluster_identity,
        createdAt=created,
        expiresAt=expires,
        observationFingerprint=fingerprint(observation),
        parameters=json.loads(parameters_json),
    )
    return ChangePlan(
        fingerprint(body),
        capability,
        cluster_identity,
        created,
        expires,
        body["observationFingerprint"],
        parameters_json,
    )


def load(value, now=None, *, history=False):
    if (
        not isinstance(value, dict)
        or set(value) != FIELDS
        or type(value.get("schemaVersion")) is not int
        or value["schemaVersion"] != 1
    ):
        raise OperationError("ops.change.invalid_plan")
    body = {k: v for k, v in value.items() if k != "id"}
    if (
        not isinstance(value["capability"], str)
        or value["capability"] not in CAPABILITIES
        or value["id"] != fingerprint(body)
    ):
        raise OperationError("ops.change.invalid_plan")
    if not isinstance(value["clusterIdentity"], str) or not re.fullmatch(
        r"[a-zA-Z0-9-]{8,128}", value["clusterIdentity"]
    ):
        raise OperationError("ops.change.invalid_plan")
    if not isinstance(value["observationFingerprint"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", value["observationFingerprint"]
    ):
        raise OperationError("ops.change.invalid_plan")
    created = timestamp(value["createdAt"])
    expires = timestamp(value["expiresAt"])
    now = now or datetime.now(timezone.utc)
    if not 60 <= (expires - created).total_seconds() <= 3600 or created > now + timedelta(
        seconds=30
    ):
        raise OperationError("ops.change.invalid_plan")
    if expires <= now and not history:
        raise OperationError("ops.change.expired")
    return ChangePlan(
        value["id"],
        value["capability"],
        value["clusterIdentity"],
        value["createdAt"],
        value["expiresAt"],
        value["observationFingerprint"],
        safe_parameters(value["parameters"]),
    )
