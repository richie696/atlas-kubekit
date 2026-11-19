# SPDX-License-Identifier: MIT
"""Render HAProxy target, backend availability and frontend capacity alerts."""
import ipaddress
import json

from .rules import _DNS_LABEL, _dns_name, _message


def load_balancer_rules(namespace, release, nodes, vips, hold_seconds, max_age_seconds,
                        severity, capacity_percent, connection_error_threshold, drift_threshold,
                        summaries, descriptions):
    if (not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace)
            or not isinstance(release, str) or len(release) > 53 or not _dns_name(release)
            or not isinstance(nodes, (list, tuple)) or not 1 <= len(nodes) <= 250
            or type(hold_seconds) is not int or not 1 <= hold_seconds <= 86400
            or type(max_age_seconds) is not int or not 5 <= max_age_seconds <= 86400
            or severity not in ("critical", "warning", "info")
            or type(capacity_percent) is not int or not 1 <= capacity_percent <= 99
            or type(connection_error_threshold) is not int or not 1 <= connection_error_threshold <= 100000
            or type(drift_threshold) is not int or not 2 <= drift_threshold <= 100
            or not isinstance(vips, (list, tuple)) or not 1 <= len(vips) <= 16
            or not isinstance(summaries, dict) or not isinstance(descriptions, dict)):
        raise ValueError("invalid load-balancer rule configuration")
    names, addresses = set(), set()
    targets, rules = [], []
    normalized_vips = []
    for vip in vips:
        try:
            parsed_vip = ipaddress.ip_address(vip)
        except (ValueError, TypeError):
            raise ValueError("invalid load-balancer VIP") from None
        if (parsed_vip.version != 4 or parsed_vip.is_loopback or parsed_vip.is_unspecified
                or parsed_vip.is_multicast or str(parsed_vip) in normalized_vips):
            raise ValueError("invalid load-balancer VIP")
        normalized_vips.append(str(parsed_vip))
    for node in nodes:
        if not isinstance(node, dict) or set(node) != {"name", "address"}:
            raise ValueError("invalid load-balancer target")
        name, address = node["name"], node["address"]
        if (not isinstance(name, str) or not _dns_name(name) or not name.startswith("lb")
                or not isinstance(address, str) or not address or name in names or address in addresses):
            raise ValueError("invalid load-balancer target")
        names.add(name)
        addresses.add(address)
        instance = address + ":8405"
        selector = "job=" + json.dumps("atlas-lb-metrics") + ",lb=" + json.dumps(name)
        metric_selector = selector + ',metrics_endpoint="haproxy"'
        metric = "up{" + metric_selector + ",instance=" + json.dumps(instance) + "}"
        age = "time() - timestamp(" + metric + ")"
        fresh = "((" + metric + " == 0 or " + metric + " == 1) and (" + age + " <= " + str(max_age_seconds) + ") and (" + age + " >= -10))"
        bad = "((" + metric + " == 0) and (" + age + " <= " + str(max_age_seconds) + ") and (" + age + " >= -10))"
        expression = "max(" + bad + " or (count(" + metric + ") != 1) or (count(" + fresh + ") != 1) or absent(" + fresh + "))"
        labels = {"node": name, "job": "atlas-lb-metrics", "instance": instance,
                  "risk_id": "R-LB-01", "severity": severity}
        rules.append({"alert": "AtlasLBTargetOrTelemetryMissing", "expr": expression,
                      "for": str(hold_seconds) + "s", "labels": labels,
                      "annotations": {"summary": summaries["target"], "description": descriptions["target"]}})
        targets.append((name, selector))
        vip_selector = selector + ',metrics_endpoint="vip"'
        active = "atlas_keepalived_active{" + vip_selector + "}"
        rules.append({"alert": "AtlasLBKeepalivedInactive", "expr": "max(1 - " + active + ") > 0",
                      "for": str(hold_seconds) + "s",
                      "labels": {"node": name, "risk_id": "R-LB-04", "severity": severity},
                      "annotations": {"summary": summaries["keepalived"],
                                      "description": descriptions["keepalived"]}})
        for vip in normalized_vips:
            owner_metric = ("atlas_keepalived_vip_owner{" + vip_selector + ",vip=" + json.dumps(vip) + "}")
            rules.append({"alert": "AtlasLBVipTelemetryMissing",
                          "expr": "absent_over_time(" + owner_metric + "[" + str(max_age_seconds) + "s])",
                          "for": str(hold_seconds) + "s",
                          "labels": {"node": name, "vip": vip, "risk_id": "R-LB-04", "severity": severity},
                          "annotations": {"summary": summaries["vip_missing"],
                                          "description": descriptions["vip_missing"]}})

    for name, selector in targets:
        active = "haproxy_backend_active_servers{" + selector + "}"
        backup = "haproxy_backend_backup_servers{" + selector + "}"
        down = "max by (lb,proxy) ((" + active + " == 0) and on (job,instance,lb,proxy) (" + backup + " == 0))"
        backend_expr = "absent(" + active + ") or " + down
        rules.append({"alert": "AtlasLBBackendUnavailableOrMetricsMissing", "expr": backend_expr,
                      "for": str(hold_seconds) + "s",
                      "labels": {"node": name, "risk_id": "R-LB-02", "severity": severity},
                      "annotations": {"summary": summaries["backend"], "description": descriptions["backend"]}})
        errors = "haproxy_backend_connection_errors_total{" + selector + ',proxy!="atlas-vip-metrics"}'
        error_expr = "sum by (lb, proxy) (increase(" + errors + "[5m])) > " + str(connection_error_threshold)
        rules.append({"alert": "AtlasLBBackendConnectionErrors", "expr": error_expr,
                      "for": str(hold_seconds) + "s",
                      "labels": {"node": name, "risk_id": "R-LB-02", "severity": severity},
                      "annotations": {"summary": summaries["connection_errors"],
                                      "description": descriptions["connection_errors"],
                                      "observed_value": "{{ $value }}"}})
        queue = "haproxy_backend_current_queue{" + selector + "}"
        queue_expr = "max by (lb, proxy) (" + queue + ") > 0"
        rules.append({"alert": "AtlasLBBackendQueueNonZero", "expr": queue_expr,
                      "for": str(hold_seconds) + "s",
                      "labels": {"node": name, "risk_id": "R-LB-03", "severity": severity},
                      "annotations": {"summary": summaries["queue"], "description": descriptions["queue"],
                                      "observed_value": "{{ $value }}"}})
        current = "haproxy_frontend_current_sessions{" + selector + ',proxy!~"stats|prometheus-metrics"}'
        limit = "haproxy_frontend_limit_sessions{" + selector + ',proxy!~"stats|prometheus-metrics"}'
        ratio = "((" + current + " / " + limit + " > " + str(capacity_percent / 100) + ") and (" + limit + " > 0))"
        rules.append({"alert": "AtlasLBFrontendCapacityHigh", "expr": ratio,
                      "for": str(hold_seconds) + "s",
                      "labels": {"node": name, "risk_id": "R-LB-03", "severity": severity},
                      "annotations": {"summary": summaries["capacity"], "description": descriptions["capacity"],
                                      "observed_value": "{{ $value }}"}})

    for vip in normalized_vips:
        owner = ('atlas_keepalived_vip_owner{job="atlas-lb-metrics",metrics_endpoint="vip",vip='
                 + json.dumps(vip) + "}")
        owners = "sum by (vip) (" + owner + ")"
        rules.append({"alert": "AtlasLBVipNoOwner", "expr": owners + " == 0",
                      "for": str(hold_seconds) + "s",
                      "labels": {"vip": vip, "risk_id": "R-LB-04", "severity": severity},
                      "annotations": {"summary": summaries["vip_no_owner"],
                                      "description": descriptions["vip_no_owner"]}})
        rules.append({"alert": "AtlasLBVipMultipleOwners", "expr": owners + " > 1",
                      "for": str(hold_seconds) + "s",
                      "labels": {"vip": vip, "risk_id": "R-LB-04", "severity": severity},
                      "annotations": {"summary": summaries["vip_multiple"],
                                      "description": descriptions["vip_multiple"]}})
        drift = "sum by (vip) (changes(" + owner + "[10m])) >= " + str(drift_threshold)
        rules.append({"alert": "AtlasLBVipOwnershipDrift", "expr": drift,
                      "for": str(hold_seconds) + "s",
                      "labels": {"vip": vip, "risk_id": "R-LB-04", "severity": severity},
                      "annotations": {"summary": summaries["vip_drift"],
                                      "description": descriptions["vip_drift"],
                                      "observed_value": "{{ $value }}"}})
    for rule in rules:
        for key in ("summary", "description"):
            text = rule["annotations"][key]
            if not _message(text):
                raise ValueError("invalid load-balancer rule message")
    rules.sort(key=lambda row: (row["labels"].get("node", ""), row["labels"].get("vip", ""), row["alert"]))
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": "atlas-lb-health", "namespace": namespace,
                         "labels": {"release": release, "atlas-kubekit.io/managed-by": "monitoring"}},
            "spec": {"groups": [{"name": "atlas.lb.health", "rules": rules}]}}
