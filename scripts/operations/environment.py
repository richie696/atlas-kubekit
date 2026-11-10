# SPDX-License-Identifier: MIT
"""Bind declared inventory to the API endpoint and actual Kubernetes node IPs."""
from dataclasses import dataclass
from urllib.parse import urlsplit

from .adapters.host import Host
from .configuration import normalize
from .models import OperationError


@dataclass(frozen=True)
class Environment:
    client: object
    identity: str = None
    ssh_config: str = None

    def host(self, configuration, name):
        config = normalize(configuration)
        node = next((n for n in config["nodes"] if n["name"] == name), None)
        if node is None:
            raise OperationError("ops.change.invalid_parameters")
        return Host(config["ssh"]["user"] + "@" + node["address"], self.identity, self.ssh_config)

    def verify(self, configuration, additional=()):
        config = normalize(configuration)
        endpoint = urlsplit(self.client.endpoint())
        if (
            endpoint.scheme != "https"
            or endpoint.hostname != config["cluster"]["apiVIP"]
            or endpoint.port != config["cluster"]["apiPort"]
        ):
            raise OperationError("ops.change.wrong_cluster")
        actual = {n["metadata"]["name"]: n for n in self.client.get("node")["items"]}
        required = {n["name"] for n in config["nodes"] if not n["name"].startswith("lb")}
        allowed = required | {n["name"] for n in additional}
        if not required <= set(actual) or not set(actual) <= allowed:
            raise OperationError("ops.lifecycle.inventory")
        for node in [*config["nodes"], *additional]:
            if node["name"].startswith("lb"):
                continue
            observed = actual.get(node["name"])
            if observed is None and node["name"] not in required:
                continue
            if observed is None:
                raise OperationError("ops.lifecycle.inventory")
            is_control_plane = "node-role.kubernetes.io/control-plane" in observed.get(
                "metadata", {}
            ).get("labels", {})
            if is_control_plane != node["name"].startswith("cp"):
                raise OperationError("ops.lifecycle.inventory")
            addresses = observed.get("status", {}).get("addresses", [])
            if not any(
                a.get("type") == "InternalIP" and a.get("address") == node["address"]
                for a in addresses
            ):
                raise OperationError("ops.change.wrong_cluster")
        return config, actual


def check_identity(configuration, name, value):
    node = next(n for n in configuration["nodes"] if n["name"] == name)
    if value.get("identityState") != "ok" or value.get("hostname") != name:
        raise OperationError("ops.change.wrong_identity")
    pin = node.get("identityFingerprint")
    if pin and pin != value.get("identityFingerprint"):
        raise OperationError("ops.change.wrong_identity")
