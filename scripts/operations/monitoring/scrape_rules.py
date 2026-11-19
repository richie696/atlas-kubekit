# SPDX-License-Identifier: MIT
"""Render explicit Prometheus scrape-target coverage rules without publishing."""
import hashlib
import json
from .rules import _DNS_LABEL, _dns_name, _message


def _target_selector(target):
    labels = target.get("labels", {})
    matchers = [("job", target["job"]), ("instance", target["instance"])]
    matchers.extend(sorted(labels.items()))
    return ",".join(name + "=" + json.dumps(value, ensure_ascii=True) for name, value in matchers)


def _target_identity(target):
    return (target["job"], target["instance"], tuple(sorted(target.get("labels", {}).items())))


def _binary_rules(namespace, release, targets, hold_seconds, max_age_seconds, severity, summary, description,
                  metric_name, alert_name, risk_id, resource_name, group_name):
    if (not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace) or
            not isinstance(release, str) or len(release) > 53 or not _dns_name(release) or
            not isinstance(targets, (list, tuple)) or not 1 <= len(targets) <= 1000 or
            type(hold_seconds) is not int or not 1 <= hold_seconds <= 86400 or
            type(max_age_seconds) is not int or not 5 <= max_age_seconds <= 86400 or
            severity not in ("critical", "warning", "info") or
            not _message(summary) or not _message(description)):
        raise ValueError("invalid scrape rule configuration")
    seen, rules = set(), []
    for target in targets:
        if (not isinstance(target, dict) or not {"job", "instance"}.issubset(target) or
                set(target) - {"job", "instance", "labels"} or
                any(not isinstance(value, str) or not value or len(value) > 256 or
                    "{{" in value or "}}" in value or
                    any(ord(c) < 32 or ord(c) == 127 for c in value)
                    for value in (target.get("job"), target.get("instance")) ) or
                not isinstance(target.get("labels", {}), dict) or
                any(not isinstance(name, str) or not name or name in ("job", "instance", "target_id") or
                    name.startswith("__") or
                    not ("A" <= name[0] <= "Z" or "a" <= name[0] <= "z" or name[0] == "_") or
                    any(not ("A" <= char <= "Z" or "a" <= char <= "z" or "0" <= char <= "9" or char == "_")
                        for char in name) or
                    not isinstance(value, str) or not value or len(value) > 256 or
                    "{{" in value or "}}" in value or
                    any(ord(char) < 32 or ord(char) == 127 for char in value)
                    for name, value in target.get("labels", {}).items())):
            raise ValueError("invalid scrape target")
        key = _target_identity(target)
        if key in seen:
            raise ValueError("duplicate scrape target")
        seen.add(key)
        selector = _target_selector(target)
        metric = metric_name + "{" + selector + "}"
        age = "time() - timestamp(" + metric + ")"
        fresh = "((" + metric + " == 0 or " + metric + " == 1) and (" + age + " <= " + str(max_age_seconds) + ") and (" + age + " >= -10))"
        bad = "((" + metric + " == 0) and (" + age + " <= " + str(max_age_seconds) + ") and (" + age + " >= -10))"
        expression = ("max(" + bad + " or (count(" + metric + ") != 1) or (count(" + fresh +
                      ") != 1) or absent(" + fresh + "))")
        labels = {"job": target["job"], "instance": target["instance"],
                  "risk_id": risk_id, "severity": severity}
        if target.get("labels"):
            stable_id = json.dumps(key, ensure_ascii=True, separators=(",", ":"))
            labels["target_id"] = hashlib.sha256(stable_id.encode("utf-8")).hexdigest()[:12]
        rules.append({"alert": alert_name, "expr": expression,
                      "for": str(hold_seconds) + "s", "labels": labels,
                      "annotations": {"summary": summary, "description": description}})
    rules.sort(key=lambda rule: (rule["labels"]["job"], rule["labels"]["instance"],
                                 rule["labels"].get("target_id", "")))
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": resource_name, "namespace": namespace,
                         "labels": {"release": release, "atlas-kubekit.io/managed-by": "monitoring"}},
            "spec": {"groups": [{"name": group_name, "rules": rules}]}}


