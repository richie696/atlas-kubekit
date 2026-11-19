# SPDX-License-Identifier: MIT
"""Filesystem PVC capacity rules for explicitly registered claims."""
import hashlib
import re

from .rules import _DNS_LABEL, _dns_name, _message

_PVC_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?\Z")


def _valid_pvc_name(value):
    return (isinstance(value, str) and len(value) <= 253 and _PVC_NAME.fullmatch(value) is not None and
            all(part and part[0].isalnum() and part[-1].isalnum() for part in value.split(".")))


def _pvc_alert_labels(namespace, claim):
    """Keep Prometheus alert label values within Kubernetes' 63-character limit."""
    if len(claim) <= 63:
        return {"persistentvolumeclaim": claim}
    identity = hashlib.sha256((namespace + "\0" + claim).encode("utf-8")).hexdigest()[:20]
    return {"persistentvolumeclaim_id": "pvc-" + identity}


def pvc_capacity_rules(namespace, release, claims, free_percent, hold_seconds, max_age_seconds, severity, summary, description):
    if (not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace) or
            not isinstance(release, str) or len(release) > 53 or not _dns_name(release) or
            not isinstance(claims, (list, tuple)) or not 1 <= len(claims) <= 1000 or
            type(free_percent) is not int or not 1 <= free_percent <= 99 or
            type(hold_seconds) is not int or not 1 <= hold_seconds <= 86400 or
            type(max_age_seconds) is not int or not 5 <= max_age_seconds <= 86400 or
            severity not in ("critical", "warning", "info") or not _message(summary) or not _message(description)):
        raise ValueError("invalid PVC rule configuration")
    seen, generated = set(), []
    for item in claims:
        if (not isinstance(item, dict) or set(item) != {"namespace", "claim"} or
                not isinstance(item["namespace"], str) or not _DNS_LABEL.fullmatch(item["namespace"]) or
                not _valid_pvc_name(item["claim"])):
            raise ValueError("invalid PVC")
        key = item["namespace"], item["claim"]
        if key in seen:
            raise ValueError("duplicate PVC")
        seen.add(key)
        selector = '{namespace="' + key[0] + '",persistentvolumeclaim="' + key[1] + '"}'
        capacity = "kubelet_volume_stats_capacity_bytes" + selector
        available = "kubelet_volume_stats_available_bytes" + selector
        def fresh(metric, positive):
            age = "time() - timestamp(" + metric + ")"
            return "((" + metric + (" > 0" if positive else " >= 0") + ") and (" + metric + " < +Inf) and (" + age + " <= " + str(max_age_seconds) + ") and (" + age + " >= -10))"
        cap, free = fresh(capacity, True), fresh(available, False)
        terms = ["(min(" + free + " / " + cap + ") < " + str(free_percent / 100) + ")",
                 "(count(" + available + " unless " + capacity + ") > 0)",
                 "(count(" + capacity + " unless " + available + ") > 0)",
                 "(count(" + free + " > " + cap + ") > 0)"]
        for metric, valid in ((capacity, cap), (available, free)):
            terms.extend(("(count(" + metric + ") != count(" + valid + "))", "absent(" + valid + ")"))
        generated.append({"alert": "AtlasPVCFreeSpaceOrTelemetryMissing", "expr": "max(" + " or ".join(terms) + ")",
                          "for": str(hold_seconds) + "s", "labels": {"namespace": key[0],
                          **_pvc_alert_labels(*key), "risk_id": "R-STOR-01", "severity": severity},
                          "annotations": {"summary": summary, "description": description,
                                          "persistentvolumeclaim": key[1]}})
    generated.sort(key=lambda rule: (rule["labels"]["namespace"],
                                     rule["annotations"]["persistentvolumeclaim"]))
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": "atlas-pvc-capacity", "namespace": namespace,
                         "labels": {"release": release, "atlas-kubekit.io/managed-by": "monitoring"}},
            "spec": {"groups": [{"name": "atlas.storage.capacity", "rules": generated}]}}
