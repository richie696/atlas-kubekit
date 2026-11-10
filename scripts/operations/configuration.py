# SPDX-License-Identifier: MIT
"""Version-one cluster configuration: pure validation and canonical values."""
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re

from .models import OperationError, fingerprint
from scripts.lib.component_versions import pinned

PINNED_VERSIONS = pinned()
CLUSTER_FIELDS = {
    "name",
    "profile",
    "apiVIP",
    "apiPort",
    "appVIP",
    "podCIDR",
    "serviceCIDR",
    "vrrpID",
    "statsAllowedCIDR",
    "localRegistryHost",
}
NODE_FIELDS = {"name", "address", "interface", "dataDevice", "identityFingerprint"}


def invalid():
    raise OperationError("ops.config.invalid")


def mapping(value, allowed, required):
    if not isinstance(value, dict) or set(value) - set(allowed) or not set(required) <= set(value):
        invalid()
    return value


def text(value, pattern=None):
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 for c in value):
        invalid()
    if pattern is not None and not re.fullmatch(pattern, value):
        invalid()
    return value


def ipv4(value):
    text(value)
    try:
        address = ipaddress.IPv4Address(value)
        if (
            address.is_multicast
            or address.is_loopback
            or address.is_unspecified
            or address.is_link_local
            or int(address) == 2**32 - 1
        ):
            invalid()
        return str(address)
    except ipaddress.AddressValueError:
        invalid()


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        invalid()
    return value


def device(value):
    value = text(value)
    if value in ("", "none", "existing"):
        return value
    path = value[6:] if value.startswith("reuse:") else value
    if (
        not re.fullmatch(r"/[A-Za-z0-9._/-]+", path)
        or ".." in PurePosixPath(path).parts
        or (not value.startswith("reuse:") and not path.startswith("/dev/"))
        or path == "/"
    ):
        invalid()
    return value


def registry(value):
    value = text(value)
    if not value:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9.-]+:[0-9]{1,5}", value):
        invalid()
    host, port = value.rsplit(":", 1)
    if not 1 <= int(port) <= 65535 or len(host) > 253:
        invalid()
    if all(c in "0123456789." for c in host):
        ipv4(host)
    elif any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
        for part in host.split(".")
    ):
        invalid()
    return host.lower() + ":" + str(int(port))


def management_cidr(value):
    value = text(value)
    if not value:
        return ""
    try:
        network = ipaddress.IPv4Network(value, strict=False)
        if network.prefixlen == 0:
            invalid()
        return str(network)
    except ValueError:
        invalid()


