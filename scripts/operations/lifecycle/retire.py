# SPDX-License-Identifier: MIT
"""Drain and quiesce one worker before UID-bound API removal and publication."""
import copy
import re

from .. import configuration_cluster
from ..configuration import normalize, revision
from ..environment import check_identity
from ..models import OperationError
from . import maintenance, retirement_storage
from .rules import node_fact, ready
from .health import healthy_etcd, wait_workloads


def from_arguments(args, configuration, environment):
    node = environment.client.get("node", args.node)
    return dict(
        configuration=configuration,
        node=args.node,
        nodeUID=node["metadata"]["uid"],
        deleteEmptyDir=args.delete_emptydir,
    )


def preview(values):
    return (("ops.preview.retire", (values["node"], values["nodeUID"])),)


def parameters(value):
    if not isinstance(value, dict) or set(value) != {
        "configuration",
        "node",
        "nodeUID",
        "deleteEmptyDir",
    }:
        raise OperationError("ops.change.invalid_parameters")
    config = normalize(value["configuration"])
    if (
        not isinstance(value["node"], str)
        or not re.fullmatch(r"w[1-9][0-9]*", value["node"])
        or value["node"] not in {item["name"] for item in config["nodes"]}
        or not isinstance(value["nodeUID"], str)
        or not re.fullmatch(r"[a-zA-Z0-9-]{8,128}", value["nodeUID"])
        or type(value["deleteEmptyDir"]) is not bool
    ):
        raise OperationError("ops.lifecycle.retire_state")
    return dict(
        configuration=config,
        node=value["node"],
        nodeUID=value["nodeUID"],
        deleteEmptyDir=value["deleteEmptyDir"],
    )


def reduced(values):
    config = copy.deepcopy(values["configuration"])
    config["nodes"] = [node for node in config["nodes"] if node["name"] != values["node"]]
    return normalize(config)


def observe(value, environment):
    values = parameters(value)
    config, name = values["configuration"], values["node"]
    candidate = next(node for node in config["nodes"] if node["name"] == name)
    _, nodes = environment.verify(reduced(values), additional=(candidate,))
    saved = configuration_cluster.read(environment.client)
    if saved is not None and saved not in (config, reduced(values)):
        raise OperationError("ops.config.conflict")
    healthy_etcd(environment.client, nodes)
    if any(not ready(node) for key, node in nodes.items() if key != name):
        raise OperationError("ops.lifecycle.not_ready")
    host = environment.host(config, name).call(
        "retirement-observe", dict(configuration=config, node=name)
    )
    check_identity(config, name, host["identity"])
    marker = host["retirement"]
    if marker and (
        marker["nodeUID"] != values["nodeUID"]
        or marker["clusterIdentity"] != environment.client.cluster_identity()
    ):
        raise OperationError("ops.change.wrong_identity")
    actual = nodes.get(name)
    if actual is None:
        if marker is None or marker["state"] != "retired":
            raise OperationError("ops.lifecycle.retire_state")
        storage = None
    else:
        if actual["metadata"]["uid"] != values["nodeUID"]:
            raise OperationError("ops.change.wrong_identity")
        if any(
            pod["metadata"].get("annotations", {}).get("kubernetes.io/config.mirror")
            for pod in environment.client.node_pods(name)
        ):
            raise OperationError("ops.lifecycle.retire_state")
        storage = retirement_storage.require_evacuated(environment.client, actual, nodes.values())
        for pod in environment.client.node_pods(name):
            if (any(volume.get('hostPath') for volume in pod.get('spec',{}).get('volumes',[]))
                    and any(owner.get('kind')=='DaemonSet' for owner in pod['metadata'].get('ownerReferences',[]))
                    and not (pod['metadata']['namespace']=='kube-system' and any(
                        owner.get('name') in ('cilium','cilium-envoy','kube-proxy')
                        for owner in pod['metadata'].get('ownerReferences',[])))):
                raise OperationError('ops.lifecycle.local_data')
        maintenance.observe(
            dict(
                configuration=config,
                node=name,
                action="drain",
                deleteEmptyDir=values["deleteEmptyDir"],
            ),
            environment,
        )
    return dict(
        node=node_fact(actual) if actual else None,
        host=host,
        storage=storage,
        sharedRevision=revision(saved) if saved else None,
    )


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    facts = observe(values, environment)
    config, name = values["configuration"], values["node"]
    if facts["node"] is not None:
        checkpoint("drain-" + name)
        maintenance.apply(
            dict(
                configuration=config,
                node=name,
                action="drain",
                deleteEmptyDir=values["deleteEmptyDir"],
            ),
            environment,
            checkpoint,
            run_id,
        )
        checkpoint("detach-volumes-" + name)
        wait_workloads(environment.client)
        retirement_storage.wait_detached(environment.client, name)
        remaining = environment.client.node_pods(name)
        pod_uids = []
        for pod in remaining:
            if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            if not any(
                owner.get("kind") == "DaemonSet"
                for owner in pod["metadata"].get("ownerReferences", [])
            ):
                raise OperationError("ops.lifecycle.retire_state")
            pod_uids.append(pod["metadata"]["uid"])
        checkpoint("quiesce-" + name)
        environment.host(config, name).call(
            "retirement-apply",
            dict(
                configuration=config,
                node=name,
                nodeUID=values["nodeUID"],
                clusterIdentity=environment.client.cluster_identity(),
                podUIDs=pod_uids,
            ),
            run_id,
            timeout=300,
        )
        checkpoint("remove-" + name)
        environment.client.delete_node(name, values["nodeUID"])
    desired = reduced(values)
    environment.verify(desired)
    wait_workloads(environment.client)
    checkpoint("publish-configuration")
    configuration_cluster.publish(environment.client, desired, expected=facts["sharedRevision"])
    return dict(
        node=name, state="retired", disksPreserved=True, configurationRevision=revision(desired)
    )
