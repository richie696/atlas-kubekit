# SPDX-License-Identifier: MIT
"""Render and safely publish in-cluster network probes from each node."""
import copy
import ipaddress
import re
from urllib.parse import urlsplit

from ..models import OperationError, fingerprint
from . import publication
from .rules import _DNS_LABEL

MONITOR = "servicemonitors.monitoring.coreos.com"
RULES = publication.RULE
POLICY = "networkpolicy"
OWNER = publication.OWNER
SCRAPE_INTERVAL_SECONDS = 30


def parse_targets(values):
    """Parse a bounded name=HTTP-URL list; only in-cluster destinations are accepted."""
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise ValueError("invalid targets")
    parsed, names = [], set()
    for item in values:
        if not isinstance(item, str) or len(item) > 512 or item.count("=") < 1:
            raise ValueError("invalid target")
        name, raw_url = item.split("=", 1)
        if not _DNS_LABEL.fullmatch(name) or name in names:
            raise ValueError("invalid target name")
        names.add(name)
        url = urlsplit(raw_url)
        try:
            port = url.port or 80
        except ValueError:
            raise ValueError("invalid target port") from None
        if (url.scheme != "http" or not url.hostname or url.username is not None or
                url.password is not None or url.query or url.fragment or
                any(ord(char) <= 32 for char in raw_url) or not 1 <= port <= 65535):
            raise ValueError("only plain in-cluster HTTP targets are supported")
        host = url.hostname.rstrip(".").lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and address.version != 4:
            raise ValueError("only IPv4 cluster targets are supported")
        parsed.append({"name": name, "url": raw_url, "host": host, "port": port,
                       "path": url.path or "/", "address": str(address) if address else None})
    return tuple(parsed)


def _ready_pod(pod):
    status = pod.get("status", {})
    conditions = status.get("conditions", [])
    return (status.get("phase") == "Running" and not pod.get("metadata", {}).get("deletionTimestamp") and
            any(item.get("type") == "Ready" and item.get("status") == "True" for item in conditions))


def _matches(labels, selector):
    return isinstance(labels, dict) and isinstance(selector, dict) and bool(selector) and all(
        labels.get(key) == value for key, value in selector.items())


def _named_target_port(pods, target_port):
    if type(target_port) is int:
        return target_port
    found = {item.get("containerPort") for pod in pods
             for container in pod.get("spec", {}).get("containers", [])
             for item in container.get("ports", [])
             if item.get("name") == target_port and type(item.get("containerPort")) is int}
    if len(found) != 1:
        raise ValueError("service target port is unresolved")
    return found.pop()


def _bind_target(target, services, pods):
    """Resolve each URL against a current Service or ready Pod and derive a narrow policy peer."""
    service = None
    if target["address"]:
        candidates = [item for item in services if target["address"] in item.get("spec", {}).get("clusterIPs", []) or
                      target["address"] == item.get("spec", {}).get("clusterIP")]
        if len(candidates) == 1:
            service = candidates[0]
        else:
            candidates = [item for item in pods if item.get("status", {}).get("podIP") == target["address"]]
            if len(candidates) != 1 or not _ready_pod(candidates[0]):
                raise ValueError("IP target is not a ready in-cluster Service or Pod")
            pod = candidates[0]
            labels = pod.get("metadata", {}).get("labels", {})
            if not labels:
                raise ValueError("Pod target has no labels for safe egress")
            return {**target, "namespace": pod["metadata"]["namespace"],
                    "selector": copy.deepcopy(labels), "targetPort": target["port"], "kind": "pod"}, \
                   (pod["metadata"]["namespace"], tuple(sorted(labels.items())), target["port"])
    else:
        labels = target["host"].split(".")
        if len(labels) < 4 or labels[2] != "svc" or not all(_DNS_LABEL.fullmatch(part) for part in labels):
            raise ValueError("DNS target must name an in-cluster service")
        candidates = [item for item in services
                      if item.get("metadata", {}).get("namespace") == labels[1] and
                      item.get("metadata", {}).get("name") == labels[0]]
        if len(candidates) != 1:
            raise ValueError("DNS target Service was not found")
        service = candidates[0]
    spec = service.get("spec", {})
    selector = spec.get("selector", {})
    namespace = service.get("metadata", {}).get("namespace")
    if (not namespace or not selector or spec.get("type") == "ExternalName" or
            spec.get("clusterIP") == "None"):
        raise ValueError("target Service must select in-cluster Pods")
    ready = [pod for pod in pods if pod.get("metadata", {}).get("namespace") == namespace and
             _matches(pod.get("metadata", {}).get("labels", {}), selector) and _ready_pod(pod)]
    if not ready:
        raise ValueError("target Service has no ready Pods")
    ports = [item for item in spec.get("ports", []) if item.get("port") == target["port"]]
    if len(ports) != 1:
        raise ValueError("URL port must match one Service port")
    backend_port = _named_target_port(ready, ports[0].get("targetPort", ports[0]["port"]))
    return {**target, "namespace": namespace, "selector": copy.deepcopy(selector),
            "targetPort": backend_port, "kind": "service"}, \
           (namespace, tuple(sorted(selector.items())), backend_port)


