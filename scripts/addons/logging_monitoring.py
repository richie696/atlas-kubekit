# SPDX-License-Identifier: MIT
"""Reconcile Prometheus collection for the installed Atlas logging releases."""
import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.addons.cert_manager_monitoring import selected_by
from scripts.lib.i18n import translate
from scripts.operations.adapters.kubernetes import Kubernetes
from scripts.operations.changes.coordinator import ClusterCoordinator
from scripts.operations.models import OperationError, fingerprint
from scripts.operations.monitoring import publication

NAMESPACE = "monitoring"
RELEASE = "kube-prometheus-stack"
PROMETHEUS = RELEASE + "-prometheus"
POD_MONITOR = "podmonitors.monitoring.coreos.com"
RULE_NAME = "atlas-logging-health"
COMPONENTS = {
    "loki": (
        "loki_distributor_bytes_received_total",
        "loki_distributor_lines_received_total",
        "loki_discarded_bytes_total",
        "loki_discarded_samples_total",
    ),
    "alloy": (
        "loki_write_sent_bytes_total",
        "loki_write_sent_entries_total",
        "loki_write_dropped_bytes_total",
        "loki_write_dropped_entries_total",
        "loki_write_batch_retries_total",
    ),
}
LABELS = {"release": RELEASE, publication.OWNER: "monitoring"}


def pod_monitor(component):
    metrics = COMPONENTS[component]
    name = "atlas-logging-" + component
    labels = {f"app.kubernetes.io/{key}": component for key in ("name", "instance")}
    if component == "loki":
        labels["app.kubernetes.io/component"] = "single-binary"
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": dict(LABELS)},
        "spec": {
            "namespaceSelector": {"matchNames": ["logging"]},
            "selector": {"matchLabels": labels},
            "podMetricsEndpoints": [{
                "port": "http-metrics", "path": "/metrics", "interval": "30s", "scrapeTimeout": "10s",
                "relabelings": [{"action": "replace", "targetLabel": "job", "replacement": name}],
                "metricRelabelings": [{
                    "action": "keep", "sourceLabels": ["__name__"],
                    "regex": "|".join(re.escape(metric) for metric in metrics),
                }],
            }],
        },
    }


def logging_rules(components, locale):
    def rule(name, expression, hold, message, sample=None):
        prefix = "phase03.monitoring.logs_" + message
        annotations = {key: translate(locale, prefix + "_" + key) for key in ("summary", "description")}
        if sample:
            annotations["observed_value"] = sample
        return {"alert": name, "expr": expression, "for": hold,
                "labels": {"severity": "warning", "risk_id": "R-OBS-05"}, "annotations": annotations}

    rules = []
    for component in COMPONENTS:
        if component not in components:
            continue
        target = 'up{job="atlas-logging-' + component + '"}'
        rules.append(rule(
            "Atlas" + component.capitalize() + "MetricsUnavailable",
            "(" + target + " == 0) or absent(" + target + ")", "2m", component + "_metrics"))
    if "loki" in components:
        rules.append(rule(
            "AtlasLokiLogIngestionDiscarded",
            'sum by (namespace,pod,reason) (increase(loki_discarded_samples_total{job="atlas-logging-loki"}[5m])) > 0',
            "1m", "loki_discards", '{{ printf "%.0f" $value }}'))
    if "alloy" in components:
        rules.append(rule(
            "AtlasAlloyLogDeliveryDropped",
            'sum by (namespace,pod) (increase(loki_write_dropped_entries_total{job="atlas-logging-alloy"}[5m])) > 0',
            "1m", "alloy_drops", '{{ printf "%.0f" $value }}'))
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": RULE_NAME, "namespace": NAMESPACE, "labels": dict(LABELS)},
            "spec": {"groups": [{"name": "atlas.logging.health", "rules": rules}]}}


def _monitor_candidate(client, manifest):
    current = client.get(POD_MONITOR, manifest["metadata"]["name"], NAMESPACE, optional=True)
    if current is None:
        return manifest, client.create
    meta = current["metadata"]
    spec = current["spec"]
    if (any(meta.get("labels", {}).get(key) != value for key, value in LABELS.items())
            or meta.get("annotations", {}).get(publication.SPEC_HASH) != fingerprint(spec)):
        raise OperationError("ops.change.foreign_resource")
    if spec == manifest["spec"]:
        return None, None
    current["spec"] = manifest["spec"]
    return current, client.replace


def reconcile(client, components, locale):
    monitors = [pod_monitor(component) for component in COMPONENTS if component in components]
    rule = logging_rules(components, locale)
    with ClusterCoordinator(client).hold("monitoring-logging-collection"):
        namespace = client.get("namespace", NAMESPACE)
        prometheus = client.get(publication.PROMETHEUS, PROMETHEUS, NAMESPACE)
        if any(not selected_by(prometheus, namespace, monitor) for monitor in monitors):
            raise OperationError("ops.monitoring.selector")
        expected_rule = publication.capture(client, rule, PROMETHEUS)
        candidates = [_monitor_candidate(client, monitor) for monitor in monitors]
        for value, write in candidates:
            if value is None:
                continue
            value["metadata"].setdefault("annotations", {})[publication.SPEC_HASH] = fingerprint(value["spec"])
            if write(value) is None:
                raise OperationError("ops.change.stale")
        publication.publish(client, rule, PROMETHEUS, expected_rule)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--lang", required=True)
    parser.add_argument("components", nargs="+", choices=tuple(COMPONENTS))
    args = parser.parse_args()
    try:
        reconcile(Kubernetes(Path(args.kubeconfig).absolute()), args.components, args.lang)
    except OperationError as error:
        print(translate(args.lang, error.key, *error.values), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
