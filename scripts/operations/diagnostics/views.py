# SPDX-License-Identifier: MIT
"""Pure projections: never include credentials, annotations, logs or event messages."""
import re

from ..models import OperationError

MAX_ITEMS = 2000


def text(value, limit=253):
    if not isinstance(value, str):
        return None
    return "".join(c for c in value[:limit] if c.isprintable())


def identity(resource):
    metadata = resource.get("metadata", {})
    return dict(name=text(metadata.get("name")), namespace=text(metadata.get("namespace")))


def conditions(resource):
    return [
        dict(
            type=text(item.get("type")),
            status=text(item.get("status")),
            reason=text(item.get("reason")),
        )
        for item in resource.get("status", {}).get("conditions", [])[:32]
    ]


def project(kind, resource):
    result = identity(resource)
    status = resource.get("status", {})
    spec = resource.get("spec", {})
    if kind == "node":
        result.update(
            conditions=conditions(resource),
            unschedulable=bool(spec.get("unschedulable")),
            kubeletVersion=text(status.get("nodeInfo", {}).get("kubeletVersion")),
        )
    elif kind == "pod":
        result.update(
            node=text(spec.get("nodeName")),
            phase=text(status.get("phase")),
            conditions=conditions(resource),
        )
        result["containers"] = [
            dict(
                name=text(row.get("name")),
                ready=row.get("ready") is True,
                restarts=row.get("restartCount"),
                state=next(
                    (
                        name
                        for name in ("running", "waiting", "terminated")
                        if name in row.get("state", {})
                    ),
                    None,
                ),
            )
            for row in status.get("containerStatuses", [])[:64]
        ]
    elif kind in ("deployment", "statefulset", "daemonset"):
        result.update(conditions=conditions(resource))
        result["replicas"] = {
            key: status.get(key)
            for key in (
                "replicas",
                "readyReplicas",
                "availableReplicas",
                "desiredNumberScheduled",
                "numberReady",
            )
            if type(status.get(key)) is int
        }
    elif kind == "pdb":
        result.update(
            disruptionsAllowed=status.get("disruptionsAllowed"),
            currentHealthy=status.get("currentHealthy"),
            desiredHealthy=status.get("desiredHealthy"),
        )
    elif kind in ("persistentvolume", "persistentvolumeclaim"):
        result.update(
            phase=text(status.get("phase")),
            storageClass=text(spec.get("storageClassName")),
            driver=text(spec.get("csi", {}).get("driver")),
        )
    elif kind == "event":
        result.update(
            type=text(resource.get("type")),
            reason=text(resource.get("reason")),
            count=resource.get("count"),
            involved=identity({"metadata": resource.get("involvedObject", {})}),
        )
    else:
        raise OperationError("ops.change.invalid_parameters")
    return result


def collection(kind, value):
    items = value.get("items")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise OperationError("ops.error.limit")
    return sorted(
        (project(kind, row) for row in items),
        key=lambda row: (row.get("namespace") or "", row.get("name") or ""),
    )
