# SPDX-License-Identifier: MIT
"""Fresh worker prerequisites and stdin-only kubeadm join credentials."""
import json
import os
from pathlib import Path
import re
import tempfile

from ..collector import identity_facts
from ..configuration import normalize
from ..models import OperationError
from . import common, stage_assets, private_process, enrollment


def parameters(value):
    if not isinstance(value, dict) or set(value) != {"configuration", "node"}:
        raise OperationError("ops.change.invalid_parameters")
    config = normalize(value["configuration"])
    node = next((n for n in config["nodes"] if n["name"] == value["node"]), None)
    if (
        node is None
        or not re.fullmatch(r"w[1-9][0-9]*", node["name"])
        or node["dataDevice"] not in ("none", "existing")
    ):
        raise OperationError("ops.lifecycle.join_storage")
    return config, node


def observe(value):
    config, node = parameters(value)
    identity = identity_facts()
    if identity["hostname"] != node["name"]:
        raise OperationError("ops.change.wrong_identity")
    if (
        node["identityFingerprint"]
        and node["identityFingerprint"] != identity["identityFingerprint"]
    ):
        raise OperationError("ops.change.wrong_identity")
    _, output = common.run(["ip", "-j", "-4", "address", "show", "dev", node["interface"]])
    addresses = [
        row["local"]
        for link in json.loads(output)
        for row in link.get("addr_info", [])
        if "local" in row
    ]
    if node["address"] not in addresses:
        raise OperationError("ops.change.wrong_identity")
    ready = Path("/var/lib/k8s-deploy/ready").is_file()
    if Path('/var/lib/k8s-deploy/materials.json').exists():
        code,_=common.run(['python3','/usr/local/lib/k8s-deploy/scripts/operations/materials/runtime.py',
                          'check-versions',config['versions']['kubernetes'],config['versions']['cilium']],check=False)
        if code!=0:
            raise OperationError('ops.materials.invalid')
    joined = Path("/etc/kubernetes/kubelet.conf").exists()
    if not ready or Path("/etc/kubernetes/admin.conf").exists():
        raise OperationError("ops.lifecycle.join_state")
    if joined:
        _, endpoint = common.run(
            [
                "kubectl",
                "--kubeconfig=/etc/kubernetes/kubelet.conf",
                "config",
                "view",
                "--minify",
                "-o",
                "jsonpath={.clusters[0].cluster.server}",
            ]
        )
        if endpoint.strip() != "https://" + config["cluster"]["apiVIP"] + ":" + str(
            config["cluster"]["apiPort"]
        ):
            raise OperationError("ops.change.wrong_cluster")
    return dict(
        identity=identity, phase1Ready=ready, kubeletState=joined, enrollment=enrollment.state()
    )


def finalize(value, run_id):
    if not observe(value)["kubeletState"]:
        raise OperationError("ops.lifecycle.join_state")
    return enrollment.close()


def prepare(value, run_id):
    config, node = parameters(value)
    if observe(value)["kubeletState"]:
        raise OperationError("ops.lifecycle.join_state")
    directory = common.journal(run_id, "node-join")
    try:
        with stage_assets.installed() as assets:
            env = [
                "env",
                "ATLAS_LANG=en",
                "ATLAS_LOCALES_DIR=" + str(assets / "locales"),
                "DATA_DEVICE=" + node["dataDevice"],
                "IFACE=" + node["interface"],
                "K8S_TEST_MODE=" + ("1" if config["cluster"]["profile"] == "test" else "0"),
                "K8S_VERSION=" + config["versions"]["kubernetes"][1:],
                "K8S_DEB_VERSION=" + config["versions"]["kubernetes"][1:] + "-1.1",
            ]
            private_process.run(
                [
                    *env,
                    "bash",
                    str(assets / "stages/setup-k8s-node.sh"),
                    node["address"],
                    node["name"],
                    "worker",
                    "--k8s-only",
                    "--yes",
                ],
                directory / "prerequisites.log",
                timeout=1800,
            )
            private_process.run(
                [
                    *env,
                    "bash",
                    str(assets / "stages/configure-registry-mirror.sh"),
                    "https://k8s.m.daocloud.io",
                    config["cluster"]["localRegistryHost"],
                ],
                directory / "registry.log",
                timeout=180,
            )
        common.record(directory, "node-join", "prerequisites-ready", node=node["name"])
        return dict(prepared=True)
    except BaseException:
        common.record(directory, "node-join", "failed", node=node["name"])
        raise


def join(value, run_id):
    if not isinstance(value, dict) or set(value) != {"configuration", "node", "token", "caHash"}:
        raise OperationError("ops.change.invalid_parameters")
    public = {key: value[key] for key in ("configuration", "node")}
    config, node = parameters(public)
    if not re.fullmatch(r"[a-z0-9]{6}\.[a-z0-9]{16}", value["token"]) or not re.fullmatch(
        r"sha256:[a-f0-9]{64}", value["caHash"]
    ):
        raise OperationError("ops.lifecycle.join_state")
    if observe(public)["kubeletState"]:
        raise OperationError("ops.lifecycle.join_state")
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{64}", run_id):
        raise OperationError("ops.change.invalid_parameters")
    directory = common.JOURNAL_DIRECTORY / run_id
    record = json.loads(common.read_owned(directory / "state.json"))
    if (
        record.get("capability") != "node-join"
        or record.get("state") != "prerequisites-ready"
        or record.get("node") != node["name"]
    ):
        raise OperationError("ops.lifecycle.join_state")
    payload = dict(
        apiVersion="kubeadm.k8s.io/v1beta4",
        kind="JoinConfiguration",
        nodeRegistration=dict(
            name=node["name"],
            criSocket="unix:///run/containerd/containerd.sock",
            kubeletExtraArgs=[dict(name="node-ip", value=node["address"])],
        ),
        discovery=dict(
            bootstrapToken=dict(
                token=value["token"],
                apiServerEndpoint=config["cluster"]["apiVIP"]
                + ":"
                + str(config["cluster"]["apiPort"]),
                caCertHashes=[value["caHash"]],
            )
        ),
    )
    common.record(directory, "node-join", "joining", node=node["name"])
    try:
        with tempfile.TemporaryDirectory(prefix=".atlas-join-") as temporary:
            file = Path(temporary) / "join.json"
            common.replace(file, json.dumps(payload).encode())
            private_process.run(
                ["kubeadm", "join", "--config", str(file)], directory / "join.log", timeout=600
            )
        common.record(directory, "node-join", "succeeded", node=node["name"])
        return dict(joined=True)
    except BaseException:
        common.record(directory, "node-join", "failed", node=node["name"])
        raise
