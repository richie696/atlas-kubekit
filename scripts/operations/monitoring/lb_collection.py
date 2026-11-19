# SPDX-License-Identifier: MIT
"""Render bounded Prometheus Operator resources for registered HAProxy nodes."""
import copy
import ipaddress
import re

from ..adapters.kubernetes import resource_name
from ..models import OperationError, fingerprint
from . import publication


SERVICE = "service"
ENDPOINTS = "endpoints"
MONITOR = "servicemonitors.monitoring.coreos.com"
RESOURCE_KIND = {"Service": SERVICE, "Endpoints": ENDPOINTS, "ServiceMonitor": MONITOR}


def manifests(namespace, release, nodes):
    """Return a headless Service, static Endpoints and a scrape monitor.

    HAProxy's native Prometheus exporter is scraped directly on each registered
    LB's private address. The endpoint is expected to be allowlisted by the LB
    installer; this function never opens a firewall or changes a host.
    """
    if (not isinstance(namespace, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace)
            or not isinstance(release, str) or len(release) > 53
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", release)
            or not isinstance(nodes, (list, tuple)) or not 1 <= len(nodes) <= 250):
        raise ValueError("invalid load-balancer monitoring target")
    normalized = []
    seen_names, seen_addresses = set(), set()
    for item in nodes:
        if not isinstance(item, dict) or set(item) != {"name", "address"}:
            raise ValueError("invalid load-balancer monitoring target")
        name, address = item["name"], item["address"]
        try:
            parsed = ipaddress.ip_address(address)
        except (ValueError, TypeError):
            raise ValueError("invalid load-balancer monitoring address") from None
        if (parsed.version != 4 or parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast
                or not isinstance(name, str) or not re.fullmatch(r"lb[1-9][0-9]*", name)
                or name in seen_names or str(parsed) in seen_addresses):
            raise ValueError("invalid load-balancer monitoring target")
        seen_names.add(name)
        seen_addresses.add(str(parsed))
        normalized.append({"name": name, "address": str(parsed)})
    normalized.sort(key=lambda row: int(row["name"][2:]))

    service_name = "atlas-lb-metrics"
    app_label = "atlas-lb-metrics"
    labels = {"app.kubernetes.io/name": app_label, publication.OWNER: "monitoring"}
    service = {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": service_name, "namespace": namespace, "labels": labels},
        # State Kubernetes Service defaults explicitly so the ownership hash
        # remains stable after the API server stores the object.
        "spec": {"clusterIP": "None", "clusterIPs": ["None"],
                 "ipFamilies": ["IPv4"], "ipFamilyPolicy": "SingleStack",
                 "sessionAffinity": "None", "internalTrafficPolicy": "Cluster",
                 "type": "ClusterIP",
                 "ports": [{"name": "metrics", "port": 8405,
                            "protocol": "TCP", "targetPort": 8405}]},
    }
    endpoints = {
        "apiVersion": "v1", "kind": "Endpoints",
        "metadata": {"name": service_name, "namespace": namespace,
                     "labels": {**labels, publication.OWNER: "monitoring"}},
        "subsets": [{"addresses": [{"ip": row["address"]} for row in normalized],
                     "ports": [{"name": "metrics", "port": 8405, "protocol": "TCP"}] }],
    }
    relabelings = []
    for row in normalized:
        relabelings.append({"sourceLabels": ["__address__"],
                            "regex": re.escape(row["address"]) + r":8405",
                            "targetLabel": "lb", "replacement": row["name"], "action": "replace"})
    haproxy_relabelings = [*relabelings, {"targetLabel": "metrics_endpoint",
                                          "replacement": "haproxy", "action": "replace"}]
    vip_relabelings = [*relabelings, {"targetLabel": "metrics_endpoint",
                                      "replacement": "vip", "action": "replace"}]
    monitor = {
        "apiVersion": "monitoring.coreos.com/v1", "kind": "ServiceMonitor",
        "metadata": {"name": service_name, "namespace": namespace,
                     "labels": {"release": release, publication.OWNER: "monitoring"}},
        "spec": {"namespaceSelector": {"matchNames": [namespace]},
                 "selector": {"matchLabels": {"app.kubernetes.io/name": app_label}},
                 "jobLabel": "app.kubernetes.io/name",
                 "endpoints": [
                     {"port": "metrics", "path": "/metrics", "interval": "30s",
                      "relabelings": haproxy_relabelings},
                     {"port": "metrics", "path": "/vip-metrics", "interval": "15s",
                      "relabelings": vip_relabelings},
                 ]},
    }
    return service, endpoints, monitor