def _selector_matches(selector, labels):
    if not isinstance(selector, dict) or set(selector) - {"matchLabels", "matchExpressions"}:
        return True
    if not _matches(labels, selector.get("matchLabels", {})) and selector.get("matchLabels"):
        return False
    for expression in selector.get("matchExpressions", []):
        key, operator, values = expression.get("key"), expression.get("operator"), expression.get("values", [])
        present = key in labels
        if operator == "In" and (not present or labels[key] not in values):
            return False
        if operator == "NotIn" and present and labels[key] in values:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
        if operator not in ("In", "NotIn", "Exists", "DoesNotExist"):
            return True
    return True


def _prometheus_selects(prometheus, selector_name, namespace_selector_name, labels):
    spec = prometheus.get("spec", {})
    selector = spec.get(selector_name)
    namespaces = spec.get(namespace_selector_name)
    if (not isinstance(selector, dict) or namespaces not in (None, {}) or
            spec.get("ignoreNamespaceSelectors") not in (None, False) or
            set(selector) - {"matchLabels", "matchExpressions"} or selector.get("matchExpressions")):
        return False
    required = selector.get("matchLabels", {})
    return isinstance(required, dict) and all(labels.get(key) == value for key, value in required.items())


def _scrape_endpoint(probe=None):
    endpoint = {"port": "http", "path": "/probe" if probe else "/metrics",
                "interval": f"{SCRAPE_INTERVAL_SECONDS}s",
                "scrapeTimeout": "10s", "relabelings": [
                    {"action": "replace", "sourceLabels": ["__meta_kubernetes_endpoint_node_name"],
                     "targetLabel": "source_node"},
                    {"action": "replace", "targetLabel": "metrics_endpoint",
                     "replacement": "blackbox-probe" if probe else "blackbox-exporter"},
                ]}
    if probe:
        endpoint["params"] = {"module": ["http_2xx"], "target": [probe["url"]]}
        endpoint["relabelings"].append({"action": "replace", "targetLabel": "probe_target",
                                       "replacement": probe["name"]})
    return endpoint


def _fresh_zero(selector):
    age = f"time() - timestamp({selector})"
    return f"({selector} == 0) and ({age} <= {3 * SCRAPE_INTERVAL_SECONDS}) and ({age} >= -10)"


