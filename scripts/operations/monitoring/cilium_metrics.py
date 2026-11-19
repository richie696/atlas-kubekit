# SPDX-License-Identifier: MIT
"""Render and publish Cilium agent and operator health metrics collection."""
import copy

from ..adapters.kubernetes import resource_name
from ..models import OperationError, fingerprint
from .publication import OWNER, SPEC_HASH

PROMETHEUS = "prometheuses.monitoring.coreos.com"
POD_MONITOR = "podmonitors.monitoring.coreos.com"
RULE = "prometheusrules.monitoring.coreos.com"
AGENT_MONITOR = "atlas-cilium-agent"
OPERATOR_MONITOR = "atlas-cilium-operator"
RULE_NAME = "atlas-cilium-health"


def _text(value):
    return (isinstance(value, str) and bool(value) and len(value) <= 256 and
            not any(ord(char) < 32 for char in value))


def resources(namespace, release, prometheus_name, hold_seconds, agent_summary,
              agent_description, operator_summary, operator_description,
              ipam_high_percent=80, bpf_pressure_percent=80, ipam_missing_summary="",
              ipam_missing_description="", ipam_summary="", ipam_description="",
              bpf_summary="", bpf_description="", connectivity_summary="",
              connectivity_description=""):
    for value in (namespace, release, prometheus_name):
        resource_name(value)
    if (type(hold_seconds) is not int or not 30 <= hold_seconds <= 86400 or
            type(ipam_high_percent) is not int or not 1 <= ipam_high_percent <= 99 or
            type(bpf_pressure_percent) is not int or not 1 <= bpf_pressure_percent <= 99 or
            not all(_text(value) for value in (agent_summary, agent_description,
                                                operator_summary, operator_description,
                                                ipam_missing_summary, ipam_missing_description,
                                                ipam_summary, ipam_description, bpf_summary,
                                                bpf_description, connectivity_summary,
                                                connectivity_description))):
        raise ValueError("invalid Cilium monitoring configuration")
    labels = {"release": release, OWNER: "monitoring"}

    def monitor(name, selector, job_label, port):
        return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PodMonitor",
                "metadata": {"name": name, "namespace": namespace, "labels": dict(labels)},
                "spec": {"namespaceSelector": {"matchNames": ["kube-system"]},
                         "selector": {"matchLabels": selector}, "jobLabel": job_label,
                         "podMetricsEndpoints": [{"portNumber": port, "path": "/metrics",
                                                  "interval": "30s", "scrapeTimeout": "10s",
                                                  "relabelings": [{"action": "replace", "sourceLabels": [
                                                      "__meta_kubernetes_pod_node_name"],
                                                      "targetLabel": "node"}]}]}}

    agent = monitor(AGENT_MONITOR, {"k8s-app": "cilium"}, "app.kubernetes.io/name", 9962)
    operator = monitor(OPERATOR_MONITOR, {"io.cilium/app": "operator"}, "io.cilium/app", 9963)
    missing_node_metric = lambda metric: (
        'kube_node_info unless on (node) max by (node) '
        '(count by (node) (' + metric + '{job="cilium-agent"}))'
    )
    rule = {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": RULE_NAME, "namespace": namespace, "labels": dict(labels)},
            "spec": {"groups": [{"name": "atlas.cilium.health", "rules": [
                {"alert": "AtlasCiliumAgentOrTelemetryMissing",
                 "expr": ('kube_node_info unless on (node) '
                          'max by (node) (up{job="cilium-agent"} == 1)'),
                 "for": str(hold_seconds) + "s",
                 "labels": {"risk_id": "R-NET-02", "severity": "warning"},
                 "annotations": {"summary": agent_summary, "description": agent_description}},
                {"alert": "AtlasCiliumOperatorTelemetryUnavailable",
                 "expr": ('(sum(up{job="operator"}) < 1) or '
                          'absent(up{job="operator"})'),
                 "for": str(hold_seconds) + "s",
                 "labels": {"risk_id": "R-NET-02", "severity": "warning"},
                 "annotations": {"summary": operator_summary, "description": operator_description}},
                {"alert": "AtlasCiliumIPAMTelemetryMissing",
                 "expr": missing_node_metric("cilium_ipam_capacity") + "\n" +
                         'or\n' + missing_node_metric("cilium_ip_addresses"),
                 "for": str(hold_seconds) + "s",
                 "labels": {"risk_id": "R-NET-04", "severity": "warning"},
                 "annotations": {"summary": ipam_missing_summary,
                                 "description": ipam_missing_description}},
                {"alert": "AtlasCiliumNodeIPAMCapacityLow",
                 "expr": ('(sum by (node, family) (cilium_ip_addresses{job="cilium-agent"}) '
                          '/ sum by (node, family) (cilium_ipam_capacity{job="cilium-agent"}) * 100) >= ' +
                          str(ipam_high_percent)),
                 "for": str(hold_seconds) + "s",
                 "labels": {"risk_id": "R-NET-04", "severity": "warning"},
                 "annotations": {"summary": ipam_summary,
                                 "description": ipam_description}},
                {"alert": "AtlasCiliumBPFMapPressureHigh",
                 "expr": ('max by (node, map_name) '
                          '(cilium_bpf_map_pressure{job="cilium-agent"}) * 100 >= ' +
                          str(bpf_pressure_percent)),
                 "for": str(hold_seconds) + "s",
                 "labels": {"risk_id": "R-NET-02", "severity": "warning"},
                 "annotations": {"summary": bpf_summary,
                                 "description": bpf_description}},
                {"alert": "AtlasCiliumNodeConnectivityDegraded",
                 "expr": ('max by (node) (cilium_node_health_connectivity_status'
                          '{job="cilium-agent",type="node",status="unreachable"}) > 0'),
                 "for": str(hold_seconds) + "s",
                 "labels": {"risk_id": "R-NET-02", "severity": "warning"},
                 "annotations": {"summary": connectivity_summary,
                                 "description": connectivity_description}},
            ]}]}}
    return {"agent": agent, "operator": operator, "rule": rule}


