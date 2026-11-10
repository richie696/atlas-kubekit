# SPDX-License-Identifier: MIT
"""A bounded configuration apply: update stats sources on registered LBs."""
import copy

from ..configuration import normalize, management_cidr, revision
from .. import configuration_cluster
from ..environment import check_identity
from ..models import OperationError


def from_arguments(args, configuration, environment):
    return dict(configuration=configuration, cidr=args.stats_cidr)


def preview(values):
    return (("ops.preview.stats", (values["cidr"] or "127.0.0.1/32",)),)


def parameters(value):
    if not isinstance(value, dict) or set(value) != {"configuration", "cidr"}:
        raise OperationError("ops.change.invalid_parameters")
    return dict(
        configuration=normalize(value["configuration"]), cidr=management_cidr(value["cidr"])
    )


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    result = {}
    saved = configuration_cluster.read(environment.client)
    desired = copy.deepcopy(config)
    desired["cluster"]["statsAllowedCIDR"] = values["cidr"]
    if saved is not None and saved not in (config, desired):
        raise OperationError("ops.config.conflict")
    for node in config["nodes"]:
        if not node["name"].startswith("lb"):
            continue
        facts = environment.host(config, node["name"]).call("stats-observe")
        check_identity(config, node["name"], facts["identity"])
        if not facts["active"]:
            raise OperationError("ops.host.inactive")
        result[node["name"]] = facts
    return dict(
        nodes=result,
        configurationRevision=revision(config),
        sharedRevision=revision(saved) if saved else None,
    )


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    config = values["configuration"]
    facts = observe(values, environment)
    result = {}
    # Preserve the active API ingress until a standby has accepted the candidate.
    order = sorted(
        facts["nodes"],
        key=lambda n: (config["cluster"]["apiVIP"] in facts["nodes"][n]["addresses"], n),
    )
    for name in order:
        checkpoint("stats-" + name)
        result[name] = environment.host(config, name).call(
            "stats-apply",
            dict(cidr=values["cidr"], expectedSha=facts["nodes"][name]["configurationSha"]),
            run_id,
        )
    updated = copy.deepcopy(config)
    updated["cluster"]["statsAllowedCIDR"] = values["cidr"]
    checkpoint("publish-configuration")
    configuration_cluster.publish(environment.client, updated, expected=facts["sharedRevision"])
    return dict(nodes=result, configurationRevision=revision(updated))
