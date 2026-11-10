# SPDX-License-Identifier: MIT
"""Public Cilium rollout projections; private Helm values never leave the host."""
import copy
import json
import re

from ..models import OperationError

OWNER = "atlas-kubekit.io/component-plan"


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise OperationError("ops.component.version")
    return tuple(int(part) for part in value.split("."))


def images(spec):
    return dict(
        sorted(
            (kind + ":" + row["name"], row["image"])
            for kind, field in (("container", "containers"), ("init", "initContainers"))
            for row in spec.get(field, [])
        )
    )


def strategy(value):
    if (
        not isinstance(value, dict)
        or set(value) - {"type", "rollingUpdate"}
        or value.get("type") not in ("OnDelete", "RollingUpdate")
    ):
        raise OperationError("ops.component.policy")
    rolling = value.get("rollingUpdate")
    if rolling is not None:
        if not isinstance(rolling, dict) or set(rolling) - {"maxUnavailable", "maxSurge"}:
            raise OperationError("ops.component.policy")
        for item in rolling.values():
            if not (
                type(item) is int
                and item >= 0
                or isinstance(item, str)
                and re.fullmatch(r"[0-9]+%?", item)
            ):
                raise OperationError("ops.component.policy")
    return copy.deepcopy(value)


def rollout_values(existing, owner):
    if not isinstance(existing, dict) or not re.fullmatch(r"[a-f0-9]{64}", owner):
        raise OperationError("ops.component.policy")
    value = copy.deepcopy(existing)
    value["updateStrategy"] = dict(type="OnDelete", rollingUpdate=None)
    for key in ("envoy", "operator"):
        if not isinstance(value.setdefault(key, {}), dict):
            raise OperationError("ops.component.policy")
    value["envoy"]["updateStrategy"] = dict(type="OnDelete", rollingUpdate=None)
    value["operator"]["updateStrategy"] = dict(
        type="RollingUpdate", rollingUpdate=dict(maxUnavailable=0, maxSurge=1)
    )
    for section in (value, value["envoy"]):
        if not isinstance(section.setdefault("podAnnotations", {}), dict):
            raise OperationError("ops.component.policy")
        section["podAnnotations"][OWNER] = owner
    return value


def documents(raw):
    decoder = json.JSONDecoder()
    result = []
    text = raw.strip()
    while text:
        value, index = decoder.raw_decode(text)
        if not isinstance(value, dict):
            raise OperationError("ops.component.policy")
        if value.get("kind") == "List":
            result.extend(value["items"])
        else:
            result.append(value)
        if len(result) > 512:
            raise OperationError("ops.error.limit")
        text = text[index:].lstrip()
    return result


def rendered(rows, target):
    daemons = {}
    operator = None
    for row in rows:
        kind, name = row.get("kind"), row.get("metadata", {}).get("name")
        if kind == "DaemonSet" and name in ("cilium", "cilium-envoy"):
            spec = row["spec"]
            if spec["updateStrategy"]["type"] != "OnDelete":
                raise OperationError("ops.component.policy")
            if not re.fullmatch(
                r"[a-f0-9]{64}",
                spec["template"].get("metadata", {}).get("annotations", {}).get(OWNER, ""),
            ):
                raise OperationError("ops.component.policy")
            daemons[name] = images(spec["template"]["spec"])
        elif kind == "Deployment" and name == "cilium-operator":
            rolling = row["spec"].get("strategy", {}).get("rollingUpdate", {})
            if rolling.get("maxUnavailable") != 0 or rolling.get("maxSurge") != 1:
                raise OperationError("ops.component.policy")
            operator = images(row["spec"]["template"]["spec"])
    if "cilium" not in daemons or operator is None:
        raise OperationError("ops.component.policy")
    for reference in (
        daemons["cilium"].get("container:cilium-agent", ""),
        operator.get("container:cilium-operator", ""),
    ):
        if not re.search(r":v?" + re.escape(target) + r"(?:@sha256:[a-f0-9]{64})?$", reference):
            raise OperationError("ops.component.image_override")
    return dict(daemons=daemons, operator=operator)


def completed_values(existing, policies, operator):
    """Restore policy in Helm's values as well as in the Kubernetes resources."""
    value = copy.deepcopy(existing)
    value["updateStrategy"] = strategy(policies["cilium"]["strategy"])
    if "cilium-envoy" in policies:
        value.setdefault("envoy", {})["updateStrategy"] = strategy(
            policies["cilium-envoy"]["strategy"]
        )
    value.setdefault("operator", {})["updateStrategy"] = strategy(operator["strategy"])
    return value


def policy_only(before, after):
    """Reject any final Helm change except the captured update strategies.

    Compare private manifests, not public projections: a random Secret, changed
    ConfigMap or Pod template must not sneak into a policy restoration.
    """

    def canonical(rows):
        result = {}
        for original in rows:
            row = copy.deepcopy(original)
            metadata = row.get("metadata", {})
            key = (
                row.get("apiVersion"),
                row.get("kind"),
                metadata.get("namespace"),
                metadata.get("name"),
            )
            if key in result:
                raise OperationError("ops.component.policy")
            if key[1] == "DaemonSet" and key[3] in ("cilium", "cilium-envoy"):
                row.get("spec", {}).pop("updateStrategy", None)
            if key[1] == "Deployment" and key[3] == "cilium-operator":
                row.get("spec", {}).pop("strategy", None)
            result[key] = row
        return result

    if canonical(before) != canonical(after):
        raise OperationError("ops.component.policy")
