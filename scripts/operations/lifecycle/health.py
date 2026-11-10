# SPDX-License-Identifier: MIT
"""Control-plane health shared by maintenance and upgrade workflows."""
from ..models import OperationError
from .rules import control_plane, ready
import time


def healthy_etcd(client, nodes, *, require_ready=True):
    planes = sorted(name for name, node in nodes.items() if control_plane(node))
    if len(planes) < 3 or require_ready and not all(ready(nodes[name]) for name in planes):
        raise OperationError("ops.lifecycle.quorum")
    rows = client.call(
        [
            "-n",
            "kube-system",
            "exec",
            "etcd-" + planes[0],
            "--",
            "etcdctl",
            "--endpoints=https://127.0.0.1:2379",
            "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
            "--cert=/etc/kubernetes/pki/etcd/healthcheck-client.crt",
            "--key=/etc/kubernetes/pki/etcd/healthcheck-client.key",
            "endpoint",
            "health",
            "--cluster",
            "--write-out=json",
        ]
    )
    if (
        not isinstance(rows, list)
        or len(rows) != len(planes)
        or not all(row.get("health") is True for row in rows)
    ):
        raise OperationError("ops.lifecycle.quorum")
    return planes


def workload_facts(client):
    rows = []
    for kind in ("deployment", "daemonset", "statefulset"):
        for item in client.get(kind)["items"]:
            meta, spec, status = item["metadata"], item.get("spec", {}), item.get("status", {})
            desired = (
                status.get("desiredNumberScheduled", 0)
                if kind == "daemonset"
                else spec.get("replicas", 1)
            )
            count = (
                status.get("numberReady", 0)
                if kind == "daemonset"
                else status.get("readyReplicas", 0)
            )
            healthy = (
                type(desired) is int
                and type(count) is int
                and count == desired
                and status.get("observedGeneration", 0) >= meta.get("generation", 1)
            )
            if kind == "deployment":
                healthy = (
                    healthy
                    and status.get("updatedReplicas", 0) >= desired
                    and status.get("availableReplicas", 0) >= desired
                )
            elif (
                kind == "daemonset"
                and spec.get("updateStrategy", {}).get("type", "RollingUpdate") != "OnDelete"
            ):
                healthy = healthy and status.get("updatedNumberScheduled", 0) >= desired
            elif (
                kind == "statefulset"
                and spec.get("updateStrategy", {}).get("type", "RollingUpdate") != "OnDelete"
            ):
                partition = (
                    spec.get("updateStrategy", {}).get("rollingUpdate", {}).get("partition", 0)
                )
                healthy = healthy and status.get("updatedReplicas", 0) >= max(
                    0, desired - partition
                )
            rows.append(
                dict(
                    kind=kind,
                    namespace=meta["namespace"],
                    name=meta["name"],
                    uid=meta["uid"],
                    desired=desired,
                    ready=count,
                    healthy=healthy,
                )
            )
    return sorted(rows, key=lambda row: (row["kind"], row["namespace"], row["name"]))


def require_workloads(client):
    rows = workload_facts(client)
    if not all(row["healthy"] for row in rows):
        raise OperationError("ops.lifecycle.workloads")
    return rows


def wait_workloads(client, timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        rows = workload_facts(client)
        if all(row["healthy"] for row in rows):
            return
        if time.monotonic() >= deadline:
            raise OperationError("ops.lifecycle.workloads")
        time.sleep(1)
