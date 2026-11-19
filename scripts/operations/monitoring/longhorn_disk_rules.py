# SPDX-License-Identifier: MIT
"""Rules for Longhorn disk occupancy and readiness metrics."""
from .rules import _DNS_LABEL, _dns_name, _message


def disk_capacity_rules(namespace, release, used_percent, hold_seconds, max_age_seconds,
                        severity, summaries, descriptions):
    if (not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace) or
            not isinstance(release, str) or len(release) > 53 or not _dns_name(release) or
            type(used_percent) is not int or not 1 <= used_percent <= 99 or
            type(hold_seconds) is not int or not 1 <= hold_seconds <= 86400 or
            type(max_age_seconds) is not int or not 5 <= max_age_seconds <= 86400 or
            severity not in ("critical", "warning", "info") or
            not isinstance(summaries, dict) or set(summaries) != {"capacity", "not_ready", "missing", "partial"} or
            not isinstance(descriptions, dict) or set(descriptions) != {"capacity", "not_ready", "missing", "partial"} or
            not all(_message(value) for value in (*summaries.values(), *descriptions.values()))):
        raise ValueError("invalid Longhorn disk rule configuration")

    # Longhorn Manager exposes cluster-wide series from multiple pods. Collapse
    # identical node/disk identities before arithmetic to avoid many-to-many joins.
    def fresh(metric):
        return "(" + metric + " and (time() - timestamp(" + metric + ") < " + str(max_age_seconds) + "))"

    usage_metric = fresh("longhorn_disk_usage_bytes{node!=\"\",disk!=\"\"}")
    capacity_metric = fresh("longhorn_disk_capacity_bytes{node!=\"\",disk!=\"\"}")
    status_metric = fresh('longhorn_disk_status{condition="ready",node!="",disk!=""}')
    usage = "max by (node, disk) (" + usage_metric + ")"
    capacity = "max by (node, disk) (" + capacity_metric + ")"
    status = "min by (node, disk) (" + status_metric + ")"
    ratio = "(" + usage + " / on (node, disk) " + capacity + " * 100)"
    healthy_capacity = "(" + capacity + " > 0)"
    rules = [
        {"alert": "AtlasLonghornDiskUsageHigh",
         "expr": "(" + ratio + " > " + str(used_percent) + ") and on (node, disk) " + healthy_capacity,
         "for": str(hold_seconds) + "s",
         "labels": {"risk_id": "R-STOR-04", "severity": severity},
         "annotations": {"summary": summaries["capacity"], "description": descriptions["capacity"],
                         "observed_value": "{{ $value }}%"}},
        {"alert": "AtlasLonghornDiskNotReady",
         "expr": status + " == 0",
         "for": str(hold_seconds) + "s",
         "labels": {"risk_id": "R-STOR-04", "severity": severity},
         "annotations": {"summary": summaries["not_ready"], "description": descriptions["not_ready"]}},
        {"alert": "AtlasLonghornDiskMetricsMissing",
         "expr": "absent(" + capacity_metric + ") or absent(" + usage_metric + ") or absent(" + status_metric + ")",
         "for": str(hold_seconds) + "s",
         "labels": {"risk_id": "R-STOR-04", "severity": severity},
         "annotations": {"summary": summaries["missing"], "description": descriptions["missing"]}},
        {"alert": "AtlasLonghornDiskTelemetryPartial",
         "expr": "(" + capacity + " unless on (node, disk) " + usage + ") or (" + usage +
                 " unless on (node, disk) " + capacity + ") or ((" + capacity + " or " + usage +
                 ") unless on (node, disk) " + status + ")",
         "for": str(hold_seconds) + "s",
         "labels": {"risk_id": "R-STOR-04", "severity": severity},
         "annotations": {"summary": summaries["partial"], "description": descriptions["partial"]}},
    ]
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": "atlas-longhorn-disk-capacity", "namespace": namespace,
                         "labels": {"release": release, "atlas-kubekit.io/managed-by": "monitoring"}},
            "spec": {"groups": [{"name": "atlas.storage.longhorn.disk", "rules": rules}]}}
