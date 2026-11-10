"""Private lab manifest and resource ownership contracts."""

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class Node:
    name: str
    address: str
    vmid: int
    interface: str


@dataclass(frozen=True)
class Lab:
    id: str
    nodes: tuple
    ssh_config: str
    material_id: str
    public_key: str
    offline_gateway: str = None


def read(file):
    file = Path(file)
    if (
        file.is_symlink()
        or file.stat().st_uid != os.geteuid()
        or file.stat().st_mode & 0o077
        or file.stat().st_size > 128 * 1024
    ):
        raise ValueError("Lab manifest must be a private owned file")
    data = json.loads(file.read_text())
    if data["schemaVersion"] != 1 or not re.fullmatch(r"atlas-e2e-[a-z0-9-]{1,40}", data["labId"]):
        raise ValueError("Invalid disposable lab identity")
    network = ipaddress.ip_network(data["network"])
    if not network.is_private or network.version != 4:
        raise ValueError("Lab nodes must use an isolated private subnet")
    nodes = tuple(Node(**row) for row in data["nodes"])
    if (
        not 3 <= len(nodes) <= 8
        or len({n.name for n in nodes}) != len(nodes)
        or len({n.address for n in nodes}) != len(nodes)
        or len({n.vmid for n in nodes}) != len(nodes)
    ):
        raise ValueError("Invalid bounded lab inventory")
    for node in nodes:
        if (
            not re.fullmatch(r"(lb|cp|w)[1-9][0-9]*", node.name)
            or ipaddress.ip_address(node.address) not in network
            or type(node.vmid) is not int
            or node.vmid < 200
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", node.interface)
        ):
            raise ValueError("Invalid lab node")
    if not re.fullmatch(r"[a-f0-9]{64}", data["materialId"]):
        raise ValueError("Expected immutable material identity")
    key = Path(data["adminPublicKey"]).read_text().strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?", key):
        raise ValueError("Expected an Ed25519 public key")
    ssh_config = Path(data["sshConfig"]).resolve(strict=True)
    gateway = data.get("offlineGateway")
    if gateway is not None and (
        ipaddress.ip_address(gateway) not in network or gateway in {n.address for n in nodes}
    ):
        raise ValueError("Invalid isolated internal gateway")
    return Lab(data["labId"], nodes, str(ssh_config), data["materialId"], key, gateway)
