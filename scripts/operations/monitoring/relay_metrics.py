# SPDX-License-Identifier: MIT
"""Render and validate notification-relay metrics collection resources."""
from ..adapters.kubernetes import resource_name
from ..models import OperationError, fingerprint
from .publication import OWNER, SPEC_HASH
from .relay_publication import METRICS_PORT_NAME, METRICS_POD_LABEL, METRICS_POD_LABEL_VALUE

PROMETHEUS = "prometheuses.monitoring.coreos.com"
POD_MONITOR = "podmonitors.monitoring.coreos.com"
RULE = "prometheusrules.monitoring.coreos.com"
POD_LABEL = METRICS_POD_LABEL
POD_LABEL_VALUE = METRICS_POD_LABEL_VALUE
MONITOR_NAME = "atlas-notification-relay"
RULE_NAME = "atlas-notification-relay"
ALERT_NAME = "AtlasNotificationDeliveryFailure"


def resources(namespace, release, prometheus_name, auth_secret, auth_key, summary, description):
    """Build PodMonitor and failure alert; all credentials stay Secret references."""
    resource_name(namespace)
    resource_name(release)
    resource_name(prometheus_name)
    resource_name(auth_secret)
    if (not isinstance(auth_key, str) or not auth_key or len(auth_key) > 253 or
            any(not (ch.isascii() and (ch.isalnum() or ch in "._-")) for ch in auth_key)):
        raise ValueError("invalid relay auth key")
    for text in (summary, description):
        if (not isinstance(text, str) or not text or len(text) > 256 or
                any(ord(ch) < 32 for ch in text) or "{{" in text or "}}" in text):
            raise ValueError("invalid relay rule text")
    labels = {"release": release, OWNER: "monitoring"}
    monitor = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {"name": MONITOR_NAME, "namespace": namespace, "labels": labels},
        "spec": {
            "namespaceSelector": {"matchNames": [namespace]},
            "selector": {"matchLabels": {POD_LABEL: POD_LABEL_VALUE}},
            "podMetricsEndpoints": [{
                "port": METRICS_PORT_NAME,
                "path": "/metrics",
                "interval": "30s",
                "scrapeTimeout": "10s",
                "authorization": {"type": "Bearer", "credentials": {"name": auth_secret, "key": auth_key}},
            }],
        },
    }
    rule = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {"name": RULE_NAME, "namespace": namespace, "labels": labels},
        "spec": {"groups": [{"name": "atlas.notification.relay", "rules": [{
            "alert": ALERT_NAME,
            "expr": 'sum(increase(atlas_relay_deliveries_total{outcome=~"unknown|retryable|rejected"}[5m])) > 0',
            "for": "30s",
            "labels": {"risk_id": "R-OBS-03", "severity": "warning"},
            "annotations": {"summary": summary, "description": description},
        }]}]},
    }
    return {"podMonitor": monitor, "rule": rule}


def _selector_matches(prometheus, selector_field, namespace_field, labels, namespace):
    spec = prometheus.get("spec")
    if not isinstance(spec, dict):
        return False
    selector = spec.get(selector_field)
    namespaces = spec.get(namespace_field)
    if (not isinstance(selector, dict) or set(selector) - {"matchLabels", "matchExpressions"} or
            selector.get("matchExpressions") not in (None, []) or namespaces not in (None, {})):
        return False
    match = selector.get("matchLabels", {})
    return isinstance(match, dict) and all(labels.get(key) == value for key, value in match.items())


def selected_prometheus(client, name, namespace, monitor, rule):
    """Return identity only if the named Prometheus selects both resources."""
    prometheus = client.get(PROMETHEUS, resource_name(name), resource_name(namespace))
    try:
        metadata = prometheus["metadata"]
        uid, version = metadata["uid"], metadata["resourceVersion"]
        if (metadata.get("namespace") != namespace or not isinstance(uid, str) or not uid or
                not isinstance(version, str) or not version):
            raise ValueError()
        if (not _selector_matches(prometheus, "podMonitorSelector", "podMonitorNamespaceSelector",
                                  monitor["metadata"]["labels"], namespace) or
                not _selector_matches(prometheus, "ruleSelector", "ruleNamespaceSelector",
                                      rule["metadata"]["labels"], namespace)):
            raise OperationError("ops.monitoring.selector")
        return {"uid": uid, "resourceVersion": version}
    except OperationError:
        raise
    except (KeyError, TypeError, ValueError):
        raise OperationError("ops.monitoring.selector") from None


