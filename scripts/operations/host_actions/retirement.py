# SPDX-License-Identifier: MIT
"""Quiesce a verified worker; preserve its disks and configuration for inspection."""
import json
from pathlib import Path
import re

from . import common, node
from ..models import OperationError

MARKER = Path("/var/lib/atlas-kubekit/retired-worker.json")
CRI = ["crictl", "--runtime-endpoint=unix:///run/containerd/containerd.sock"]


def observe(value):
    facts = node.observe(value)
    manifests = Path("/etc/kubernetes/manifests")
    # kubeadm leaves .kubelet-keep on workers; kubelet ignores dot files.
    if manifests.exists() and any(not path.name.startswith(".") for path in manifests.iterdir()):
        raise OperationError("ops.lifecycle.retire_state")
    saved = json.loads(common.read_owned(MARKER)) if MARKER.exists() else None
    if saved is not None and (
        not isinstance(saved, dict)
        or set(saved)
        != {"schemaVersion", "node", "nodeUID", "clusterIdentity", "planId", "state", "podUIDs"}
        or saved["schemaVersion"] != 1
        or saved["node"] != value["node"]
        or saved["state"] not in ("quiescing", "retired")
    ):
        raise OperationError("ops.lifecycle.retire_state")
    facts["retirement"] = saved
    return facts


def apply(value, run_id):
    if not isinstance(value, dict) or set(value) != {
        "configuration",
        "node",
        "nodeUID",
        "clusterIdentity",
        "podUIDs",
    }:
        raise OperationError("ops.change.invalid_parameters")
    for key in ("nodeUID", "clusterIdentity"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[a-zA-Z0-9-]{8,128}", value[key]):
            raise OperationError("ops.change.invalid_parameters")
    if not isinstance(value["podUIDs"], list) or any(
        not isinstance(item, str) or not re.fullmatch(r"[a-zA-Z0-9-]{8,128}", item)
        for item in value["podUIDs"]
    ):
        raise OperationError("ops.change.invalid_parameters")
    public = {key: value[key] for key in ("configuration", "node")}
    facts = observe(public)
    saved = facts["retirement"]
    if saved and any(saved[key] != value[key] for key in ("nodeUID", "clusterIdentity")):
        raise OperationError("ops.change.wrong_identity")
    allowed = set(value["podUIDs"]) | (set(saved["podUIDs"]) if saved else set())
    directory = common.journal(run_id, "node-retire")
    MARKER.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = dict(
        schemaVersion=1,
        node=value["node"],
        nodeUID=value["nodeUID"],
        clusterIdentity=value["clusterIdentity"],
        planId=run_id,
        state="quiescing",
        podUIDs=sorted(allowed),
    )
    common.replace(MARKER, json.dumps(record).encode())
    common.run(["systemctl", "disable", "--now", "kubelet"])
    _, raw = common.run([*CRI, "pods", "-o", "json"])
    for pod in json.loads(raw).get("items", []):
        if pod.get("state") != "SANDBOX_READY":
            continue
        if pod.get("labels", {}).get("io.kubernetes.pod.uid") not in allowed or not re.fullmatch(
            r"[a-f0-9]{64}", pod.get("id", "")
        ):
            raise OperationError("ops.lifecycle.retire_state")
        common.run([*CRI, "stopp", pod["id"]])
    active, _ = common.run(["systemctl", "is-active", "kubelet"], check=False)
    if active == 0:
        raise OperationError("ops.lifecycle.retire_state")
    record["state"] = "retired"
    common.replace(MARKER, json.dumps(record).encode())
    common.record(directory, "node-retire", "succeeded", node=value["node"])
    return dict(quiesced=True, disksPreserved=True)