def target_rules(namespace, release, targets, hold_seconds, max_age_seconds, severity, summary, description):
    return _binary_rules(namespace, release, targets, hold_seconds, max_age_seconds, severity, summary, description,
                         "up", "AtlasScrapeTargetOrTelemetryMissing", "R-OBS-01", "atlas-scrape-targets", "atlas.scrape.targets")


def etcd_leader_rules(namespace, release, targets, hold_seconds, max_age_seconds, severity, summary, description):
    """Alert on member leader signals and loss of majority across one etcd cluster."""
    result = _binary_rules(namespace, release, targets, hold_seconds, max_age_seconds, severity, summary, description,
                           "etcd_server_has_leader", "AtlasEtcdMemberLeaderOrTelemetryMissing", "R-ETCD-01",
                           "atlas-etcd-leader", "atlas.etcd.leader")
    majority = len(targets) // 2 + 1
    terms = []
    for target in targets:
        selector = "job=" + json.dumps(target["job"], ensure_ascii=True) + ",instance=" + json.dumps(
            target["instance"], ensure_ascii=True)
        metric = "etcd_server_has_leader{" + selector + "}"
        age = "time() - timestamp(" + metric + ")"
        fresh_leader = ("max by (job,instance) ((" + metric + " == 1) and (" + age + " <= " +
                        str(max_age_seconds) + ") and (" + age + " >= -10))")
        terms.append("sum((" + fresh_leader + ") or vector(0))")
    expression = "(" + " + ".join(terms) + ") < " + str(majority)
    result["spec"]["groups"][0]["rules"].append({
        "alert": "AtlasEtcdQuorumOrTelemetryMissing", "expr": expression,
        "for": str(hold_seconds) + "s",
        "labels": {"risk_id": "R-ETCD-01", "severity": severity,
                   "registered_members": str(len(targets)), "quorum_required": str(majority)},
        "annotations": {"summary": summary, "description": description,
                        "observed_value": "{{ $value }}"},
    })
    return result


def control_plane_rules(namespace, release, targets, hold_seconds, max_age_seconds, severity, summary, description):
    """Alert on explicitly enrolled control-plane metrics endpoints.

    Scrape authentication and endpoint discovery are owned by Prometheus; this
    rule reports unavailable, stale, duplicate, or missing enrolled targets.
    It does not replace an authenticated API request probe.
    """
    return _binary_rules(namespace, release, targets, hold_seconds, max_age_seconds, severity, summary, description,
                         "up", "AtlasControlPlaneTargetOrTelemetryMissing", "R-CP-01",
                         "atlas-control-plane-targets", "atlas.control-plane.targets")