def _identity(value):
    if value is None:
        return None
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        raise OperationError("ops.change.kubernetes")
    uid, version = metadata.get("uid"), metadata.get("resourceVersion")
    spec = value.get("spec")
    labels, annotations = metadata.get("labels"), metadata.get("annotations")
    if (not isinstance(uid, str) or not uid or not isinstance(version, str) or not version or
            not isinstance(spec, dict) or not isinstance(labels, dict) or not isinstance(annotations, dict) or
            labels.get(OWNER) != "monitoring" or annotations.get(SPEC_HASH) != fingerprint(spec)):
        raise OperationError("ops.change.foreign_resource")
    return {"uid": uid, "resourceVersion": version, "specHash": fingerprint(spec)}


def capture(client, namespace, prometheus_name, monitor, rule):
    """Capture selected Prometheus and owned monitor/rule identities."""
    namespace = resource_name(namespace)
    prometheus_name = resource_name(prometheus_name)
    prometheus = client.get(PROMETHEUS, prometheus_name, namespace)
    try:
        metadata = prometheus["metadata"]
        uid, version = metadata["uid"], metadata["resourceVersion"]
        if (metadata.get("namespace") != namespace or not isinstance(uid, str) or not uid or
                not isinstance(version, str) or not version):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise OperationError("ops.monitoring.selector") from None
    selected = selected_prometheus(client, prometheus_name, namespace, monitor, rule)
    current_monitor = client.get(POD_MONITOR, MONITOR_NAME, namespace, optional=True)
    current_rule = client.get(RULE, RULE_NAME, namespace, optional=True)
    for current, wanted in ((current_monitor, monitor), (current_rule, rule)):
        if current is not None:
            current_labels = current.get("metadata", {}).get("labels", {})
            if not isinstance(current_labels, dict) or any(
                    current_labels.get(key) != value for key, value in wanted["metadata"]["labels"].items()):
                raise OperationError("ops.change.foreign_resource")
    return {"prometheus": selected, "monitor": _identity(current_monitor), "rule": _identity(current_rule)}


def _write(client, manifest, kind, expected):
    meta = manifest["metadata"]
    current = client.get(kind, meta["name"], meta["namespace"], optional=True)
    if _identity(current) != expected:
        raise OperationError("ops.change.stale")
    value = manifest if current is None else {**current, "spec": manifest["spec"]}
    value = {**value, "metadata": dict(value.get("metadata", {}))}
    value["metadata"]["labels"] = {**value["metadata"].get("labels", {}), **meta["labels"]}
    value["metadata"]["annotations"] = {
        **value["metadata"].get("annotations", {}), SPEC_HASH: fingerprint(value["spec"])}
    result = client.create(value) if current is None else client.replace(value)
    if result is None:
        raise OperationError("ops.change.stale")
    return _identity(result)


def publish(client, namespace, prometheus_name, monitor, rule, expected, checkpoint):
    """Write monitored resources in a guarded order and leave evaluation unverified."""
    if capture(client, namespace, prometheus_name, monitor, rule) != expected:
        raise OperationError("ops.change.stale")
    checkpoint("monitoring-relay-pod-monitor")
    monitor_after = _write(client, monitor, POD_MONITOR, expected["monitor"])
    halfway = capture(client, namespace, prometheus_name, monitor, rule)
    if halfway != {**expected, "monitor": monitor_after}:
        raise OperationError("ops.change.stale")
    checkpoint("monitoring-relay-delivery-rule")
    rule_after = _write(client, rule, RULE, expected["rule"])
    after = capture(client, namespace, prometheus_name, monitor, rule)
    if after != {**expected, "monitor": monitor_after, "rule": rule_after}:
        raise OperationError("ops.change.stale")
    return {"state": "resources-written", "monitor": monitor_after, "rule": rule_after,
            "evaluation": "unverified"}
