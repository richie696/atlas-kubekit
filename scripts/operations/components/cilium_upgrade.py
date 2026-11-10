# SPDX-License-Identifier: MIT
"""Cilium patch rollout uses PDB eviction per node while automatic rollout is frozen."""
import base64
import copy
from pathlib import Path
import re

from .. import configuration_cluster
from ..configuration import normalize, revision
from ..changes.coordinator import ClusterCoordinator
from ..environment import check_identity
from ..lifecycle.health import healthy_etcd, require_workloads, wait_workloads
from ..lifecycle.rules import ready, require_pdbs, node_fact
from ..models import OperationError
from . import cilium_chart, cilium_policy
from .cilium_rollout import daemon_facts, target_pod, wait_pod, wait_agent, verify_operator


def capture(client):
    result = {}
    for name in ("cilium", "cilium-envoy"):
        value = client.get("daemonset", name, "kube-system", optional=True)
        if value is None:
            if name == "cilium":
                raise OperationError("ops.component.release")
            continue
        result[name] = dict(
            uid=value["metadata"]["uid"],
            strategy=cilium_policy.strategy(value["spec"]["updateStrategy"]),
        )
    return result


def capture_operator(client):
    value = client.get("deployment", "cilium-operator", "kube-system")
    return dict(
        uid=value["metadata"]["uid"], strategy=cilium_policy.strategy(value["spec"]["strategy"])
    )


def from_arguments(args, configuration, environment):
    if not args.to_version or not args.chart:
        raise OperationError("ops.change.invalid_parameters")
    target = str(args.to_version)
    if target.startswith("v"):
        target = target[1:]
    _, digest = cilium_chart.read(args.chart, target)
    return dict(
        configuration=configuration,
        version=target,
        chartFile=str(Path(args.chart).expanduser().absolute()),
        chartSha=digest,
        policies=capture(environment.client),
        operatorPolicy=capture_operator(environment.client),
        resumeOf="",
        rolloutOwner="",
    )


def preview(values):
    return (("ops.preview.component", (values["version"], values["chartSha"])),)


def parameters(value):
    if not isinstance(value, dict) or set(value) != {
        "configuration",
        "version",
        "chartFile",
        "chartSha",
        "policies",
        "operatorPolicy",
        "resumeOf",
        "rolloutOwner",
    }:
        raise OperationError("ops.change.invalid_parameters")
    config = normalize(value["configuration"])
    source = cilium_policy.version(config["versions"]["cilium"])
    target = cilium_policy.version(value["version"])
    kube = tuple(int(x) for x in config["versions"]["kubernetes"].lstrip("v").split("."))
    if (
        source[:2] != (1, 20)
        or target[:2] != source[:2]
        or target <= source
        or kube[0] != 1
        or not 33 <= kube[1] <= 36
    ):
        raise OperationError("ops.component.version")
    for key in ("chartSha", "resumeOf", "rolloutOwner"):
        if not isinstance(value[key], str) or not re.fullmatch(
            r"[a-f0-9]{64}" if key == "chartSha" else r"(?:[a-f0-9]{64})?", value[key]
        ):
            raise OperationError("ops.change.invalid_parameters")
    if not isinstance(value["chartFile"], str) or not value["chartFile"].startswith("/"):
        raise OperationError("ops.change.invalid_parameters")
    if bool(value["resumeOf"]) != bool(value["rolloutOwner"]):
        raise OperationError("ops.change.invalid_parameters")
    policies = value["policies"]
    if (
        not isinstance(policies, dict)
        or "cilium" not in policies
        or set(policies) - {"cilium", "cilium-envoy"}
    ):
        raise OperationError("ops.component.policy")
    for item in [*policies.values(), value["operatorPolicy"]]:
        if (
            not isinstance(item, dict)
            or set(item) != {"uid", "strategy"}
            or not isinstance(item["uid"], str)
            or not re.fullmatch(r"[a-zA-Z0-9-]{8,128}", item["uid"])
        ):
            raise OperationError("ops.component.policy")
        cilium_policy.strategy(item["strategy"])
    if value["operatorPolicy"]["strategy"]["type"] != "RollingUpdate":
        raise OperationError("ops.component.policy")
    return copy.deepcopy(dict(value, configuration=config))


def request(values):
    raw, _ = cilium_chart.read(values["chartFile"], values["version"], values["chartSha"])
    return dict(version=values["version"], chart=base64.b64encode(raw).decode())