def _selected(prometheus, selector_field, namespace_field, labels):
    spec = prometheus.get("spec")
    if not isinstance(spec, dict):
        return False
    selector, namespaces = spec.get(selector_field), spec.get(namespace_field)
    if (not isinstance(selector, dict) or set(selector) - {"matchLabels", "matchExpressions"} or
            selector.get("matchExpressions") not in (None, []) or namespaces not in (None, {})):
        return False
    match = selector.get("matchLabels", {})
    return isinstance(match, dict) and all(labels.get(key) == value for key, value in match.items())


def _identity(value):
    if value is None:
        return None
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        raise OperationError("ops.change.kubernetes")
    labels, annotations, spec = metadata.get("labels"), metadata.get("annotations"), value.get("spec")
    uid, version = metadata.get("uid"), metadata.get("resourceVersion")
    if (not isinstance(uid, str) or not uid or not isinstance(version, str) or not version or
            not isinstance(labels, dict) or not isinstance(annotations, dict) or not isinstance(spec, dict) or
            labels.get(OWNER) != "monitoring" or annotations.get(SPEC_HASH) != fingerprint(spec)):
        raise OperationError("ops.change.foreign_resource")
    return {"uid": uid, "specHash": fingerprint(spec)}


def _cilium_ready(client):
    daemon = client.get("daemonset", "cilium", "kube-system")
    operator = client.get("deployment", "cilium-operator", "kube-system")
    try:
        agent_pod = daemon["spec"]["template"]
        agent_ports = [port for container in agent_pod["spec"]["containers"]
                       if container.get("name") == "cilium-agent" for port in container.get("ports", [])]
        agent_annotations = agent_pod["metadata"].get("annotations", {})
        operator_pod = operator["spec"]["template"]
        operator_ports = [port for container in operator_pod["spec"]["containers"]
                          for port in container.get("ports", [])]
        operator_annotations = operator_pod["metadata"].get("annotations", {})
    except (KeyError, TypeError, AttributeError):
        raise OperationError("ops.monitoring.cilium_metrics_disabled") from None
    if (not any(port.get("containerPort") == 9962 for port in agent_ports) or
            agent_annotations.get("prometheus.io/scrape") not in ("true", True) or
            agent_annotations.get("prometheus.io/port") != "9962" or
            not any(port.get("containerPort") == 9963 for port in operator_ports) or
            operator_annotations.get("prometheus.io/scrape") not in ("true", True) or
            operator_annotations.get("prometheus.io/port") != "9963"):
        raise OperationError("ops.monitoring.cilium_metrics_disabled")


