# SPDX-License-Identifier: MIT
"""Preserve CoreDNS scheduling policy while kubeadm owns image/schema migration."""
import copy
import json
import re

from ..models import OperationError

FIELDS = frozenset(
    (
        "nodeSelector",
        "affinity",
        "tolerations",
        "topologySpreadConstraints",
        "schedulerName",
        "priorityClassName",
        "nodeName",
    )
)


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"uid", "replicas", "placement"}
        or not isinstance(value["uid"], str)
        or not re.fullmatch(r"[a-zA-Z0-9-]{8,128}", value["uid"])
        or type(value["replicas"]) is not int
        or not 1 <= value["replicas"] <= 1024
        or not isinstance(value["placement"], dict)
        or set(value["placement"]) - FIELDS
    ):
        raise OperationError("ops.dns.layout")
    if len(json.dumps(value)) > 64 * 1024:
        raise OperationError("ops.error.limit")
    return copy.deepcopy(value)


def project(deployment):
    spec = deployment["spec"]["template"]["spec"]
    return validate(
        dict(
            uid=deployment["metadata"]["uid"],
            replicas=deployment["spec"]["replicas"],
            placement={key: spec[key] for key in FIELDS if key in spec},
        )
    )


def capture(client):
    return project(client.get("deployment", "coredns", "kube-system"))


def candidate(actual, layout):
    if actual["metadata"]["uid"] != layout["uid"]:
        raise OperationError("ops.change.stale")
    desired = copy.deepcopy(actual)
    desired["spec"]["replicas"] = layout["replicas"]
    template = desired["spec"]["template"]["spec"]
    for field in FIELDS:
        if field in layout["placement"]:
            template[field] = copy.deepcopy(layout["placement"][field])
        else:
            template.pop(field, None)
    return desired


def check_server(client, value):
    layout = validate(value)
    desired = candidate(client.get("deployment", "coredns", "kube-system"), layout)
    result = client.call(
        ["replace", "--dry-run=server", "--validate=strict", "-f", "-", "-o", "json"], value=desired
    )
    if project(result) != layout:
        raise OperationError("ops.dns.layout")


def restore(client, value):
    layout = validate(value)
    for _ in range(3):
        actual = client.get("deployment", "coredns", "kube-system")
        if actual["metadata"]["uid"] != layout["uid"]:
            raise OperationError("ops.change.stale")
        if project(actual) == layout:
            return
        desired = candidate(actual, layout)
        if client.replace(desired) is not None:
            if capture(client) != layout:
                raise OperationError("ops.dns.layout")
            return
    raise OperationError("ops.change.stale")
