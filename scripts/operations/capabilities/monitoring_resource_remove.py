# SPDX-License-Identifier: MIT
"""Safely remove one Atlas-owned monitoring object through a confirmed change plan."""
import copy
import re

from ..adapters.kubernetes import resource_name
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring.publication import OWNER, SPEC_HASH


# Deliberately exclude cluster-scoped objects, Secrets, PVCs and workload data.
RESOURCE_KINDS = {
    "prometheusrule": ("prometheusrules.monitoring.coreos.com", "PrometheusRule"),
    "servicemonitor": ("servicemonitors.monitoring.coreos.com", "ServiceMonitor"),
    "podmonitor": ("podmonitors.monitoring.coreos.com", "PodMonitor"),
    "alertmanagerconfig": ("alertmanagerconfigs.monitoring.coreos.com", "AlertmanagerConfig"),
    "networkpolicy": ("networkpolicy", "NetworkPolicy"),
}


def _identity(client, kind, namespace, name):
    resource, expected_kind = RESOURCE_KINDS[kind]
    current = client.get(resource, name, namespace, optional=True)
    if current is None:
        raise OperationError("ops.change.stale")
    try:
        metadata = current["metadata"]
        spec = current["spec"]
        labels = metadata["labels"]
        annotations = metadata["annotations"]
        uid, resource_version = metadata["uid"], metadata["resourceVersion"]
        if (current.get("kind") != expected_kind or
                metadata.get("namespace") != namespace or metadata.get("name") != name or
                metadata.get("deletionTimestamp") or not isinstance(spec, dict) or
                labels.get(OWNER) != "monitoring" or
                annotations.get(SPEC_HASH) != fingerprint(spec) or
                not isinstance(uid, str) or not uid or
                not isinstance(resource_version, str) or not resource_version):
            raise OperationError("ops.change.foreign_resource")
        return {"uid": uid, "resourceVersion": resource_version, "specHash": fingerprint(spec)}
    except (KeyError, TypeError, AttributeError):
        raise OperationError("ops.change.foreign_resource") from None


def parameters(value):
    required = {"configuration", "kind", "namespace", "name", "expected"}
    if not isinstance(value, dict) or set(value) != required:
        raise OperationError("ops.change.invalid_parameters")
    result = copy.deepcopy(value)
    result["configuration"] = normalize(value["configuration"])
    if result["kind"] not in RESOURCE_KINDS:
        raise OperationError("ops.change.invalid_parameters")
    result["namespace"] = resource_name(value["namespace"])
    result["name"] = resource_name(value["name"])
    expected = value["expected"]
    if (not isinstance(expected, dict) or set(expected) != {"uid", "resourceVersion", "specHash"} or
            not isinstance(expected["uid"], str) or
            not re.fullmatch(r"[A-Za-z0-9-]{1,128}", expected["uid"]) or
            not isinstance(expected["resourceVersion"], str) or
            not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", expected["resourceVersion"]) or
            not isinstance(expected["specHash"], str) or
            not re.fullmatch(r"[a-f0-9]{64}", expected["specHash"])):
        raise OperationError("ops.change.invalid_parameters")
    result["expected"] = copy.deepcopy(expected)
    return result


def from_arguments(args, configuration, environment):
    config, _ = environment.verify(configuration)
    values = {"configuration": config, "kind": args.monitoring_resource_kind,
              "namespace": args.monitoring_resource_namespace,
              "name": args.monitoring_resource_name}
    if values["kind"] not in RESOURCE_KINDS:
        raise OperationError("ops.change.invalid_parameters")
    values["namespace"] = resource_name(values["namespace"])
    values["name"] = resource_name(values["name"])
    values["expected"] = _identity(environment.client, values["kind"], values["namespace"], values["name"])
    return parameters(values)


def preview(values):
    values = parameters(values)
    return (("ops.monitoring.resource_remove_preview",
             (values["kind"], values["namespace"], values["name"], values["expected"]["uid"])),
            ("ops.monitoring.resource_remove_warning", ()))


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values["configuration"])
    observed = _identity(environment.client, values["kind"], values["namespace"], values["name"])
    if observed != values["expected"]:
        raise OperationError("ops.change.stale")
    return {"configurationRevision": revision(config), "target": observed,
            "kind": values["kind"], "namespace": values["namespace"], "name": values["name"]}


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values["configuration"])
    checkpoint("monitoring-resource-remove")
    environment.client.delete_monitoring_resource(
        values["kind"], values["namespace"], values["name"],
        values["expected"]["uid"], values["expected"]["resourceVersion"])
    resource = RESOURCE_KINDS[values["kind"]][0]
    remaining = environment.client.get(resource, values["name"], values["namespace"], optional=True)
    if remaining is not None:
        raise OperationError("ops.change.stale")
    return {"state": "resource-removed", "kind": values["kind"],
            "namespace": values["namespace"], "name": values["name"],
            "uid": values["expected"]["uid"]}