def capture(client, namespace, prometheus_name, wanted):
    namespace, prometheus_name = resource_name(namespace), resource_name(prometheus_name)
    _cilium_ready(client)
    prom = client.get(PROMETHEUS, prometheus_name, namespace)
    metadata = prom.get("metadata", {})
    if (not isinstance(metadata.get("uid"), str) or not metadata["uid"] or
            not isinstance(metadata.get("resourceVersion"), str) or not metadata["resourceVersion"]):
        raise OperationError("ops.monitoring.selector")
    if (not all(_selected(prom, "podMonitorSelector", "podMonitorNamespaceSelector", item["metadata"]["labels"])
                for item in (wanted["agent"], wanted["operator"])) or
            not _selected(prom, "ruleSelector", "ruleNamespaceSelector", wanted["rule"]["metadata"]["labels"])):
        raise OperationError("ops.monitoring.selector")
    result = {"prometheus": {"uid": metadata["uid"], "specHash": fingerprint(prom["spec"])} }
    for key, manifest in (("agent", wanted["agent"]), ("operator", wanted["operator"]), ("rule", wanted["rule"])):
        meta = manifest["metadata"]
        current = client.get(POD_MONITOR if key != "rule" else RULE,
                             meta["name"], meta["namespace"], optional=True)
        if current is not None:
            labels = current.get("metadata", {}).get("labels", {})
            if not isinstance(labels, dict) or any(labels.get(k) != v for k, v in meta["labels"].items()):
                raise OperationError("ops.change.foreign_resource")
        result[key] = _identity(current)
    return result


def _write(client, manifest, kind, expected):
    metadata = manifest["metadata"]
    current = client.get(kind, metadata["name"], metadata["namespace"], optional=True)
    if _identity(current) != expected:
        raise OperationError("ops.change.stale")
    value = copy.deepcopy(manifest) if current is None else copy.deepcopy(current)
    value["spec"] = copy.deepcopy(manifest["spec"])
    value.setdefault("metadata", {}).setdefault("labels", {}).update(metadata["labels"])
    value["metadata"].setdefault("annotations", {})[SPEC_HASH] = fingerprint(value["spec"])
    applied = client.create(value) if current is None else client.replace(value)
    if applied is None:
        raise OperationError("ops.change.stale")
    return _identity(applied)


def publish(client, namespace, prometheus_name, wanted, expected, checkpoint):
    if capture(client, namespace, prometheus_name, wanted) != expected:
        raise OperationError("ops.change.stale")
    updated = dict(expected)
    for key in ("agent", "operator", "rule"):
        manifest = wanted[key]
        kind = RULE if key == "rule" else POD_MONITOR
        checkpoint("monitoring-cilium-" + key)
        updated[key] = _write(client, manifest, kind, expected[key])
        if capture(client, namespace, prometheus_name, wanted) != updated:
            raise OperationError("ops.change.stale")
    return {"state": "resources-written", "agentMonitor": updated["agent"],
            "operatorMonitor": updated["operator"], "rule": updated["rule"], "evaluation": "unverified"}
