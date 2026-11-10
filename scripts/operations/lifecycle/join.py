# SPDX-License-Identifier: MIT
"""Worker enrollment uses native prerequisites and ephemeral, stdin-only join secrets."""
import copy
import re

from .. import configuration_cluster, registry_trust
from ..configuration import normalize, revision
from ..environment import check_identity
from ..models import OperationError
from .rules import node_fact, ready


def from_arguments(args, configuration, environment):
    return dict(
        configuration=configuration,
        candidate=dict(
            name=args.node,
            address=args.node_address,
            interface=args.node_interface,
            dataDevice=args.data_device,
        ),
    )


def preview(values):
    return (("ops.preview.join", (values["candidate"]["name"], values["candidate"]["address"])),)


def parameters(value):
    if not isinstance(value, dict) or set(value) != {"configuration", "candidate"}:
        raise OperationError("ops.change.invalid_parameters")
    config = normalize(value["configuration"])
    candidate = value["candidate"]
    if not isinstance(candidate, dict) or not re.fullmatch(
        r"w[1-9][0-9]*", str(candidate.get("name", ""))
    ):
        raise OperationError("ops.lifecycle.join_state")
    updated = copy.deepcopy(config)
    updated["nodes"].append(candidate)
    updated = normalize(updated)
    node = next(n for n in updated["nodes"] if n["name"] == candidate["name"])
    if node["dataDevice"] not in ("none", "existing"):
        raise OperationError("ops.lifecycle.join_storage")
    return dict(configuration=config, candidate=node)


def expanded(values):
    config = copy.deepcopy(values["configuration"])
    config["nodes"].append(values["candidate"])
    return normalize(config)


def observe(value, environment):
    values = parameters(value)
    config, nodes = environment.verify(values["configuration"], additional=(values["candidate"],))
    current = configuration_cluster.read(environment.client)
    if any(not ready(n) for name, n in nodes.items() if name != values["candidate"]["name"]):
        raise OperationError("ops.lifecycle.not_ready")
    desired = expanded(values)
    candidate = values["candidate"]
    trust=registry_trust.installed(config,environment)
    registry_fact=registry_trust.probe(desired,environment,candidate['name'],trust)
    facts = environment.host(desired, candidate["name"]).call(
        "node-observe", dict(configuration=desired, node=candidate["name"])
    )
    check_identity(desired, candidate["name"], facts["identity"])
    desired["nodes"] = [
        (
            dict(node, identityFingerprint=facts["identity"]["identityFingerprint"])
            if node["name"] == candidate["name"]
            else node
        )
        for node in desired["nodes"]
    ]
    if current is not None and current not in (config, normalize(desired)):
        raise OperationError("ops.config.conflict")
    existing = nodes.get(candidate["name"])
    if facts["kubeletState"] != (existing is not None):
        raise OperationError("ops.lifecycle.join_state")
    identities = {}
    for node in config["nodes"]:
        identity = environment.host(config, node["name"]).call("security-probe")["identity"]
        check_identity(config, node["name"], identity)
        machine = identity.get("machineIdFingerprint")
        if not machine or machine == facts["identity"].get("machineIdFingerprint"):
            raise OperationError("ops.lifecycle.duplicate_identity")
        identities[node["name"]] = identity["identityFingerprint"]
    return dict(
        candidate=facts,
        existing=node_fact(existing) if existing else None,
        identities=identities,
        registry=registry_fact,
        sharedRevision=revision(current) if current else None,
    )


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    facts = observe(values, environment)
    config = values["configuration"]
    values["candidate"]["identityFingerprint"] = facts["candidate"]["identity"][
        "identityFingerprint"
    ]
    desired = expanded(values)
    name = values["candidate"]["name"]
    target = environment.host(desired, name)
    arguments = dict(configuration=desired, node=name)
    if facts["existing"] is None:
        checkpoint("prepare-" + name)
        target.call("node-prepare", arguments, run_id, timeout=2100)
        if config.get('registry') is not None:
            checkpoint('registry-'+name)
            registry_trust.apply(desired,environment,name,registry_trust.installed(config,environment),run_id)
        issuer = environment.host(config, "cp1")
        checkpoint("issue-join-credential")
        secret = issuer.call(
            "join-issue", {key: config["cluster"][key] for key in ("apiVIP", "apiPort")}, run_id
        )
        try:
            checkpoint("join-" + name)
            target.call("node-join", dict(arguments, **secret), run_id, timeout=660)
        finally:
            try:
                issuer.call("join-revoke", dict(id=secret["token"].split(".", 1)[0]), run_id)
            finally:
                secret.clear()
    checkpoint("verify-" + name)
    environment.client.call(
        ["wait", "--for=condition=Ready", "node/" + name, "--timeout=10m"],
        timeout=630,
        json_output=False,
    )
    environment.verify(desired)
    checkpoint("close-enrollment-" + name)
    target.call("node-finalize", arguments, run_id)
    values["candidate"]["identityFingerprint"] = facts["candidate"]["identity"][
        "identityFingerprint"
    ]
    desired = expanded(values)
    checkpoint("publish-configuration")
    configuration_cluster.publish(environment.client, desired, expected=facts["sharedRevision"])
    return dict(node=name, state="joined", configurationRevision=revision(desired))
