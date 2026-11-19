# SPDX-License-Identifier: MIT
"""Pure, bounded PrometheusRule rendering; never publishes cluster resources."""
import re


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _dns_name(value):
    return (isinstance(value, str) and 0 < len(value) <= 253 and
            all(_DNS_LABEL.fullmatch(part) for part in value.split(".")))


def _message(value):
    return (isinstance(value, str) and 0 < len(value) <= 256 and
            not any(ord(char) < 32 for char in value) and "{{" not in value and "}}" not in value)


def node_readiness_rule(namespace, release, nodes, hold_seconds, max_age_seconds,
                        severity, summary, description, cpu_percent=90, memory_percent=90,
                        filesystem_percent=85, inode_percent=85, resource_messages=None):
    """Render per-node alerts which remain active on missing or stale data.

    The release label must be checked against the actual Prometheus selector by
    the publisher. Rendering does not establish selector or data compatibility.
    """
    if (not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace) or
            not isinstance(release, str) or len(release) > 53 or not _dns_name(release) or
            not isinstance(nodes, (list, tuple)) or not 1 <= len(nodes) <= 1000 or
            any(not _dns_name(node) for node in nodes) or len(set(nodes)) != len(nodes) or
            type(hold_seconds) is not int or not 1 <= hold_seconds <= 86400 or
            type(max_age_seconds) is not int or not 5 <= max_age_seconds <= 86400 or
            severity not in ("critical", "warning", "info") or
            not _message(summary) or not _message(description)):
        raise ValueError("invalid node rule configuration")
    rules = []
    for node in sorted(nodes):
        metric = 'kube_node_status_condition{condition="Ready",status="true",node="' + node + '"}'
        age = "time() - timestamp(" + metric + ")"
        fresh = "((" + metric + " == 0 or " + metric + " == 1) and (" + age + " <= " + str(max_age_seconds) + ") and (" + age + " >= -10))"
        bad = "((" + metric + " == 0) and (" + age + " <= " + str(max_age_seconds) + ") and (" + age + " >= -10))"
        expression = ("max(" + bad + " or (count(" + metric + ") != 1) or (count(" + fresh +
                      ") != 1) or absent(" + fresh + "))")
        rules.append({"alert": "AtlasNodeReadinessOrTelemetryMissing", "expr": expression,
                      "for": str(hold_seconds) + "s",
                      "labels": {"node": node, "risk_id": "R-NODE-01", "severity": severity},
                      "annotations": {"summary": summary, "description": description}})
    if resource_messages is None:
        resource_messages = {}
    if (any(type(value) is not int or not 1 <= value <= 99
            for value in (cpu_percent, memory_percent, filesystem_percent, inode_percent)) or
            not isinstance(resource_messages, dict)):
        raise ValueError("invalid node resource thresholds")

    resource_rules = []
    resources = (
        ("cpu", "R-NODE-02", "AtlasNodeCPUUsageHigh", cpu_percent,
         'avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m]))',
         ('node_cpu_seconds_total{mode="idle"}',)),
        ("memory", "R-NODE-03", "AtlasNodeMemoryUsageHigh", memory_percent,
         'max by (instance) (100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))',
         ("node_memory_MemAvailable_bytes", "node_memory_MemTotal_bytes")),
        ("filesystem", "R-NODE-04", "AtlasNodeFilesystemUsageHigh", filesystem_percent,
         'max by (instance) (100 * (1 - node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"} '
         '/ node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}))',
         ('node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}',
          'node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}')),
        ("inode", "R-NODE-04", "AtlasNodeInodeUsageHigh", inode_percent,
         'max by (instance) (100 * (1 - node_filesystem_files_free{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"} '
         '/ node_filesystem_files{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}))',
         ('node_filesystem_files_free{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}',
          'node_filesystem_files{mountpoint="/",fstype!~"tmpfs|overlay|squashfs"}')),
    )
    for node in sorted(nodes):
        identity = 'node_uname_info{nodename="' + node + '"}'
        for key, risk_id, alert_name, threshold, metric_expr, freshness_metrics in resources:
            localized = resource_messages.get(key, {})
            if not isinstance(localized, dict) or (localized and set(localized) != {
                    "summary", "description", "missing_summary", "missing_description"}):
                raise ValueError("invalid node resource messages")
            resource_summary = localized.get("summary", summary)
            resource_description = localized.get("description", description)
            missing_summary = localized.get("missing_summary", resource_summary)
            missing_description = localized.get("missing_description", resource_description)
            if not all(_message(message) for message in (resource_summary, resource_description,
                                                         missing_summary, missing_description)):
                raise ValueError("invalid node resource message")
            if key == "cpu":
                usage = "100 * (1 - " + metric_expr + ")"
            elif key == "memory":
                usage = metric_expr
            else:
                usage = metric_expr
            mapped_usage = "(" + usage + ") * on(instance) group_left(nodename) " + identity
            freshness = " and on(instance) ".join(
                "(max by (instance) (timestamp(" + metric + ")) > time() - " +
                str(max_age_seconds) + " and max by (instance) (timestamp(" + metric +
                ")) <= time() + 10)"
                for metric in freshness_metrics)
            resource_rules.append({
                "alert": alert_name,
                "expr": "(" + mapped_usage + " > " + str(threshold) + ") and on(instance) (" + freshness + ")",
                "for": str(hold_seconds) + "s",
                "labels": {"node": node, "risk_id": risk_id, "severity": severity},
                "annotations": {"summary": resource_summary, "description": resource_description,
                                "observed_value": "{{ $value }}%"},
            })
            missing_name = {"cpu": "AtlasNodeCPUTelemetryMissing",
                            "memory": "AtlasNodeMemoryTelemetryMissing",
                            "filesystem": "AtlasNodeFilesystemTelemetryMissing",
                            "inode": "AtlasNodeInodeTelemetryMissing"}[key]
            # `unless` detects a missing individual metric while node identity
            # remains visible; absent_over_time handles a vanished exporter.
            missing_metrics = " or ".join(
                "(" + identity + " unless on(instance) (" + metric +
                " and timestamp(" + metric + ") > time() - " + str(max_age_seconds) +
                " and timestamp(" + metric + ") <= time() + 10))"
                for metric in freshness_metrics)
            missing = "(" + missing_metrics + ") or absent_over_time(" + identity + "[" + str(max_age_seconds) + "s])"
            resource_rules.append({
                "alert": missing_name,
                "expr": missing,
                "for": str(hold_seconds) + "s",
                "labels": {"node": node, "risk_id": risk_id, "severity": severity},
                "annotations": {"summary": missing_summary, "description": missing_description},
            })
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": "atlas-node-readiness", "namespace": namespace,
                         "labels": {"release": release, "atlas-kubekit.io/managed-by": "monitoring"}},
            "spec": {"groups": [{"name": "atlas.node.readiness", "rules": rules},
                                 {"name": "atlas.node.resources", "rules": resource_rules}]}}
