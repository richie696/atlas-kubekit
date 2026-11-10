# SPDX-License-Identifier: MIT
"""Immutable public evidence records owned by the source Kubernetes cluster."""
import json
import re

from ..changes.coordinator import managed_map, require_owned
from ..models import OperationError, fingerprint
from . import schema

PREFIX = "atlas-kubekit-protection-"
NAMESPACE = "kube-system"


def read(value, cluster):
    require_owned(value)
    try:
        record = json.loads(value["data"]["record"])
        if (
            set(record) != {"schemaVersion", "id", "declaration", "evidence"}
            or record["schemaVersion"] != 1
            or not re.fullmatch("[a-f0-9]{64}", record["id"])
            or record["id"] != fingerprint(record["declaration"])
            or record["declaration"]["clusterIdentity"] != cluster
            or value["metadata"]["name"] != PREFIX + record["id"][:24]
            or record["evidence"]
            != dict(
                integrity="ciphertext-verified", decryption="unverified", restoration="unverified"
            )
        ):
            raise ValueError()
        schema.validate_public(record["declaration"])
        return record
    except (KeyError, TypeError, ValueError):
        raise OperationError("ops.protection.record") from None


def list_records(client):
    cluster = client.cluster_identity()
    return sorted(
        (
            read(row, cluster)
            for row in client.get("configmap", namespace=NAMESPACE)["items"]
            if row["metadata"]["name"].startswith(PREFIX)
        ),
        key=lambda record: record["id"],
    )


def publish(client, record):
    name = PREFIX + record["id"][:24]
    old = client.get("configmap", name, NAMESPACE, optional=True)
    if old is not None:
        if read(old, client.cluster_identity()) != record:
            raise OperationError("ops.protection.record")
        return False
    if (
        client.create(managed_map(name, NAMESPACE, dict(record=json.dumps(record, sort_keys=True))))
        is None
    ):
        raise OperationError("ops.config.conflict")
    return True
