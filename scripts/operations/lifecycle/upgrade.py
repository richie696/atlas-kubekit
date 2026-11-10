# SPDX-License-Identifier: MIT
"""Serial, resumable same-minor Kubernetes patch upgrades with eviction guards."""
import copy
import re
import time

from .. import configuration_cluster
from ..configuration import normalize, revision
from ..environment import check_identity
from ..models import OperationError
from ..changes.coordinator import ClusterCoordinator
from . import maintenance, dns_layout, scheduling
from .rules import node_fact, ready
from .health import healthy_etcd, require_workloads, wait_workloads


def from_arguments(args, configuration, environment):
    return dict(
        configuration=configuration,
        version=args.to_version,
        deleteEmptyDir=args.delete_emptydir,
        dnsLayout=dns_layout.capture(environment.client),
    )


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", value):
        raise OperationError("ops.upgrade.version")
    return tuple(int(part) for part in value[1:].split("."))


def preview(values):
    return (
        (
            "ops.preview.upgrade",
            (
                values["configuration"]["versions"]["kubernetes"],
                values["version"],
                ", ".join(
                    node["name"]
                    for node in values["configuration"]["nodes"]
                    if not node["name"].startswith("lb")
                ),
            ),
        ),
    )


def parameters(value):
    if (
        not isinstance(value, dict)
        or set(value) - {"configuration", "version", "deleteEmptyDir", "dnsLayout", "resumeOwners"}
        or not {"configuration", "version", "deleteEmptyDir"} <= set(value)
    ):
        raise OperationError("ops.change.invalid_parameters")
    config = normalize(value["configuration"])
    source, target = version(config["versions"]["kubernetes"]), version(value["version"])
    if source[:2] != target[:2] or target <= source or type(value["deleteEmptyDir"]) is not bool:
        raise OperationError("ops.upgrade.version")
    owners = value.get("resumeOwners", [])
    if (
        not isinstance(owners, list)
        or len(owners) > 32
        or any(
            not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item) for item in owners
        )
        or len(set(owners)) != len(owners)
    ):
        raise OperationError("ops.change.invalid_parameters")
    return dict(
        configuration=config,
        version=value["version"],
        deleteEmptyDir=value["deleteEmptyDir"],
        dnsLayout=(
            dns_layout.validate(value["dnsLayout"]) if value.get("dnsLayout") is not None else None
        ),
        resumeOwners=owners,
    )


def drain_values(values, name, action="drain"):
    return dict(
        configuration=values["configuration"],
        node=name,
        action=action,
        deleteEmptyDir=values["deleteEmptyDir"],
    )