def _resource_identity(value, data_field):
    if value is None:
        return None
    try:
        meta = publication._metadata(value)
        labels = meta.get("labels", {})
        annotations = meta.get("annotations", {})
        payload = value[data_field]
        if (labels.get(publication.OWNER) != "monitoring" or not isinstance(payload, (dict, list))
                or not isinstance(annotations, dict)
                or annotations.get(publication.SPEC_HASH) != fingerprint(payload)):
            raise ValueError()
        return {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"],
                "specHash": fingerprint(payload)}
    except (KeyError, AttributeError, TypeError, ValueError):
        raise OperationError("ops.change.foreign_resource") from None


def _current(client, manifest):
    meta = manifest["metadata"]
    return client.get(RESOURCE_KIND[manifest["kind"]], meta["name"], meta["namespace"], optional=True)


def _owned(client, manifest):
    current = _current(client, manifest)
    if current is None:
        return None
    labels = current.get("metadata", {}).get("labels", {})
    wanted = manifest["metadata"]["labels"]
    if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in wanted.items()):
        raise OperationError("ops.change.foreign_resource")
    return current


def _selector(prometheus, labels):
    spec = prometheus.get("spec", {})
    selector = spec.get("serviceMonitorSelector")
    namespaces = spec.get("serviceMonitorNamespaceSelector")
    if (not isinstance(selector, dict) or namespaces not in (None, {})
            or spec.get("ignoreNamespaceSelectors") not in (None, False)
            or set(selector) - {"matchLabels", "matchExpressions"} or selector.get("matchExpressions")):
        return False
    match = selector.get("matchLabels", {})
    return isinstance(match, dict) and all(labels.get(key) == value for key, value in match.items())


def capture(client, service, endpoints, monitor, prometheus_name):
    namespace = resource_name(service["metadata"]["namespace"])
    prometheus_name = resource_name(prometheus_name)
    prom = client.get(publication.PROMETHEUS, prometheus_name, namespace)
    prom_meta = publication._metadata(prom)
    if not _selector(prom, monitor["metadata"]["labels"]):
        raise OperationError("ops.monitoring.selector")
    resources = {}
    for manifest, field in ((service, "spec"), (endpoints, "subsets"), (monitor, "spec")):
        current = _owned(client, manifest)
        resources[manifest["kind"].lower()] = _resource_identity(current, field)
    return {"prometheus": {"uid": prom_meta["uid"], "specHash": publication.fingerprint(prom["spec"])},
            "resources": resources}


def _write(client, manifest, field, expected):
    current = _owned(client, manifest)
    if _resource_identity(current, field) != expected:
        raise OperationError("ops.change.stale")
    if current is not None and current.get(field) == manifest[field]:
        return _resource_identity(current, field)
    value = copy.deepcopy(manifest if current is None else current)
    value[field] = manifest[field]
    value["metadata"].setdefault("labels", {}).update(manifest["metadata"]["labels"])
    value["metadata"].setdefault("annotations", {})[publication.SPEC_HASH] = fingerprint(value[field])
    result = client.create(value) if current is None else client.replace(value)
    if result is None:
        raise OperationError("ops.change.stale")
    return _resource_identity(result, field)


def publish(client, service, endpoints, monitor, prometheus_name, expected, checkpoint):
    if capture(client, service, endpoints, monitor, prometheus_name) != expected:
        raise OperationError("ops.change.stale")
    service_id = _write(client, service, "spec", expected["resources"]["service"])
    checkpoint("monitoring-lb-endpoints")
    endpoints_id = _write(client, endpoints, "subsets", expected["resources"]["endpoints"])
    checkpoint("monitoring-lb-service-monitor")
    monitor_id = _write(client, monitor, "spec", expected["resources"]["servicemonitor"])
    after = capture(client, service, endpoints, monitor, prometheus_name)
    wanted = {"service": service_id, "endpoints": endpoints_id, "servicemonitor": monitor_id}
    if after != {"prometheus": expected["prometheus"], "resources": wanted}:
        raise OperationError("ops.change.stale")
    return {"state": "resources-written", "resources": wanted, "scraping": "unverified"}
