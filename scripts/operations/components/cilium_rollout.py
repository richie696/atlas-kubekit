# SPDX-License-Identifier: MIT
"""Cilium Pod identity, per-node readiness and runtime rollout verification."""
import time

from ..lifecycle.rules import ready
from ..models import OperationError
from . import cilium_policy


def daemon_facts(client, values, nodes):
    result = {}
    pods = client.get("pod", namespace="kube-system")["items"]
    for name, pinned in values["policies"].items():
        daemon = client.get("daemonset", name, "kube-system")
        if daemon["metadata"]["uid"] != pinned["uid"]:
            raise OperationError("ops.change.stale")
        selected = [
            pod
            for pod in pods
            if not pod["metadata"].get("deletionTimestamp")
            and any(
                owner.get("uid") == pinned["uid"]
                for owner in pod["metadata"].get("ownerReferences", [])
            )
        ]
        present = [pod["spec"].get("nodeName") for pod in selected]
        if (
            len(present) != len(set(present))
            or set(present) - set(nodes)
            or not values["resumeOf"]
            and set(present) != set(nodes)
        ):
            raise OperationError("ops.component.policy")
        result[name] = dict(
            uid=pinned["uid"],
            strategy=cilium_policy.strategy(daemon["spec"]["updateStrategy"]),
            images=cilium_policy.images(daemon["spec"]["template"]["spec"]),
            owner=daemon["spec"]["template"]
            .get("metadata", {})
            .get("annotations", {})
            .get(cilium_policy.OWNER),
            pods=sorted(
                (
                    dict(
                        name=pod["metadata"]["name"],
                        uid=pod["metadata"]["uid"],
                        node=pod["spec"]["nodeName"],
                        ready=ready(pod),
                        images=cilium_policy.images(pod["spec"]),
                        owner=pod["metadata"].get("annotations", {}).get(cilium_policy.OWNER),
                    )
                    for pod in selected
                ),
                key=lambda row: row["node"],
            ),
        )
    return result, pods


def target_pod(client, values, daemon, node):
    uid = values["policies"][daemon]["uid"]
    pods = [
        pod
        for pod in client.get("pod", namespace="kube-system")["items"]
        if pod["spec"].get("nodeName") == node
        and not pod["metadata"].get("deletionTimestamp")
        and any(owner.get("uid") == uid for owner in pod["metadata"].get("ownerReferences", []))
    ]
    return pods[0] if len(pods) == 1 else None


def wait_pod(client, values, daemon, node, images, owner, timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        pod = target_pod(client, values, daemon, node)
        if (
            pod
            and ready(pod)
            and cilium_policy.images(pod["spec"]) == images
            and pod["metadata"].get("annotations", {}).get(cilium_policy.OWNER) == owner
        ):
            return pod
        if time.monotonic() >= deadline:
            raise OperationError("ops.component.not_ready")
        time.sleep(1)


def wait_agent(client, pod, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        result = client.call(
            [
                "-n",
                "kube-system",
                "exec",
                pod["metadata"]["name"],
                "-c",
                "cilium-agent",
                "--",
                "cilium-dbg",
                "status",
                "--brief",
            ],
            json_output=False,
            timeout=30,
            allow_failure=True,
        )
        if result is not None:
            return
        if time.monotonic() >= deadline:
            raise OperationError("ops.component.not_ready")
        time.sleep(1)


def verify_operator(client, values, images):
    deployment = client.get("deployment", "cilium-operator", "kube-system")
    uid = values["operatorPolicy"]["uid"]
    if (
        deployment["metadata"]["uid"] != uid
        or cilium_policy.images(deployment["spec"]["template"]["spec"]) != images
    ):
        raise OperationError("ops.component.not_ready")
    replicas = {
        row["metadata"]["uid"]
        for row in client.get("replicaset", namespace="kube-system")["items"]
        if any(owner.get("uid") == uid for owner in row["metadata"].get("ownerReferences", []))
    }
    pods = [
        pod
        for pod in client.get("pod", namespace="kube-system")["items"]
        if not pod["metadata"].get("deletionTimestamp")
        and any(
            owner.get("uid") in replicas for owner in pod["metadata"].get("ownerReferences", [])
        )
    ]
    if len(pods) != deployment["spec"].get("replicas", 1) or any(
        not ready(pod) or cilium_policy.images(pod["spec"]) != images for pod in pods
    ):
        raise OperationError("ops.component.not_ready")