def observe(value, environment):
    values = parameters(value)
    config, nodes = environment.verify(values["configuration"])
    saved = configuration_cluster.read(environment.client)
    desired = copy.deepcopy(config)
    desired["versions"]["kubernetes"] = values["version"]
    if saved is not None and saved not in (config, desired):
        raise OperationError("ops.config.conflict")
    coordinator = ClusterCoordinator(environment.client)
    for old_id in values["resumeOwners"]:
        record = coordinator.journal(old_id)
        if (
            not record
            or record.get("capability") != "cluster-upgrade"
            or record["state"] not in ("failed", "cancelled")
        ):
            raise OperationError("ops.change.incomplete")
    for node in nodes.values():
        if (
            node.get("spec", {}).get("unschedulable")
            and scheduling.owner(node) not in values["resumeOwners"]
        ):
            raise OperationError("ops.upgrade.maintenance_owner")
    accepted = {config["versions"]["kubernetes"], values["version"]}
    if environment.client.version() not in accepted or any(
        not ready(node)
        or node.get("status", {}).get("nodeInfo", {}).get("kubeletVersion") not in accepted
        for node in nodes.values()
    ):
        raise OperationError("ops.upgrade.version")
    planes = healthy_etcd(environment.client, nodes)
    workloads = require_workloads(environment.client)
    current_dns = dns_layout.capture(environment.client)
    # A stopped upgrade may have completed a control plane before restoring
    # kubeadm's DNS changes. The continued plan records that drift; the completed
    # control-plane branch repairs it before any new node can be cordoned.
    pending_dns_restore = bool(
        values["resumeOwners"]
        and values["dnsLayout"]
        and current_dns != values["dnsLayout"]
        and nodes[planes[0]]["status"]["nodeInfo"]["kubeletVersion"] == values["version"]
    )
    dns_layout.check_server(
        environment.client, values["dnsLayout"] or dns_layout.capture(environment.client)
    )
    result = {}
    for name, node in sorted(nodes.items()):
        facts = environment.host(config, name).call(
            "upgrade-observe", dict(version=values["version"])
        )
        check_identity(config, name, facts["identity"])
        accepted_packages = {item[1:] + "-1.1" for item in accepted}
        if set(facts["installed"]) != {"kubeadm", "kubelet", "kubectl"} or any(
            installed not in accepted_packages for installed in facts["installed"].values()
        ):
            raise OperationError("ops.upgrade.version")
        result[name] = dict(node=node_fact(node), host=facts)
        if node["status"]["nodeInfo"]["kubeletVersion"] != values["version"] or any(
            installed != values["version"][1:] + "-1.1" for installed in facts["installed"].values()
        ):
            try:
                maintenance.observe(drain_values(values, name), environment)
            except OperationError as error:
                if not pending_dns_restore or error.key != "ops.lifecycle.pdb":
                    raise
                result[name]["drainCheckDeferredUntilDnsRestore"] = True
    return dict(
        nodes=result,
        controlPlanes=planes,
        sharedRevision=revision(saved) if saved else None,
        workloads=workloads,
        dnsLayout=current_dns,
    )


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    facts = observe(values, environment)
    original_dns = values["dnsLayout"] or facts["dnsLayout"]
    config = values["configuration"]
    order = facts["controlPlanes"] + sorted(set(facts["nodes"]) - set(facts["controlPlanes"]))
    results = {}
    for name in order:
        node = environment.client.get("node", name)
        if (
            node["status"]["nodeInfo"]["kubeletVersion"] == values["version"]
            and ready(node)
            and all(
                installed == values["version"][1:] + "-1.1"
                for installed in facts["nodes"][name]["host"]["installed"].values()
            )
        ):
            results[name] = dict(state="already-current")
            if (
                node.get("spec", {}).get("unschedulable")
                and scheduling.owner(node) in values["resumeOwners"]
            ):
                checkpoint("restore-scheduling-" + name)
                maintenance.apply(
                    drain_values(values, name, "uncordon"), environment, checkpoint, run_id
                )
                wait_workloads(environment.client)
            if name in facts["controlPlanes"]:
                checkpoint("restore-dns-layout-" + name)
                dns_layout.restore(environment.client, original_dns)
                wait_workloads(environment.client)
            continue
        checkpoint("drain-" + name)
        _, current = environment.verify(config)
        healthy_etcd(environment.client, current)
        deadline = time.monotonic() + 120
        while True:
            try:
                maintenance.observe(drain_values(values, name), environment)
                break
            except OperationError as error:
                if error.key != "ops.lifecycle.pdb" or time.monotonic() >= deadline:
                    raise
                time.sleep(1)
        maintenance.apply(drain_values(values, name), environment, checkpoint, run_id)
        checkpoint("upgrade-" + name)
        results[name] = environment.host(config, name).call(
            "upgrade-apply",
            dict(version=values["version"], firstControlPlane=name == facts["controlPlanes"][0]),
            run_id,
            timeout=2400,
        )
        checkpoint("verify-" + name)
        environment.client.call(
            [
                "wait",
                "--for=jsonpath={.status.nodeInfo.kubeletVersion}=" + values["version"],
                "node/" + name,
                "--timeout=5m",
            ],
            timeout=330,
            json_output=False,
        )
        environment.client.call(
            ["wait", "--for=condition=Ready", "node/" + name, "--timeout=5m"],
            timeout=330,
            json_output=False,
        )
        actual = environment.client.get("node", name)
        if actual["status"]["nodeInfo"]["kubeletVersion"] != values["version"]:
            raise OperationError("ops.upgrade.version")
        maintenance.apply(drain_values(values, name, "uncordon"), environment, checkpoint, run_id)
        if name in facts["controlPlanes"]:
            # kubeadm may rewrite replica count and placement. Repair this before
            # evaluating the next node's disruption budget, not only at the end.
            checkpoint("restore-dns-layout-" + name)
            dns_layout.restore(environment.client, original_dns)
        checkpoint("workloads-" + name)
        wait_workloads(environment.client)
    if environment.client.version() != values["version"]:
        raise OperationError("ops.upgrade.version")
    _, actual = environment.verify(config)
    healthy_etcd(environment.client, actual)
    checkpoint("restore-dns-layout")
    dns_layout.restore(environment.client, original_dns)
    wait_workloads(environment.client)
    updated = copy.deepcopy(config)
    updated["versions"]["kubernetes"] = values["version"]
    checkpoint("publish-configuration")
    configuration_cluster.publish(environment.client, updated, expected=facts["sharedRevision"])
    return dict(version=values["version"], nodes=results, configurationRevision=revision(updated))