def observe(value, environment):
    values = parameters(value)
    config, nodes = environment.verify(values["configuration"])
    saved = configuration_cluster.read(environment.client)
    desired = copy.deepcopy(config)
    desired["versions"]["cilium"] = values["version"]
    if saved is not None and saved not in (config, desired):
        raise OperationError("ops.config.conflict")
    if values["resumeOf"]:
        record = ClusterCoordinator(environment.client).journal(values["resumeOf"])
        if (
            not record
            or record.get("capability") != "component-upgrade"
            or record["state"] not in ("failed", "cancelled")
        ):
            raise OperationError("ops.change.incomplete")
    healthy_etcd(environment.client, nodes, require_ready=not bool(values["resumeOf"]))
    if not values["resumeOf"]:
        if not all(ready(node) for node in nodes.values()):
            raise OperationError("ops.lifecycle.not_ready")
        require_workloads(environment.client)
    host = environment.host(config, "cp1").call("component-observe", request(values), timeout=330)
    check_identity(config, "cp1", host["identity"])
    info = host["release"]
    initial = (
        info["status"] == "deployed" and info["chart"] == "cilium-" + config["versions"]["cilium"]
    )
    continuing = (
        values["resumeOf"]
        and info["status"] in ("deployed", "failed")
        and info["chart"] == "cilium-" + values["version"]
        and info["rolloutOwner"] == values["rolloutOwner"]
    )
    if not initial and not continuing:
        raise OperationError("ops.component.release")
    daemons, pods = daemon_facts(environment.client, values, nodes)
    if capture_operator(environment.client)["uid"] != values["operatorPolicy"]["uid"]:
        raise OperationError("ops.change.stale")
    if set(daemons) != set(host["rendered"]["daemons"]):
        raise OperationError("ops.component.policy")
    if (
        initial
        and not values["resumeOf"]
        and any(
            row["strategy"] != values["policies"][name]["strategy"] for name, row in daemons.items()
        )
    ):
        raise OperationError("ops.change.stale")
    selected = {pod["uid"] for row in daemons.values() for pod in row["pods"]}
    require_pdbs(
        [pod for pod in pods if pod["metadata"]["uid"] in selected],
        environment.client.get("pdb")["items"],
    )
    identities = {}
    for name in sorted(nodes):
        identity = environment.host(config, name).call("security-probe")["identity"]
        check_identity(config, name, identity)
        identities[name] = identity
    return dict(
        host=host,
        identities=identities,
        daemons=daemons,
        nodes=sorted((node_fact(node) for node in nodes.values()), key=lambda row: row["name"]),
        sharedRevision=revision(saved) if saved else None,
    )


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    facts = observe(values, environment)
    config = values["configuration"]
    owner = values["rolloutOwner"] or run_id
    images = sorted(
        {
            image
            for section in [
                *facts["host"]["rendered"]["daemons"].values(),
                facts["host"]["rendered"]["operator"],
            ]
            for image in section.values()
        }
    )
    for node in sorted(item["name"] for item in facts["nodes"]):
        checkpoint("cilium-images-" + node)
        warmed = environment.host(config, node).call(
            "component-images", dict(images=images), run_id, timeout=1800
        )
        check_identity(config, node, warmed["identity"])
    checkpoint("cilium-templates")
    expected = {key: facts["host"][key] for key in ("release", "chartSha", "rendered")}
    environment.host(config, "cp1").call(
        "component-apply",
        dict(request(values), expected=expected, rolloutOwner=owner),
        run_id,
        timeout=960,
    )
    for node in sorted(item["name"] for item in facts["nodes"]):
        _, current = environment.verify(config)
        healthy_etcd(environment.client, current, require_ready=not bool(values["resumeOf"]))
        for daemon in sorted(values["policies"], reverse=True):
            checkpoint(daemon + "-" + node)
            pod = target_pod(environment.client, values, daemon, node)
            wanted = facts["host"]["rendered"]["daemons"][daemon]
            if (
                pod
                and cilium_policy.images(pod["spec"]) == wanted
                and pod["metadata"].get("annotations", {}).get(cilium_policy.OWNER) == owner
                and ready(pod)
            ):
                continue
            if pod:
                require_pdbs([pod], environment.client.get("pdb")["items"])
                environment.client.evict_pod(
                    "kube-system", pod["metadata"]["name"], pod["metadata"]["uid"]
                )
            wait_pod(environment.client, values, daemon, node, wanted, owner)
        agent = target_pod(environment.client, values, "cilium", node)
        if agent is None:
            raise OperationError("ops.component.not_ready")
        wait_agent(environment.client, agent)
        environment.client.call(
            ["wait", "--for=condition=Ready", "node/" + node, "--timeout=3m"],
            timeout=200,
            json_output=False,
        )
        wait_workloads(environment.client)
    verify_operator(environment.client, values, facts["host"]["rendered"]["operator"])
    checkpoint("restore-cilium-strategies")
    environment.host(config, "cp1").call(
        "component-complete",
        dict(
            request(values),
            rolloutOwner=owner,
            policies=values["policies"],
            operatorPolicy=values["operatorPolicy"],
        ),
        run_id,
        timeout=960,
    )
    wait_workloads(environment.client)
    final, _ = daemon_facts(environment.client, values, [item["name"] for item in facts["nodes"]])
    for name, row in final.items():
        if row["strategy"] != values["policies"][name]["strategy"] or any(
            not pod["ready"] or pod["images"] != facts["host"]["rendered"]["daemons"][name]
            for pod in row["pods"]
        ):
            raise OperationError("ops.component.not_ready")
    if capture_operator(environment.client) != values["operatorPolicy"]:
        raise OperationError("ops.component.policy")
    _, nodes = environment.verify(config)
    healthy_etcd(environment.client, nodes)
    checkpoint("publish-configuration")
    updated = copy.deepcopy(config)
    updated["versions"]["cilium"] = values["version"]
    configuration_cluster.publish(environment.client, updated, expected=facts["sharedRevision"])
    return dict(
        component="cilium",
        version=values["version"],
        nodes=len(nodes),
        configurationRevision=revision(updated),
    )


def continue_parameters(values, environment):
    current = configuration_cluster.read(environment.client)
    if current is None:
        return values
    original = values["configuration"]
    for field in ("name", "apiVIP", "apiPort", "podCIDR", "serviceCIDR"):
        if current["cluster"][field] != original["cluster"][field]:
            raise OperationError("ops.change.wrong_cluster")
    rebased = copy.deepcopy(current)
    rebased["versions"]["cilium"] = original["versions"]["cilium"]
    return dict(values, configuration=rebased)
