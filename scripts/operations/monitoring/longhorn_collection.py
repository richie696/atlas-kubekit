# SPDX-License-Identifier: MIT
"""Owned ServiceMonitor and narrowly scoped Longhorn ingress policy."""
import copy
import re

from ..adapters.kubernetes import resource_name
from ..models import OperationError, fingerprint
from . import publication
from .rules import _DNS_LABEL, _dns_name

MONITOR = "servicemonitors.monitoring.coreos.com"
POLICY = "networkpolicy"


def manifests(namespace, release, prometheus, longhorn_namespace):
    if (not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace) or
            not isinstance(longhorn_namespace, str) or not _DNS_LABEL.fullmatch(longhorn_namespace) or
            not isinstance(release, str) or len(release) > 53 or not _dns_name(release) or
            not isinstance(prometheus, str) or len(prometheus) > 63 or
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", prometheus)):
        raise ValueError("invalid monitoring target")
    labels = {"release": release, publication.OWNER: "monitoring"}
    monitor = {"apiVersion": "monitoring.coreos.com/v1", "kind": "ServiceMonitor",
        "metadata": {"name": "atlas-longhorn-metrics", "namespace": namespace, "labels": labels},
        "spec": {"namespaceSelector": {"matchNames": [longhorn_namespace]},
                 "selector": {"matchLabels": {"app": "longhorn-manager"}},
                 "endpoints": [{"port": "manager", "path": "/metrics"}]}}
    policy = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": "atlas-longhorn-prometheus", "namespace": longhorn_namespace,
                     "labels": {publication.OWNER: "monitoring"}},
        "spec": {"podSelector": {"matchLabels": {"app": "longhorn-manager"}}, "policyTypes": ["Ingress"],
                 "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
                                        "podSelector": {"matchLabels": {"prometheus": prometheus}}}],
                              "ports": [{"protocol": "TCP", "port": 9500}]}]}}
    return monitor, policy


def _selector(prom, labels):
    spec = prom.get("spec", {})
    selector = spec.get("serviceMonitorSelector")
    namespaces = spec.get("serviceMonitorNamespaceSelector")
    if (not isinstance(selector, dict) or namespaces not in (None, {}) or
            spec.get("ignoreNamespaceSelectors") not in (None, False)):
        return False
    if set(selector) - {"matchLabels", "matchExpressions"} or selector.get("matchExpressions"):
        return False
    match = selector.get("matchLabels", {})
    return isinstance(match, dict) and all(labels.get(key) == value for key, value in match.items())


def _object(client, kind, manifest):
    meta = manifest["metadata"]
    current = client.get(kind, meta["name"], meta["namespace"], optional=True)
    if current is not None:
        current_labels = current.get("metadata", {}).get("labels", {})
        if not isinstance(current_labels, dict) or any(current_labels.get(k) != v for k, v in meta["labels"].items()):
            raise OperationError("ops.change.foreign_resource")
    return publication._identity(current)


def _existing_ingress_isolation(client, namespace, managed_name):
    """Avoid newly isolating managers and breaking their pre-existing traffic."""
    policies = client.get(POLICY, namespace=namespace).get("items")
    if not isinstance(policies, list):
        return False
    for item in policies:
        if not isinstance(item, dict) or item.get("metadata", {}).get("name") == managed_name:
            continue
        spec = item.get("spec", {})
        if not isinstance(spec, dict):
            continue
        selector = spec.get("podSelector")
        if not isinstance(selector, dict) or set(selector) - {"matchLabels"}:
            continue
        labels = selector.get("matchLabels", {})
        types = spec.get("policyTypes", ["Ingress"])
        if (isinstance(labels, dict) and all({"app": "longhorn-manager"}.get(k) == v for k, v in labels.items())
                and isinstance(types, list) and "Ingress" in types):
            return True
    return False


def capture(client, monitor, policy, prometheus_name):
    m = monitor["metadata"]
    namespace, lh_namespace = resource_name(m["namespace"]), resource_name(policy["metadata"]["namespace"])
    if not _existing_ingress_isolation(client, lh_namespace, policy["metadata"]["name"]):
        raise OperationError("ops.monitoring.longhorn_isolation_required")
    prom = client.get(publication.PROMETHEUS, resource_name(prometheus_name), namespace)
    meta = publication._metadata(prom)
    if not _selector(prom, m["labels"]):
        raise OperationError("ops.monitoring.selector")
    services = client.get("service", namespace=lh_namespace).get("items", [])
    matches = [s for s in services if s.get("metadata", {}).get("labels", {}).get("app") == "longhorn-manager"]
    if len(matches) != 1 or matches[0].get("metadata", {}).get("name") != "longhorn-backend":
        raise OperationError("ops.monitoring.longhorn_service")
    service = matches[0]
    service_meta = publication._metadata(service)
    spec = service.get("spec", {})
    ports = [p for p in spec.get("ports", []) if p.get("name") == "manager"]
    if (spec.get("selector") != {"app": "longhorn-manager"} or len(ports) != 1 or
            ports[0].get("port") != 9500 or ports[0].get("protocol", "TCP") != "TCP" or
            ports[0].get("targetPort") not in (9500, "manager")):
        raise OperationError("ops.monitoring.longhorn_service")
    return {"prometheus": {"uid": meta["uid"], "specHash": fingerprint(prom["spec"])},
            "service": {"uid": service_meta["uid"], "specHash": fingerprint(spec)},
            "monitor": _object(client, MONITOR, monitor), "policy": _object(client, POLICY, policy)}


def _write(client, kind, manifest, expected):
    meta = manifest["metadata"]
    current = client.get(kind, meta["name"], meta["namespace"], optional=True)
    if _object(client, kind, manifest) != expected:
        raise OperationError("ops.change.stale")
    value = copy.deepcopy(manifest) if current is None else copy.deepcopy(current)
    value["spec"] = copy.deepcopy(manifest["spec"])
    value["metadata"].setdefault("labels", {}).update(meta["labels"])
    value["metadata"].setdefault("annotations", {})[publication.SPEC_HASH] = fingerprint(value["spec"])
    result = client.create(value) if current is None else client.replace(value)
    if result is None:
        raise OperationError("ops.change.stale")
    return publication._identity(result)


def publish(client, monitor, policy, prometheus_name, expected, checkpoint):
    if capture(client, monitor, policy, prometheus_name) != expected:
        raise OperationError("ops.change.stale")
    checkpoint("longhorn-network-policy")
    policy_after = _write(client, POLICY, policy, expected["policy"])
    halfway = capture(client, monitor, policy, prometheus_name)
    if halfway != {**expected, "policy": policy_after}:
        raise OperationError("ops.change.stale")
    checkpoint("longhorn-service-monitor")
    monitor_after = _write(client, MONITOR, monitor, expected["monitor"])
    after = capture(client, monitor, policy, prometheus_name)
    if after != {**expected, "policy": policy_after, "monitor": monitor_after}:
        raise OperationError("ops.change.stale")
    return {"state": "resources-written", "monitor": monitor_after, "policy": policy_after, "evaluation": "unverified"}
