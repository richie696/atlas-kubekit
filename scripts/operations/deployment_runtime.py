# SPDX-License-Identifier: MIT
"""Phase-two publication bridge. Input contains public fields, never shell source."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.lib.i18n import translate
from scripts.operations.adapters.kubernetes import Kubernetes
from scripts.operations import configuration_cluster, deployment_lock
from scripts.operations.configuration import normalize, PINNED_VERSIONS
from scripts.operations.models import OperationError
from scripts.operations.environment import Environment
from scripts.operations.host_actions.common import replace

DESTINATION = Path("/usr/local/lib/k8s-deploy")
FIELDS = {
    "CLUSTER_NAME",
    "SSH_USER",
    "PROFILE",
    "VIP",
    "APP_INGRESS_VIP",
    "API_PORT",
    "VRRP_ID",
    "POD_CIDR",
    "SVC_CIDR",
    "LB_STATS_ALLOWED_CIDR",
    "LOCAL_REGISTRY_HOST",
}


def read_rows(stream):
    raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise OperationError("ops.error.limit")
    values, nodes = {}, []
    for line in raw.splitlines():
        row = line.split("\t")
        if row[0] == "NODE" and len(row) == 6:
            _, name, address, interface, device, machine = row
            pin = hashlib.sha256((name + "\n" + machine).encode()).hexdigest() if machine else ""
            nodes.append(
                dict(
                    name=name,
                    address=address,
                    interface=interface,
                    dataDevice=device,
                    identityFingerprint=pin,
                )
            )
        elif len(row) == 2 and row[0] in FIELDS and row[0] not in values:
            values[row[0]] = row[1]
        else:
            raise OperationError("ops.config.invalid")
    if set(values) != FIELDS:
        raise OperationError("ops.config.invalid")
    cluster = dict(
        name=values["CLUSTER_NAME"],
        profile=values["PROFILE"],
        apiVIP=values["VIP"],
        apiPort=int(values["API_PORT"]),
        appVIP=values["APP_INGRESS_VIP"],
        vrrpID=int(values["VRRP_ID"]),
        podCIDR=values["POD_CIDR"],
        serviceCIDR=values["SVC_CIDR"],
        statsAllowedCIDR=values["LB_STATS_ALLOWED_CIDR"],
        localRegistryHost=values["LOCAL_REGISTRY_HOST"],
    )
    return normalize(
        dict(
            schemaVersion=1,
            cluster=cluster,
            ssh=dict(user=values["SSH_USER"]),
            versions=dict(PINNED_VERSIONS),
            nodes=nodes,
        )
    )


def without_pins(configuration):
    value = copy.deepcopy(normalize(configuration))
    for node in value["nodes"]:
        node["identityFingerprint"] = ""
        # LB nodes never receive a Kubernetes data disk. Older native plans
        # persisted only Kubernetes disk choices, so their empty LB marker is
        # equivalent to the explicit "none" accepted by the JSON configuration.
        if node["name"].startswith("lb"):
            node["dataDevice"] = ""
    return value


def guard(client, configuration):
    current = configuration_cluster.read(client)
    if current is not None and without_pins(current) != without_pins(configuration):
        raise OperationError("ops.config.conflict")


def install():
    if os.geteuid() != 0:
        raise OperationError("ops.error.root")
    # Own Python sources only; no credentials, configuration files or caches.
    for directory in ("scripts/operations", "scripts/lib"):
        for source in (
            path for path in (ROOT / directory).rglob("*") if path.suffix in (".py", ".sh") or path.name=='component-versions.json'
        ):
            if source.is_symlink():
                raise OperationError("ops.error.path")
            target = DESTINATION / source.relative_to(ROOT)
            if source.resolve() == target.resolve():
                continue
            if any(path.is_symlink() for path in (target, *target.parents)):
                raise OperationError("ops.error.path")
            target.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
            replace(target, source.read_bytes(), 0o644)
    for source in (ROOT / "locales").glob("*.json"):
        target = DESTINATION / "locales" / source.name
        if source.resolve() == target.resolve():
            continue
        if source.is_symlink() or any(path.is_symlink() for path in (target, *target.parents)):
            raise OperationError("ops.error.path")
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        replace(target, source.read_bytes(), 0o644)
    source = ROOT / "VERSION"
    target = DESTINATION / "VERSION"
    if source.is_file() and source.resolve() != target.resolve():
        if target.is_symlink():
            raise OperationError("ops.error.path")
        replace(target, source.read_bytes(), 0o644)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if args == ["install"]:
            install()
            return 0
        if len(args) != 3 or args[0] not in (
            "guard",
            "publish",
            "name",
            "managed",
            "lock-begin",
            "lock-fence",
            "lock-complete",
        ):
            raise OperationError("ops.config.arguments")
        config = read_rows(sys.stdin)
        target = None if args[1] == "local" else args[1]
        client = Kubernetes("/etc/kubernetes/admin.conf", target, args[2] if target else None)
        if client.endpoint() != "https://" + config["cluster"]["apiVIP"] + ":" + str(
            config["cluster"]["apiPort"]
        ):
            raise OperationError("ops.change.wrong_cluster")
        if args[0] == "name":
            current_config = configuration_cluster.read(client)
            print((current_config or config)["cluster"]["name"])
            return 0
        if args[0] == "managed":
            print("yes" if configuration_cluster.read(client) is not None else "no")
            return 0
        if args[0].startswith("lock-"):
            if args[0] == "lock-begin":
                deployment_lock.begin(client, config)
            elif args[0] == "lock-fence":
                deployment_lock.fence(client)
            else:
                deployment_lock.complete(client)
            return 0
        if args[0] == "guard":
            deployment_lock.require_idle(client)
        guard(client, config)
        if args[0] == "publish":
            deployment_lock.owned(client)
            Environment(client).verify(config)
            configuration_cluster.publish(client, config)
        return 0
    except (OperationError, ValueError, OSError, KeyError) as error:
        key = error.key if isinstance(error, OperationError) else "ops.config.invalid"
        print(translate(os.environ.get("ATLAS_LANG", "en"), key), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
