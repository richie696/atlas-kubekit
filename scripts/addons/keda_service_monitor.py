# SPDX-License-Identifier: MIT
"""Render a selected KEDA operator ServiceMonitor without changing its Helm release."""
import argparse
import json
from pathlib import Path
import re
import sys


MONITOR_NAME = "atlas-keda-operator-metrics"
MANAGED_BY = "monitoring"


def _labels_match(selector, labels):
    if not isinstance(selector, dict) or not isinstance(labels, dict):
        return False
    match_labels = selector.get("matchLabels", {})
    expressions = selector.get("matchExpressions", [])
    if not isinstance(match_labels, dict) or not isinstance(expressions, list):
        return False
    if any(not isinstance(key, str) or labels.get(key) != value for key, value in match_labels.items()):
        return False
    for item in expressions:
        if not isinstance(item, dict):
            return False
        key, operator, values = item.get("key"), item.get("operator"), item.get("values", [])
        if not isinstance(key, str) or not isinstance(values, list):
            return False
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
            return False
    return True


def _namespace_matches(selector, target_namespace, monitor_namespace):
    # ServiceMonitor null/empty namespace selector means its own namespace.
    if selector is None:
        return monitor_namespace == prometheus_namespace
    if not isinstance(selector, dict):
        return False
    if selector.get("any") is True:
        return True
    names = selector.get("matchNames", [])
    if not isinstance(names, list):
        return False
    return target_namespace in names if names else monitor_namespace == service_namespace


def selected_by(prometheus, monitor, prometheus_namespace_labels):
    if not isinstance(prometheus, dict) or not isinstance(monitor, dict):
        return False
    prom_metadata = prometheus.get("metadata", {})
    prom_spec = prometheus.get("spec", {})
    prom_namespace = prom_metadata.get("namespace")
    monitor_metadata = monitor.get("metadata", {})
    monitor_spec = monitor.get("spec", {})
    prom_labels = prom_metadata.get("labels", {})
    monitor_labels = monitor_metadata.get("labels", {})
    prom_namespace_selector = prom_spec.get("serviceMonitorNamespaceSelector")
    if prom_namespace_selector is None:
        monitor_namespace_selected = monitor_metadata.get("namespace") == prom_namespace
    else:
        monitor_namespace_selected = _labels_match(prom_namespace_selector, prometheus_namespace_labels)
    return (monitor_namespace_selected and
            _labels_match(prom_spec.get("serviceMonitorSelector"), monitor_labels))


def _monitor_targets_service(monitor, service, service_namespace, prometheus_namespace):
    spec = monitor.get("spec", {})
    metadata = monitor.get("metadata", {})
    target_namespace_selector = spec.get("namespaceSelector")
    if target_namespace_selector is None:
        namespace_selected = metadata.get("namespace") == service_namespace
    else:
        namespace_selected = _namespace_matches(target_namespace_selector, service_namespace,
                                                metadata.get("namespace"))
    selector = spec.get("selector")
    service_labels = service.get("metadata", {}).get("labels", {})
    if not namespace_selected or not _labels_match(selector, service_labels):
        return False
    endpoints = spec.get("endpoints", [])
    return any(isinstance(endpoint, dict) and endpoint.get("port") == "metrics"
               and endpoint.get("path", "/metrics") == "/metrics" for endpoint in endpoints)


def service_monitor(prometheus, service, prometheus_namespace_labels, release,
                    existing_monitors=(), service_namespace="keda"):
    if (not isinstance(release, str) or len(release) > 63 or
            not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", release)):
        raise ValueError("invalid Prometheus release")
    prom_metadata = prometheus.get("metadata", {}) if isinstance(prometheus, dict) else {}
    prom_namespace = prom_metadata.get("namespace")
    service_metadata = service.get("metadata", {}) if isinstance(service, dict) else {}
    service_spec = service.get("spec", {}) if isinstance(service, dict) else {}
    service_labels = service_metadata.get("labels", {})
    if (not isinstance(prom_namespace, str) or not isinstance(service_namespace, str) or
            service_metadata.get("namespace") != service_namespace or
            service_labels.get("app.kubernetes.io/name") != "keda-operator" or
            service_labels.get("app.kubernetes.io/instance") != "keda" or
            not isinstance(service_spec.get("selector"), dict) or not service_spec["selector"]):
        raise ValueError("KEDA operator metrics Service is missing or incompatible")
    metric_ports = [port for port in service_spec.get("ports", [])
                    if isinstance(port, dict) and port.get("name") == "metrics"]
    if len(metric_ports) != 1 or type(metric_ports[0].get("port")) is not int:
        raise ValueError("KEDA operator metrics Service port is missing or ambiguous")

    manifest = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "ServiceMonitor",
        "metadata": {
            "name": MONITOR_NAME,
            "namespace": prom_namespace,
            "labels": {"release": release, "atlas-kubekit.io/managed-by": MANAGED_BY},
        },
        "spec": {
            "namespaceSelector": {"matchNames": [service_namespace]},
            "selector": {"matchLabels": {
                "app.kubernetes.io/name": service_labels["app.kubernetes.io/name"],
                "app.kubernetes.io/instance": service_labels["app.kubernetes.io/instance"],
            }},
            "endpoints": [{"port": "metrics", "path": "/metrics", "interval": "60s",
                           "scrapeTimeout": "30s"}],
        },
    }
    if not selected_by(prometheus, manifest, prometheus_namespace_labels):
        raise ValueError("Prometheus selectors do not select the KEDA ServiceMonitor")

    for current in existing_monitors:
        if not isinstance(current, dict):
            continue
        if (selected_by(prometheus, current, prometheus_namespace_labels) and
                _monitor_targets_service(current, service, service_namespace, prom_namespace)):
            return None
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-json", required=True, type=Path)
    parser.add_argument("--service-json", required=True, type=Path)
    parser.add_argument("--prometheus-namespace-labels-json", required=True, type=Path)
    parser.add_argument("--monitors-json", required=True, type=Path)
    parser.add_argument("--release", required=True)
    args = parser.parse_args()
    try:
        prometheus = json.loads(args.prometheus_json.read_text(encoding="utf-8"))
        service = json.loads(args.service_json.read_text(encoding="utf-8"))
        namespace = json.loads(args.prometheus_namespace_labels_json.read_text(encoding="utf-8"))
        monitors = json.loads(args.monitors_json.read_text(encoding="utf-8"))
        manifest = service_monitor(prometheus, service, namespace.get("metadata", {}).get("labels", {}),
                                   args.release, monitors.get("items", []))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        parser.error(str(error))
    if manifest is None:
        print("KEDA operator metrics are already selected by Prometheus.", file=sys.stderr)
        return 4
    sys.stdout.write(json.dumps(manifest, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
