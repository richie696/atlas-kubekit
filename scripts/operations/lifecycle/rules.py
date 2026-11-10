# SPDX-License-Identifier: MIT
"""Pure node, workload and quorum facts; no shell or Kubernetes effects."""
from ..models import OperationError


def ready(node):
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in node.get("status", {}).get("conditions", [])
    )


def control_plane(node):
    return "node-role.kubernetes.io/control-plane" in node.get("metadata", {}).get("labels", {})


def node_fact(node):
    return dict(
        name=node["metadata"]["name"],
        uid=node["metadata"]["uid"],
        ready=ready(node),
        controlPlane=control_plane(node),
        unschedulable=bool(node.get("spec", {}).get("unschedulable")),
        maintenanceOwner=node.get("metadata", {})
        .get("annotations", {})
        .get("atlas-kubekit.io/maintenance-plan"),
        version=node.get("status", {}).get("nodeInfo", {}).get("kubeletVersion"),
    )


def require_quorum(nodes, target):
    planes = [n for n in nodes.values() if control_plane(n)]
    if not planes or target not in nodes:
        raise OperationError("ops.lifecycle.inventory")
    if control_plane(nodes[target]):
        others = sum(ready(n) for n in planes if n["metadata"]["name"] != target)
        if len(planes) < 3 or others < len(planes) // 2 + 1:
            raise OperationError("ops.lifecycle.quorum")


def movable_pods(pods, delete_empty_dir):
    result = []
    for pod in pods:
        meta = pod["metadata"]
        owners = meta.get("ownerReferences", [])
        if meta.get("annotations", {}).get("kubernetes.io/config.mirror") or any(
            o.get("kind") == "DaemonSet" for o in owners
        ):
            continue
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        for volume in pod.get("spec", {}).get("volumes", []):
            if "hostPath" in volume or ("emptyDir" in volume and not delete_empty_dir):
                raise OperationError("ops.lifecycle.local_data")
        result.append(
            dict(
                name=meta["name"],
                namespace=meta["namespace"],
                uid=meta["uid"],
                owners=sorted(o["uid"] for o in owners),
                volumes=pod.get("spec", {}).get("volumes", []),
                phase=pod.get("status", {}).get("phase"),
                ready=ready(pod),
            )
        )
    return sorted(result, key=lambda p: (p["namespace"], p["name"]))


def selector_matches(selector, labels):
    if selector is None:
        return False
    if not isinstance(selector, dict) or set(selector) - {"matchLabels", "matchExpressions"}:
        raise OperationError("ops.lifecycle.pdb")
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for item in selector.get("matchExpressions", []):
        key, operator, values = item["key"], item["operator"], item.get("values", [])
        if operator == "In":
            match = key in labels and labels[key] in values
        elif operator == "NotIn":
            match = key not in labels or labels[key] not in values
        elif operator == "Exists":
            match = key in labels
        elif operator == "DoesNotExist":
            match = key not in labels
        else:
            raise OperationError("ops.lifecycle.pdb")
        if not match:
            return False
    return True


def require_pdbs(pods, budgets):
    for pod in pods:
        if pod.get("status", {}).get("phase") in ("Pending", "Succeeded", "Failed") or pod[
            "metadata"
        ].get("deletionTimestamp"):
            continue
        matches = [
            budget
            for budget in budgets
            if budget["metadata"]["namespace"] == pod["metadata"]["namespace"]
            and selector_matches(
                budget.get("spec", {}).get("selector"), pod["metadata"].get("labels", {})
            )
        ]
        if len(matches) > 1:
            raise OperationError("ops.lifecycle.pdb")
        if not matches:
            continue
        budget = matches[0]
        meta, spec, status = budget["metadata"], budget.get("spec", {}), budget.get("status", {})
        if not ready(pod):
            if spec.get("unhealthyPodEvictionPolicy") == "AlwaysAllow":
                continue
            current, desired = status.get("currentHealthy"), status.get("desiredHealthy")
            if type(current) is int and type(desired) is int and desired > 0 and current >= desired:
                continue
        allowed = status.get("disruptionsAllowed")
        if (
            type(meta.get("generation")) is not int
            or meta["generation"] < 1
            or type(status.get("observedGeneration")) is not int
            or status.get("observedGeneration") != meta["generation"]
            or type(allowed) is not int
            or allowed < 1
        ):
            raise OperationError("ops.lifecycle.pdb")
