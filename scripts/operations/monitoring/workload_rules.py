# SPDX-License-Identifier: MIT
"""Deployment availability rules for explicitly registered workloads."""
from .rules import _DNS_LABEL, _dns_name, _message


_CONTROLLERS = {
    "deployment": ("kube_deployment_spec_replicas", "kube_deployment_status_replicas_available", "Deployment"),
    "statefulset": ("kube_statefulset_replicas", "kube_statefulset_status_replicas_available", "StatefulSet"),
    "daemonset": ("kube_daemonset_status_desired_number_scheduled", "kube_daemonset_status_number_available", "DaemonSet"),
}


def controller_rules(kind, namespace, release, workloads, hold_seconds, max_age_seconds, severity, summary, description,
                     rollout_summary=None, rollout_description=None):
    if (not isinstance(kind, str) or kind not in _CONTROLLERS or not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace) or
            not isinstance(release, str) or len(release) > 53 or not _dns_name(release) or
            not isinstance(workloads, (list, tuple)) or not 1 <= len(workloads) <= 1000 or
            type(hold_seconds) is not int or not 1 <= hold_seconds <= 86400 or
            type(max_age_seconds) is not int or not 5 <= max_age_seconds <= 86400 or
            severity not in ("critical", "warning", "info") or
            not _message(summary) or not _message(description) or
            (kind == "deployment" and
             (not _message(rollout_summary) or not _message(rollout_description))) or
            (kind != "deployment" and (rollout_summary is not None or rollout_description is not None))):
        raise ValueError("invalid workload rule configuration")
    seen, generated = set(), []
    for item in workloads:
        if (not isinstance(item, dict) or set(item) != {"namespace", kind} or
                not isinstance(item["namespace"], str) or not _DNS_LABEL.fullmatch(item["namespace"]) or
                not _dns_name(item[kind])):
            raise ValueError("invalid workload")
        key = item["namespace"], item[kind]
        if key in seen:
            raise ValueError("duplicate workload")
        seen.add(key)
        selector = '{namespace="' + key[0] + '",' + kind + '="' + key[1] + '"}'
        desired = _CONTROLLERS[kind][0] + selector
        available = _CONTROLLERS[kind][1] + selector
        def fresh(metric):
            age = "time() - timestamp(" + metric + ")"
            return "((" + metric + " >= 0) and (" + metric + " <= 1000000) and (" + age + " <= " + str(max_age_seconds) + ") and (" + age + " >= -10))"
        expected, observed = fresh(desired), fresh(available)
        shortage = "(vector(scalar(" + observed + ") < bool scalar(" + expected + ")) == 1)"
        # Both metrics must come from the same series identity (aside from name).
        # Otherwise complementary gaps from different scrapers could look healthy.
        terms = [shortage, "(count(" + desired + " unless " + available + ") > 0)",
                 "(count(" + available + " unless " + desired + ") > 0)"]
        for metric, valid in ((desired, expected), (available, observed)):
            terms.extend(("(count(" + metric + ") != 1)", "(count(" + valid + ") != 1)", "absent(" + valid + ")"))
        generated.append({"alert": "Atlas" + _CONTROLLERS[kind][2] + "AvailabilityOrTelemetryMissing", "expr": "max(" + " or ".join(terms) + ")",
                          "for": str(hold_seconds) + "s", "labels": {"namespace": key[0], kind: key[1],
                          "risk_id": "R-WORK-01", "severity": severity},
                          "annotations": {"summary": summary, "description": description}})
        if kind == "deployment":
            condition = ('kube_deployment_status_condition{condition="Progressing",status="false",'
                         'reason="ProgressDeadlineExceeded",namespace="' + key[0] + '",deployment="' + key[1] + '"}')
            condition_age = "time() - timestamp(" + condition + ")"
            valid_condition = ("(" + condition + " == 1) and (" + condition_age + " <= " +
                               str(max_age_seconds) + ") and (" + condition_age + " >= -10)")
            generated.append({"alert": "AtlasDeploymentRolloutStalled", "expr": "max by (namespace,deployment) (" +
                             valid_condition + ")", "for": str(hold_seconds) + "s",
                             "labels": {"namespace": key[0], "deployment": key[1],
                                        "risk_id": "R-WORK-04", "severity": severity},
                             "annotations": {"summary": rollout_summary, "description": rollout_description}})
    generated.sort(key=lambda rule: (rule["labels"]["namespace"], rule["labels"][kind]))
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": "atlas-" + kind + "-availability", "namespace": namespace,
                         "labels": {"release": release, "atlas-kubekit.io/managed-by": "monitoring"}},
            "spec": {"groups": [{"name": "atlas.workload." + kind + ".availability", "rules": generated}]}}


def deployment_rules(namespace, release, workloads, hold_seconds, max_age_seconds, severity, summary, description,
                     rollout_summary, rollout_description):
    """Retain the original Deployment rule and group identity for existing plans."""
    result = controller_rules("deployment", namespace, release, workloads, hold_seconds, max_age_seconds,
                              severity, summary, description, rollout_summary, rollout_description)
    result["spec"]["groups"][0]["name"] = "atlas.workload.availability"
    return result
