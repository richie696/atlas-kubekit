# SPDX-License-Identifier: MIT
"""Cluster-wide privilege handoff; preserved management access is proven first."""
import copy
import re

from ..adapters.host import Host
from ..configuration import normalize, revision
from .. import configuration_cluster
from ..environment import check_identity
from ..models import OperationError


def from_arguments(args, configuration, environment):
    return dict(configuration=configuration, retainedUser=args.retained_user)


def preview(values):
    return (
        (
            "ops.preview.security",
            (
                values["configuration"]["ssh"]["user"],
                values["retainedUser"],
                ", ".join(node["name"] for node in values["configuration"]["nodes"]),
            ),
        ),
    )


def parameters(value):
    if not isinstance(value, dict) or set(value) != {"configuration", "retainedUser"}:
        raise OperationError("ops.change.invalid_parameters")
    config = normalize(value["configuration"])
    user = value["retainedUser"]
    if (
        not isinstance(user, str)
        or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user)
        or user in ("root", config["ssh"]["user"])
    ):
        raise OperationError("ops.security.separate_identity")
    return dict(configuration=config, retainedUser=user)


def retained_host(environment, config, node, user):
    return Host(user + "@" + node["address"], environment.identity, environment.ssh_config)


def probe(environment, config, node, user):
    value = retained_host(environment, config, node, user).call("security-probe", timeout=25)
    check_identity(config, node["name"], value["identity"])
    return value.get("uid") == 0 and value.get("sudoUser") == user


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    retained = values["retainedUser"]
    if (
        environment.client.control_target
        and environment.client.control_target.split("@", 1)[0] != retained
    ):
        raise OperationError("ops.security.control_identity")
    saved = configuration_cluster.read(environment.client)
    desired = copy.deepcopy(config)
    desired["ssh"]["user"] = retained
    if saved is not None and saved not in (config, desired):
        raise OperationError("ops.config.conflict")
    result = {}
    for node in config["nodes"]:
        if not probe(environment, config, node, retained):
            raise OperationError("ops.security.probe_failed")
        facts = retained_host(environment, config, node, retained).call(
            "security-observe", dict(deploymentUser=config["ssh"]["user"], retainedUser=retained)
        )
        check_identity(config, node["name"], facts["identity"])
        result[node["name"]] = facts
    return dict(nodes=result, sharedRevision=revision(saved) if saved else None)


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    config = values["configuration"]
    retained = values["retainedUser"]
    facts = observe(values, environment)
    results = {}
    for node in config["nodes"]:
        checkpoint("handoff-" + node["name"])
        host = retained_host(environment, config, node, retained)
        params = dict(
            deploymentUser=config["ssh"]["user"],
            retainedUser=retained,
            expectedSha=facts["nodes"][node["name"]]["bootstrapSha"],
        )
        results[node["name"]] = host.confirmed(
            "security-finalize",
            params,
            run_id,
            lambda n=node: probe(environment, config, n, retained),
        )
    updated = copy.deepcopy(config)
    updated["ssh"]["user"] = retained
    checkpoint("publish-configuration")
    configuration_cluster.publish(environment.client, updated, expected=facts["sharedRevision"])
    return dict(nodes=results, operationsUser=retained, configurationRevision=revision(updated))
