# SPDX-License-Identifier: MIT
"""Render explicitly bounded critical-to-warning/info Alertmanager inhibition."""
from .rules import _DNS_LABEL

DEFAULT_EQUAL = ("cluster", "namespace", "alertname")
ALLOWED_EQUAL = frozenset(("alertname", "cluster", "namespace", "node", "risk_id", "service", "workload"))


def normalize(enabled, source_severity, target_severity, equal):
    if type(enabled) is not bool:
        raise ValueError("invalid inhibition switch")
    if not enabled:
        return {"enabled": False, "sourceSeverity": "critical", "targetSeverity": "warning",
                "equal": list(DEFAULT_EQUAL)}
    if source_severity != "critical" or target_severity not in ("warning", "info"):
        raise ValueError("inhibition must only suppress warning or info under critical")
    labels = DEFAULT_EQUAL if equal is None else equal
    if (not isinstance(labels, (list, tuple)) or not 2 <= len(labels) <= 8 or
            any(not isinstance(label, str) or label not in ALLOWED_EQUAL for label in labels) or
            len(set(labels)) != len(labels) or "cluster" not in labels or
            not ({"alertname", "risk_id"} & set(labels))):
        raise ValueError("inhibition equal labels are too broad")
    return {"enabled": True, "sourceSeverity": source_severity,
            "targetSeverity": target_severity, "equal": list(labels)}


def manifest(namespace, release, policy):
    if not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace):
        raise ValueError("invalid namespace")
    if not isinstance(release, str) or not _DNS_LABEL.fullmatch(release):
        raise ValueError("invalid release")
    policy = normalize(policy["enabled"], policy["sourceSeverity"],
                       policy["targetSeverity"], policy["equal"])
    matchers = []
    if policy["enabled"]:
        matchers = [{"name": "severity", "value": policy["sourceSeverity"], "matchType": "="}]
    target_matchers = ([{"name": "severity", "value": policy["targetSeverity"], "matchType": "="}]
                       if policy["enabled"] else [])
    return {"apiVersion": "monitoring.coreos.com/v1alpha1", "kind": "AlertmanagerConfig",
            "metadata": {"name": "atlas-inhibition-policy", "namespace": namespace,
                         "labels": {"release": release, "atlas-kubekit.io/managed-by": "monitoring"}},
            "spec": {"route": {"receiver": "atlas-inhibition-noop", "continue": True,
                               "matchers": [{"name": "atlas_kubekit_policy", "value": "never-match",
                                             "matchType": "="}]},
                     "receivers": [{"name": "atlas-inhibition-noop"}],
                     "inhibitRules": ([{"sourceMatch": matchers, "targetMatch": target_matchers,
                                        "equal": policy["equal"]}] if policy["enabled"] else [])}}
