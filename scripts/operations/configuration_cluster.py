# SPDX-License-Identifier: MIT
"""Public desired configuration stored with CAS in its owning cluster."""
import json

from .configuration import normalize, revision
from .changes.coordinator import managed_map, require_owned
from .models import OperationError

NAME = "atlas-kubekit-configuration"
NAMESPACE = "kube-system"


def read(client):
    value = client.get("configmap", NAME, NAMESPACE, optional=True)
    if value is None:
        return None
    require_owned(value)
    try:
        data = json.loads(value["data"]["configuration"])
        config = normalize(data["config"])
        if (
            data["schemaVersion"] != 1
            or data["revision"] != revision(config)
            or data["clusterIdentity"] != client.cluster_identity()
        ):
            raise ValueError()
        return config
    except (KeyError, ValueError, TypeError):
        raise OperationError("ops.config.store_invalid") from None


def publish(client, configuration, expected=None):
    config = normalize(configuration)
    value = client.get("configmap", NAME, NAMESPACE, optional=True)
    if value is not None:
        require_owned(value)
        current = read(client)
        if current == config:
            return revision(config)
        if expected is None or revision(current) != expected:
            raise OperationError("ops.config.conflict")
    elif expected is not None:
        raise OperationError("ops.config.conflict")
    record = dict(
        schemaVersion=1,
        clusterIdentity=client.cluster_identity(),
        revision=revision(config),
        config=config,
    )
    data = dict(configuration=json.dumps(record, sort_keys=True))
    if value is None:
        result = client.create(managed_map(NAME, NAMESPACE, data))
    else:
        value["data"] = data
        result = client.replace(value)
    if result is None:
        raise OperationError("ops.config.conflict")
    return record["revision"]