def manifests(namespace, release, prometheus_name, exporter_release, raw_targets,
              services, pods, notification):
    if (not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace) or
            not isinstance(release, str) or not _DNS_LABEL.fullmatch(release) or
            not isinstance(prometheus_name, str) or not _DNS_LABEL.fullmatch(prometheus_name) or
            not isinstance(exporter_release, str) or not _DNS_LABEL.fullmatch(exporter_release) or
            not isinstance(notification, dict) or set(notification) != {"failedSummary", "failedDescription", "scrapeSummary"}):
        raise ValueError("invalid monitoring configuration")
    targets = parse_targets(raw_targets)
    bound, peers = [], set()
    for target in targets:
        resolved, peer = _bind_target(target, services, pods)
        bound.append(resolved)
        peers.add(peer)

    labels = {"release": release, OWNER: "monitoring"}
    exporter_labels = {"app.kubernetes.io/name": "prometheus-blackbox-exporter",
                       "app.kubernetes.io/instance": exporter_release}
    monitor = {"apiVersion": "monitoring.coreos.com/v1", "kind": "ServiceMonitor",
               "metadata": {"name": "atlas-network-probes", "namespace": namespace, "labels": labels},
               "spec": {"namespaceSelector": {"matchNames": [namespace]},
                        "selector": {"matchLabels": exporter_labels},
                        "jobLabel": "app.kubernetes.io/instance",
                        "endpoints": [_scrape_endpoint(), *[_scrape_endpoint(target) for target in bound]]}}
    dns = next((item for item in services if item.get("metadata", {}).get("namespace") == "kube-system" and
                item.get("metadata", {}).get("name") == "kube-dns"), None)
    dns_selector = dns.get("spec", {}).get("selector", {}) if dns else {}
    if not dns_selector:
        raise ValueError("kube-system/kube-dns Service has no Pod selector")
    egress = [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                       "podSelector": {"matchLabels": dns_selector}}],
               "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]
    for target_namespace, selector, port in sorted(peers):
        egress.append({"to": [{"namespaceSelector": {"matchLabels": {
                                    "kubernetes.io/metadata.name": target_namespace}},
                               "podSelector": {"matchLabels": dict(selector)}}],
                       "ports": [{"protocol": "TCP", "port": port}]})
    policy = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
              "metadata": {"name": "atlas-network-probes", "namespace": namespace,
                           "labels": {OWNER: "monitoring"}},
              "spec": {"podSelector": {"matchLabels": exporter_labels},
                       "policyTypes": ["Egress"], "egress": egress}}
    probe_metric = f'probe_success{{job="{exporter_release}",metrics_endpoint="blackbox-probe"}}'
    scrape_metric = f'up{{job="{exporter_release}",metrics_endpoint=~"blackbox-probe|blackbox-exporter"}}'
    rule = {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": "atlas-network-probes", "namespace": namespace,
                         "labels": labels},
            "spec": {"groups": [{"name": "atlas.network.probes", "rules": [
                {"alert": "AtlasNetworkProbeFailed",
                 "expr": _fresh_zero(probe_metric),
                 "for": "1m", "labels": {"risk_id": "R-NET-03", "severity": "warning"},
                 "annotations": {"summary": notification["failedSummary"],
                                 "description": notification["failedDescription"]}},
                {"alert": "AtlasNetworkProbeScrapeMissing",
                 "expr": _fresh_zero(scrape_metric),
                 "for": "5m", "labels": {"risk_id": "R-OBS-01", "severity": "warning"},
                 "annotations": {"summary": notification["scrapeSummary"],
                                 "description": notification["failedDescription"]}},
            ]}]}}
    return monitor, policy, rule, tuple(bound)


def _identity(client, kind, manifest):
    meta = manifest["metadata"]
    current = client.get(kind, meta["name"], meta["namespace"], optional=True)
    if current is not None:
        labels = current.get("metadata", {}).get("labels", {})
        if labels.get(OWNER) != "monitoring":
            raise OperationError("ops.change.foreign_resource")
    return publication._identity(current)


