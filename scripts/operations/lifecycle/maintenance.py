# SPDX-License-Identifier: MIT
"""Cordon, PDB-respecting drain and verified return to scheduling."""
import re

from ..configuration import normalize
from ..models import OperationError
from .rules import node_fact, ready, require_quorum, movable_pods, require_pdbs, control_plane
from .health import healthy_etcd
from . import scheduling


def from_arguments(args, configuration, environment):
    return dict(
        configuration=configuration,
        node=args.node,
        action=args.maintenance_action,
        deleteEmptyDir=args.delete_emptydir,
    )


def preview(values):
    return (
        (
            "ops.preview.maintenance",
            (values["node"], values["action"], str(values["deleteEmptyDir"]).lower()),
        ),
    )


def parameters(value):
    if not isinstance(value, dict) or set(value) != {
        "configuration",
        "node",
        "action",
        "deleteEmptyDir",
    }:
        raise OperationError("ops.change.invalid_parameters")
    config = normalize(value["configuration"])
    name = value["node"]
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"(?:cp|w)[1-9][0-9]*", name)
        or name not in {n["name"] for n in config["nodes"]}
        or value["action"] not in ("cordon", "drain", "uncordon")
        or type(value["deleteEmptyDir"]) is not bool
    ):
        raise OperationError("ops.change.invalid_parameters")
    return dict(
        configuration=config,
        node=name,
        action=value["action"],
        deleteEmptyDir=value["deleteEmptyDir"],
    )


def drain_arguments(values, dry_run=False):
    args = [
        "drain",
        values["node"],
        "--ignore-daemonsets",
        "--timeout=30s" if dry_run else "--timeout=5m",
    ]
    if values["deleteEmptyDir"]:
        args.append("--delete-emptydir-data")
    if dry_run:
        args += ["--dry-run=server"]
    return args


def observe(values, environment):
    values = parameters(values)
    config, nodes = environment.verify(values["configuration"])
    target = nodes[values["node"]]
    facts = dict(
        node=node_fact(target),
        nodes=sorted((node_fact(n) for n in nodes.values()), key=lambda n: n["name"]),
    )
    if values["action"] == "uncordon":
        if not ready(target):
            raise OperationError("ops.lifecycle.not_ready")
        return facts
    require_quorum(nodes, values["node"])
    if control_plane(target):
        healthy_etcd(environment.client, nodes)
    if values["action"] == "drain":
        pods = environment.client.node_pods(values["node"])
        facts["pods"] = movable_pods(pods, values["deleteEmptyDir"])
        evictable = {item["uid"] for item in facts["pods"]}
        pending = [pod for pod in pods if pod["metadata"]["uid"] in evictable]
        for pod in pending:
            for volume in pod.get("spec", {}).get("volumes", []):
                if "persistentVolumeClaim" not in volume:
                    continue
                claim = environment.client.get(
                    "persistentvolumeclaim",
                    volume["persistentVolumeClaim"]["claimName"],
                    pod["metadata"]["namespace"],
                )
                name = claim.get("spec", {}).get("volumeName")
                if not name:
                    raise OperationError("ops.lifecycle.local_data")
                pv = environment.client.get("persistentvolume", name)
                if any(k in pv.get("spec", {}) for k in ("local", "hostPath")):
                    raise OperationError("ops.lifecycle.local_data")
        pdbs = environment.client.get("pdb")["items"]
        require_pdbs(pending, pdbs)
        facts["pdbs"] = sorted(
            (
                dict(
                    namespace=p["metadata"]["namespace"],
                    name=p["metadata"]["name"],
                    uid=p["metadata"]["uid"],
                    selector=p.get("spec", {}).get("selector"),
                    generation=p["metadata"].get("generation"),
                    observedGeneration=p.get("status", {}).get("observedGeneration"),
                    currentHealthy=p.get("status", {}).get("currentHealthy"),
                    desiredHealthy=p.get("status", {}).get("desiredHealthy"),
                    unhealthyPodEvictionPolicy=p.get("spec", {}).get("unhealthyPodEvictionPolicy"),
                    allowed=p.get("status", {}).get("disruptionsAllowed"),
                )
                for p in pdbs
            ),
            key=lambda p: (p["namespace"], p["name"]),
        )
        environment.client.call(drain_arguments(values, True), timeout=45, json_output=False)
    return facts


def apply(values, environment, checkpoint, run_id=None):
    values = parameters(values)
    baseline = observe(values, environment)
    checkpoint("scheduling-" + values["action"] + '-' + values['node'])
    scheduling.change(
        environment.client,
        values["node"],
        baseline["node"]["uid"],
        values["action"] != "uncordon",
        run_id,
    )
    if values["action"] == "drain":
        checkpoint("evict-workloads-" + values['node'])
        environment.client.call(drain_arguments(values), timeout=330, json_output=False)
    actual = environment.client.get("node", values["node"])
    if bool(actual.get("spec", {}).get("unschedulable")) != (values["action"] != "uncordon"):
        raise OperationError("ops.lifecycle.scheduling")
    return dict(
        node=values["node"],
        action=values["action"],
        unschedulable=bool(actual.get("spec", {}).get("unschedulable")),
    )