def core_dns_rules(namespace, release, targets, hold_seconds, max_age_seconds, severity,
                   error_percent, window_seconds, latency_ms, summary, description,
                   error_summary, error_description, latency_summary, latency_description,
                   deployment, min_replicas, replica_summary, replica_description,
                   latency_min_requests=100):
    """Alert on enrolled CoreDNS endpoints, response errors, and P95 latency.

    The endpoint rule detects absent, stale, duplicated, or down scrape data.
    The response error and latency rules only evaluate explicit targets and
    suppress zero-traffic periods; they do not replace active DNS queries from
    inside/outside the cluster.
    """
    if (type(error_percent) is not int or not 1 <= error_percent <= 100 or
            type(window_seconds) is not int or not 5 <= window_seconds <= 86400 or
            type(latency_ms) is not int or not 1 <= latency_ms <= 60000 or
            type(latency_min_requests) is not int or not 1 <= latency_min_requests <= 10000000 or
            not _message(error_summary) or not _message(error_description) or
            not _message(latency_summary) or not _message(latency_description) or
            not isinstance(deployment, dict) or set(deployment) != {"namespace", "name"} or
            not isinstance(deployment["namespace"], str) or not _DNS_LABEL.fullmatch(deployment["namespace"]) or
            not isinstance(deployment["name"], str) or not _dns_name(deployment["name"]) or
            type(min_replicas) is not int or not 1 <= min_replicas <= 1000 or
            not _message(replica_summary) or not _message(replica_description)):
        raise ValueError("invalid CoreDNS threshold configuration")
    result = _binary_rules(namespace, release, targets, hold_seconds, max_age_seconds, severity,
                           summary, description, "coredns_build_info", "AtlasCoreDNSTargetOrTelemetryMissing",
                           "R-NET-01", "atlas-coredns-health", "atlas.dns.health")
    rules = result["spec"]["groups"][0]["rules"]
    for endpoint_rule in tuple(rules):
        job = endpoint_rule["labels"]["job"]
        instance = endpoint_rule["labels"]["instance"]
        selector = "job=" + json.dumps(job, ensure_ascii=True) + ",instance=" + json.dumps(instance, ensure_ascii=True)
        total = "sum(rate(coredns_dns_responses_total{" + selector + "}[" + str(window_seconds) + "s]))"
        errors = ("(sum(rate(coredns_dns_responses_total{" + selector + ',rcode=~"SERVFAIL|REFUSED"}' +
                  "[" + str(window_seconds) + "s])) or vector(0))")
        rules.append({"alert": "AtlasCoreDNSResponseErrorRateHigh",
                      # Keep the ratio on the left so the alert value is a percentage.
                      "expr": "((100 * " + errors + " / " + total + " > " + str(error_percent) +
                              ") and (" + total + " > 0))",
                      "for": str(hold_seconds) + "s",
                      "labels": {"job": job, "instance": instance, "risk_id": "R-NET-01", "severity": severity},
                      "annotations": {"summary": error_summary, "description": error_description,
                                      "observed_value": '{{ printf "%.2f%%" $value }}'}})
        histogram = "coredns_dns_request_duration_seconds_bucket{" + selector + "}"
        request_count = "coredns_dns_request_duration_seconds_count{" + selector + "}"
        p95 = ("histogram_quantile(0.95, sum by (le) (rate(" + histogram + "[" +
               str(window_seconds) + "s])))")
        requests = "sum(increase(" + request_count + "[" + str(window_seconds) + "s]))"
        rules.append({"alert": "AtlasCoreDNSP95LatencyHigh",
                      # PromQL set operators preserve the left-hand sample value.
                      # Keep P95 on the left so $value remains seconds, not request count.
                      "expr": "((" + p95 + " > " + format(latency_ms / 1000, ".3f") +
                              ") and (" + requests + " >= " + str(latency_min_requests) + "))",
                      "for": str(hold_seconds) + "s",
                      "labels": {"job": job, "instance": instance, "risk_id": "R-NET-01", "severity": severity},
                      "annotations": {"summary": latency_summary, "description": latency_description,
                                      "observed_value": '{{ printf "%.3f" $value }}s'}})
    rules.sort(key=lambda rule: (rule["labels"]["job"], rule["labels"]["instance"],
                                 rule["alert"] != "AtlasCoreDNSTargetOrTelemetryMissing"))
    deployment_selector = ('namespace=' + json.dumps(deployment["namespace"], ensure_ascii=True) +
                           ',deployment=' + json.dumps(deployment["name"], ensure_ascii=True))
    available = "kube_deployment_status_replicas_available{" + deployment_selector + "}"
    rules.append({"alert": "AtlasCoreDNSReplicasBelowMinimum",
                  "expr": "(min by (namespace,deployment) (" + available + ") < " + str(min_replicas) +
                          ") or absent(" + available + ")",
                  "for": str(hold_seconds) + "s",
                  "labels": {"namespace": deployment["namespace"], "deployment": deployment["name"],
                             "minimum_replicas": str(min_replicas), "risk_id": "R-NET-01", "severity": severity},
                  "annotations": {"summary": replica_summary, "description": replica_description}})
    return result
