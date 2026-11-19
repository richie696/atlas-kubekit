# SPDX-License-Identifier: MIT
"""Render a cert-manager PodMonitor only when the selected Prometheus accepts it."""
import argparse
import json
from pathlib import Path
import re
import sys


def pod_monitor(prometheus_release):
    if (not isinstance(prometheus_release, str) or len(prometheus_release) > 63 or
            not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", prometheus_release)):
        raise ValueError("invalid Prometheus release name")
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {
            "name": "atlas-cert-manager-metrics",
            "namespace": "cert-manager",
            "labels": {"release": prometheus_release, "atlas-kubekit.io/managed-by": "monitoring"},
        },
        "spec": {
            "jobLabel": "app.kubernetes.io/name",
            "selector": {
                "matchExpressions": [
                    {"key": "app.kubernetes.io/name", "operator": "In",
                     "values": ["cainjector", "cert-manager", "webhook"]},
                    {"key": "app.kubernetes.io/instance", "operator": "In", "values": ["cert-manager"]},
                    {"key": "app.kubernetes.io/component", "operator": "In",
                     "values": ["cainjector", "controller", "webhook"]},
                ],
            },
            "podMetricsEndpoints": [{"port": "http-metrics", "path": "/metrics",
                                     "interval": "60s", "scrapeTimeout": "30s"}],
        },
    }


def _matches(selector, labels):
    if selector is None or not isinstance(selector, dict):
        return False
    match_labels = selector.get("matchLabels", {})
    if not isinstance(match_labels, dict) or any(labels.get(key) != value for key, value in match_labels.items()):
        return False
    expressions = selector.get("matchExpressions", [])
    if not isinstance(expressions, list):
        return False
    for expression in expressions:
        if not isinstance(expression, dict):
            return False
        key, operator, values = expression.get("key"), expression.get("operator"), expression.get("values", [])
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


def selected_by(prometheus, namespace, monitor):
    if not isinstance(prometheus, dict) or not isinstance(namespace, dict):
        return False
    prom_namespace = prometheus.get("metadata", {}).get("namespace")
    namespace_selector = prometheus.get("spec", {}).get("podMonitorNamespaceSelector")
    namespace_labels = namespace.get("metadata", {}).get("labels", {})
    if namespace_selector is None:
        namespace_selected = prom_namespace == monitor["metadata"]["namespace"]
    else:
        namespace_selected = _matches(namespace_selector, namespace_labels)
    monitor_selector = prometheus.get("spec", {}).get("podMonitorSelector")
    return namespace_selected and _matches(monitor_selector, monitor["metadata"]["labels"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-release", required=True)
    parser.add_argument("--prometheus-json", required=True, type=Path)
    parser.add_argument("--namespace-json", required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest = pod_monitor(args.prometheus_release)
        prometheus = json.loads(args.prometheus_json.read_text(encoding="utf-8"))
        namespace = json.loads(args.namespace_json.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        parser.error(str(error))
    if not selected_by(prometheus, namespace, manifest):
        print("Prometheus selectors do not select the cert-manager PodMonitor.", file=sys.stderr)
        return 3
    sys.stdout.write(json.dumps(manifest, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