def normalize(data):
    """Return a fresh canonical document; neither mutate nor execute the input."""
    mapping(
        data,
        {"schemaVersion", "cluster", "ssh", "versions", "nodes", "registry"},
        {"schemaVersion", "cluster", "ssh", "versions", "nodes"},
    )
    if type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1:
        invalid()
    cluster = mapping(data["cluster"], CLUSTER_FIELDS, {"name", "apiVIP", "podCIDR", "serviceCIDR"})
    ssh = mapping(data["ssh"], {"user"}, {"user"})
    versions = mapping(data["versions"], set(PINNED_VERSIONS), set(PINNED_VERSIONS))
    app = text(cluster.get("appVIP", ""))
    values = dict(
        name=text(cluster["name"], r"[a-z][a-z0-9-]{0,62}"),
        profile=text(cluster.get("profile", "standard"), r"standard|test"),
        apiVIP=ipv4(cluster["apiVIP"]),
        apiPort=integer(cluster.get("apiPort", 6443), 1, 65535),
        appVIP=ipv4(app) if app else "",
        vrrpID=integer(cluster.get("vrrpID", 51), 1, 255),
        localRegistryHost=registry(cluster.get("localRegistryHost", "")),
    )
    if values["apiPort"] == 7000 or values["appVIP"] == values["apiVIP"]:
        invalid()
    try:
        pod = ipaddress.IPv4Network(text(cluster["podCIDR"]), strict=True)
        service = ipaddress.IPv4Network(text(cluster["serviceCIDR"]), strict=True)
        if pod.overlaps(service):
            invalid()
        values.update(podCIDR=str(pod), serviceCIDR=str(service))
        values["statsAllowedCIDR"] = management_cidr(cluster.get("statsAllowedCIDR", ""))
    except ValueError:
        invalid()
    user = text(ssh["user"], r"[a-z_][a-z0-9_-]{0,31}")
    if user == "root":
        invalid()
    wanted = dict(
        kubernetes=text(versions["kubernetes"], r"v[0-9]+\.[0-9]+\.[0-9]+"),
        cilium=text(versions["cilium"], r"[0-9]+\.[0-9]+\.[0-9]+"),
    )
    rows = data["nodes"]
    if not isinstance(rows, list) or not 2 <= len(rows) <= 1024:
        invalid()
    nodes = []
    for row in rows:
        mapping(row, NODE_FIELDS, {"name", "address", "interface"})
        node = dict(
            name=text(row["name"], r"(?:lb|cp|w)[1-9][0-9]*"),
            address=ipv4(row["address"]),
            interface=text(row["interface"], r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}"),
            dataDevice=device(row.get("dataDevice", "")),
            identityFingerprint=text(row.get("identityFingerprint", ""), r"(?:[0-9a-f]{64})?"),
        )
        if node["name"].startswith("lb") and node["dataDevice"] not in ("", "none"):
            invalid()
        nodes.append(node)
    names = {row["name"] for row in nodes}
    addresses = {row["address"] for row in nodes}
    if len(names) != len(nodes) or len(addresses) != len(nodes) or not {"lb1", "cp1"} <= names:
        invalid()
    if sum(n.startswith("lb") for n in names) > 250:
        invalid()
    vips = {values["apiVIP"]} | ({values["appVIP"]} if values["appVIP"] else set())
    if vips & addresses:
        invalid()
    if any(ipaddress.IPv4Address(v) in net for v in addresses | vips for net in (pod, service)):
        invalid()

    def order(node):
        match = re.fullmatch(r"(lb|cp|w)([0-9]+)", node["name"])
        return (("lb", "cp", "w").index(match[1]), int(match[2]))

    result = dict(
        schemaVersion=1,
        cluster=values,
        ssh=dict(user=user),
        versions=wanted,
        nodes=sorted(nodes, key=order),
    )
    # Optional additions preserve the canonical revision of existing v1 documents.
    if 'registry' in data:
        entry=mapping(data['registry'],{'authority','caFingerprint'},{'authority','caFingerprint'})
        result['registry']=dict(authority=registry(entry['authority']),
                               caFingerprint=text(entry['caFingerprint'],r'(?:[a-f0-9]{64})?'))
        if not result['registry']['authority']:
            invalid()
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            invalid()
        result[key] = value
    return result


def load_config(path):
    path = Path(path).expanduser().absolute()
    if (
        any(p.is_symlink() for p in (path, *path.parents))
        or not path.is_file()
        or path.stat().st_size > 1024 * 1024
    ):
        invalid()
    try:
        return normalize(json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique))
    except (OSError, ValueError, RecursionError):
        invalid()


def revision(config):
    return fingerprint(normalize(config))


def template():
    return normalize(
        dict(
            schemaVersion=1,
            cluster=dict(
                name="example-cluster",
                apiVIP="192.0.2.10",
                podCIDR="10.244.0.0/16",
                serviceCIDR="10.96.0.0/12",
            ),
            ssh=dict(user="ubuntu"),
            versions=dict(PINNED_VERSIONS),
            nodes=[
                dict(name="lb1", address="192.0.2.11", interface="enp6s18"),
                dict(name="cp1", address="192.0.2.12", interface="enp6s18"),
            ],
        )
    )


def deployment_rows(config):
    data = normalize(config)
    if 'registry' in data:
        raise OperationError('ops.config.operations_only')
    if data["versions"] != PINNED_VERSIONS:
        raise OperationError("ops.config.unsupported_versions")
    cluster = data["cluster"]
    result = [("SSH_USER", data["ssh"]["user"]), ("CLUSTER_NAME", cluster["name"])]
    for field, key in [
        ("PROFILE", "profile"),
        ("VIP", "apiVIP"),
        ("APP_INGRESS_VIP", "appVIP"),
        ("API_PORT", "apiPort"),
        ("VRRP_ID", "vrrpID"),
        ("POD_CIDR", "podCIDR"),
        ("SVC_CIDR", "serviceCIDR"),
        ("LB_STATS_ALLOWED_CIDR", "statsAllowedCIDR"),
        ("LOCAL_REGISTRY_HOST", "localRegistryHost"),
    ]:
        result.append((field, str(cluster[key])))
    result.extend(
        (
            "NODE",
            n["name"],
            n["address"],
            n["interface"],
            n["dataDevice"] or "-",
            n["identityFingerprint"] or "-",
        )
        for n in data["nodes"]
    )
    return tuple(result)
