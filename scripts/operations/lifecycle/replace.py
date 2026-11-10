# SPDX-License-Identifier: MIT
"""Replace a worker with a distinct prepared identity, preserving partial intent."""
from . import retire, join
from ..environment import check_identity
from ..models import OperationError


def from_arguments(args, configuration, environment):
    retirement = retire.from_arguments(args, configuration, environment)
    return dict(
        retirement,
        candidate=dict(
            name=args.replacement_name,
            address=args.node_address,
            interface=args.node_interface,
            dataDevice=args.data_device,
        ),
    )


def preview(values):
    return (
        (
            "ops.preview.replace",
            (values["node"], values["candidate"]["name"], values["candidate"]["address"]),
        ),
    )


def parameters(value):
    if not isinstance(value, dict) or set(value) != {
        "configuration",
        "node",
        "nodeUID",
        "deleteEmptyDir",
        "candidate",
    }:
        raise OperationError("ops.change.invalid_parameters")
    old = retire.parameters(
        {key: value[key] for key in ("configuration", "node", "nodeUID", "deleteEmptyDir")}
    )
    new = join.parameters(dict(configuration=old["configuration"], candidate=value["candidate"]))
    return dict(old, candidate=new["candidate"])


def old_values(values):
    return {key: values[key] for key in ("configuration", "node", "nodeUID", "deleteEmptyDir")}


def require_retired(values, environment):
    facts = environment.host(values["configuration"], values["node"]).call(
        "retirement-observe", dict(configuration=values["configuration"], node=values["node"])
    )
    check_identity(values["configuration"], values["node"], facts["identity"])
    marker = facts["retirement"]
    if (
        marker is None
        or marker["state"] != "retired"
        or marker["nodeUID"] != values["nodeUID"]
        or marker["clusterIdentity"] != environment.client.cluster_identity()
    ):
        raise OperationError("ops.lifecycle.retire_state")
    return facts


def observe(value, environment):
    values = parameters(value)
    actual = environment.client.get("node", values["node"], optional=True)
    if actual is not None:
        old = retire.observe(old_values(values), environment)
        config = values["configuration"]
    else:
        old = require_retired(values, environment)
        config = retire.reduced(old_values(values))
    new = join.observe(dict(configuration=config, candidate=values["candidate"]), environment)
    return dict(retirement=old, replacement=new, oldNodePresent=actual is not None)


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    facts = observe(values, environment)
    if facts["oldNodePresent"]:
        retire.apply(old_values(values), environment, checkpoint, run_id)
    else:
        require_retired(values, environment)
    result = join.apply(
        dict(configuration=retire.reduced(old_values(values)), candidate=values["candidate"]),
        environment,
        checkpoint,
        run_id,
    )
    return dict(retiredNode=values["node"], replacement=result, disksPreserved=True)