def capture(client, namespace, release, prometheus_name, exporter_release, manifests_):
    monitor, policy, rule, bound_targets = manifests_
    labels = monitor["metadata"]["labels"]
    prom = client.get(publication.PROMETHEUS, prometheus_name, namespace)
    prom_meta = publication._metadata(prom)
    if not _prometheus_selects(prom, "serviceMonitorSelector", "serviceMonitorNamespaceSelector", labels):
        raise OperationError("ops.monitoring.selector")
    rule_prom = client.get(publication.PROMETHEUS, prometheus_name, namespace)
    if not publication._compatible(rule_prom, rule["metadata"]["labels"]):
        raise OperationError("ops.monitoring.selector")
    exporter_selector = {"app.kubernetes.io/name": "prometheus-blackbox-exporter",
                         "app.kubernetes.io/instance": exporter_release}
    services = client.get("service", namespace=namespace).get("items", [])
    matching_services = [item for item in services if all(
        item.get("metadata", {}).get("labels", {}).get(key) == value
        for key, value in exporter_selector.items())]
    if len(matching_services) != 1:
        raise OperationError("ops.monitoring.network_exporter")
    exporter_service = matching_services[0]
    ports = [item for item in exporter_service.get("spec", {}).get("ports", [])
             if item.get("name") == "http" and item.get("port") == 9115]
    if len(ports) != 1 or exporter_service.get("spec", {}).get("selector") != exporter_selector:
        raise OperationError("ops.monitoring.network_exporter")
    daemons = client.get("daemonset", namespace=namespace).get("items", [])
    matching_daemons = [item for item in daemons if all(
        item.get("metadata", {}).get("labels", {}).get(key) == value
        for key, value in exporter_selector.items())]
    if len(matching_daemons) != 1:
        raise OperationError("ops.monitoring.network_exporter")
    daemon = matching_daemons[0]
    desired = daemon.get("status", {}).get("desiredNumberScheduled", 0)
    if desired < 1 or daemon.get("status", {}).get("numberReady", 0) != desired:
        raise OperationError("ops.monitoring.network_exporter")
    all_pods = client.get("pod").get("items", [])
    dns = next((item for item in client.get("service", namespace="kube-system").get("items", [])
                if item.get("metadata", {}).get("name") == "kube-dns"), None)
    if not dns or not dns.get("spec", {}).get("selector"):
        raise OperationError("ops.monitoring.network_target")
    for existing in client.get(POLICY, namespace=namespace).get("items", []):
        meta = existing.get("metadata", {})
        spec = existing.get("spec", {})
        types = spec.get("policyTypes", ["Ingress"] + (["Egress"] if "egress" in spec else []))
        if meta.get("name") == policy["metadata"]["name"] or "Egress" not in types:
            continue
        if _selector_matches(spec.get("podSelector", {}), exporter_selector):
            raise OperationError("ops.monitoring.network_policy_conflict")
    targets_now = tuple(_bind_target(item, client.get("service").get("items", []), all_pods)[0]
                        for item in parse_targets([target["name"] + "=" + target["url"]
                                                   for target in bound_targets]))
    return {"prometheus": {"uid": prom_meta["uid"], "specHash": fingerprint(prom["spec"])},
            "service": {"uid": exporter_service["metadata"]["uid"],
                        "specHash": fingerprint(exporter_service["spec"])},
            "daemonset": {"uid": daemon["metadata"]["uid"],
                          "specHash": fingerprint(daemon["spec"])},
            "targets": fingerprint(targets_now),
            "monitor": _identity(client, MONITOR, monitor),
            "policy": _identity(client, POLICY, policy),
            "rule": _identity(client, RULES, rule)}


def _write(client, kind, manifest, expected, step, checkpoint):
    meta = manifest["metadata"]
    current = client.get(kind, meta["name"], meta["namespace"], optional=True)
    identity = publication._identity(current)
    wanted = fingerprint(manifest["spec"])
    if identity != expected and not (identity and identity["specHash"] == wanted):
        raise OperationError("ops.change.stale")
    value = copy.deepcopy(manifest if current is None else current)
    if current is not None:
        value["spec"] = copy.deepcopy(manifest["spec"])
        value["metadata"].setdefault("labels", {}).update(meta["labels"])
    value["metadata"].setdefault("annotations", {})[publication.SPEC_HASH] = wanted
    checkpoint(step)
    applied = client.create(value) if current is None else client.replace(value)
    if applied is None:
        latest = client.get(kind, meta["name"], meta["namespace"], optional=True)
        latest_identity = publication._identity(latest)
        if not latest_identity or latest_identity["specHash"] != wanted:
            raise OperationError("ops.change.stale")
    after = client.get(kind, meta["name"], meta["namespace"])
    if publication._identity(after)["specHash"] != wanted:
        raise OperationError("ops.change.stale")


def publish(client, manifests_, expected, checkpoint):
    monitor, policy, rule, _ = manifests_
    for kind, manifest, key, step in (
        (POLICY, policy, "policy", "monitoring-network-egress"),
        (MONITOR, monitor, "monitor", "monitoring-network-servicemonitor"),
        (RULES, rule, "rule", "monitoring-network-rules"),
    ):
        _write(client, kind, manifest, expected[key], step, checkpoint)
    return {"state": "resources-written", "evaluation": "unverified"}
