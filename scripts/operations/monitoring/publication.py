# SPDX-License-Identifier: MIT
"""Compare-and-set publication of one owned rule; caller provides the cluster lock."""
import copy

from ..models import OperationError, fingerprint
from ..adapters.kubernetes import resource_name

PROMETHEUS = "prometheuses.monitoring.coreos.com"
RULE = "prometheusrules.monitoring.coreos.com"
OWNER = "atlas-kubekit.io/managed-by"
SPEC_HASH = "atlas-kubekit.io/spec-sha256"


def _metadata(value):
    try:
        meta = value["metadata"]
        uid, version = meta["uid"], meta["resourceVersion"]
        if not isinstance(uid, str) or not uid or not isinstance(version, str) or not version:
            raise ValueError()
        return meta
    except (KeyError, TypeError, ValueError):
        raise OperationError("ops.change.kubernetes") from None


def _compatible(prometheus, labels):
    try:
        spec = prometheus["spec"]
        selector = spec.get("ruleSelector")
        namespaces = spec.get("ruleNamespaceSelector")
        if not isinstance(selector, dict) or namespaces not in (None, {}):
            return False
        if set(selector) - {"matchLabels", "matchExpressions"} or selector.get("matchExpressions"):
            return False
        match = selector.get("matchLabels", {})
        return isinstance(match, dict) and all(labels.get(key) == value for key, value in match.items())
    except (KeyError, AttributeError, TypeError):
        return False


def _identity(value):
    if value is None:
        return None
    meta = _metadata(value)
    labels, annotations = meta.get("labels"), meta.get("annotations")
    if (not isinstance(value.get("spec"), dict) or not isinstance(labels, dict) or not isinstance(annotations, dict) or
            labels.get(OWNER) != "monitoring" or annotations.get(SPEC_HASH) != fingerprint(value.get("spec"))):
        raise OperationError("ops.change.foreign_resource")
    # Plans observe the owned object's identity and desired spec, not its
    # resourceVersion. Controllers may update status/validation annotations
    # between preview and apply. publish() still uses the latest resourceVersion
    # in its replace request, so the API server retains write-time CAS protection.
    return {"uid": meta["uid"], "specHash": fingerprint(value.get("spec"))}


def capture(client, manifest, prometheus_name):
    try:
        metadata = manifest["metadata"]
        namespace = resource_name(metadata["namespace"])
        name = resource_name(metadata["name"])
        resource_name(prometheus_name)
        labels = metadata["labels"]
        if (manifest.get("kind") != "PrometheusRule" or
                manifest.get("apiVersion") != "monitoring.coreos.com/v1" or
                labels.get(OWNER) != "monitoring" or not isinstance(manifest["spec"], dict)):
            raise ValueError()
    except (KeyError, TypeError, AttributeError, ValueError):
        raise OperationError("ops.change.invalid_parameters") from None
    prom = client.get(PROMETHEUS, prometheus_name, namespace)
    meta = _metadata(prom)
    if not _compatible(prom, labels):
        raise OperationError("ops.monitoring.selector")
    current = client.get(RULE, name, namespace, optional=True)
    if current is not None:
        existing_labels = current.get("metadata", {}).get("labels", {})
        if not isinstance(existing_labels, dict) or any(existing_labels.get(k) != v for k, v in labels.items()):
            raise OperationError("ops.change.foreign_resource")
    return {"prometheus": {"uid": meta["uid"], "specHash": fingerprint(prom["spec"])},
            "rule": _identity(current)}


def publish(client, manifest, prometheus_name, expected):
    """CAS publish; never claim the rule has been selected or evaluated."""
    if capture(client, manifest, prometheus_name) != expected:
        raise OperationError("ops.change.stale")
    metadata = manifest["metadata"]
    current = client.get(RULE, metadata["name"], metadata["namespace"], optional=True)
    if _identity(current) != expected["rule"]:
        raise OperationError("ops.change.stale")
    if current is not None and current.get("spec") == manifest["spec"]:
        after = capture(client, manifest, prometheus_name)
        if after != expected:
            raise OperationError("ops.change.stale")
        return {"state": "resource-written", "rule": after["rule"], "evaluation": "unverified"}
    applied = None
    for _ in range(3):
        if current is None:
            value = copy.deepcopy(manifest)
        else:
            value = copy.deepcopy(current)
            value["spec"] = copy.deepcopy(manifest["spec"])
            value["metadata"].setdefault("labels", {}).update(metadata["labels"])
        value["metadata"].setdefault("annotations", {})[SPEC_HASH] = fingerprint(value["spec"])
        applied = client.create(value) if current is None else client.replace(value)
        if applied is not None:
            break
        # A controller may update annotations/status after GET and before
        # replace. Retry only when UID and managed spec still match the plan;
        # each replace continues to use the latest API resourceVersion.
        if current is None:
            raise OperationError("ops.change.stale")
        current = client.get(RULE, metadata["name"], metadata["namespace"], optional=True)
        if _identity(current) != expected["rule"]:
            raise OperationError("ops.change.stale")
    if applied is None:
        raise OperationError("ops.change.stale")
    applied_meta = _metadata(applied)
    after = capture(client, manifest, prometheus_name)
    if (after["prometheus"] != expected["prometheus"] or after["rule"] is None or
            after["rule"]["uid"] != applied_meta["uid"] or
            after["rule"]["specHash"] != fingerprint(manifest["spec"])):
        raise OperationError("ops.change.stale")
    return {"state": "resource-written", "rule": after["rule"], "evaluation": "unverified"}
