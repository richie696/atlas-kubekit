# SPDX-License-Identifier: MIT
"""Bounded kube-state-metrics alerts for common workload failure states."""
from .rules import _DNS_LABEL, _dns_name, _message


_POD_FAILURE_REASONS = "CrashLoopBackOff|ImagePullBackOff|ErrImagePull|CreateContainerConfigError|RunContainerError|InvalidImageName"


def workload_health_rules(namespace, release, hold_seconds, max_age_seconds, restart_threshold,
                          window_seconds, job_timeout_seconds, cronjob_overdue_seconds,
                          keda_latency_seconds, keda_namespace, severity, summaries, description):
    if (not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace) or
            not isinstance(release, str) or len(release) > 53 or not _dns_name(release) or
            type(hold_seconds) is not int or not 1 <= hold_seconds <= 86400 or
            type(max_age_seconds) is not int or not 15 <= max_age_seconds <= 86400 or
            type(restart_threshold) is not int or not 1 <= restart_threshold <= 100 or
            type(window_seconds) is not int or not 60 <= window_seconds <= 3600 or
            type(job_timeout_seconds) is not int or not 60 <= job_timeout_seconds <= 604800 or
            type(cronjob_overdue_seconds) is not int or not 60 <= cronjob_overdue_seconds <= 604800 or
            type(keda_latency_seconds) is not int or not 1 <= keda_latency_seconds <= 300 or
            not isinstance(keda_namespace, str) or (keda_namespace and not _DNS_LABEL.fullmatch(keda_namespace)) or
            severity not in ("critical", "warning", "info") or
            not isinstance(summaries, dict) or
            set(summaries) != {"pending", "container", "restarts", "oom", "job", "job_timeout",
                               "cronjob_overdue", "hpa", "hpa_metrics_unavailable", "keda_scaler_errors",
                               "keda_object_errors", "keda_empty_response",
                               "keda_latency", "keda_target"} or
            not all(_message(value) for value in summaries.values()) or not _message(description)):
        raise ValueError("invalid workload health rule configuration")

    def fresh(metric, condition="== 1"):
        age = "(time() - timestamp(" + metric + "))"
        selected = "(" + metric + " " + condition + ")" if condition else metric
        return ("(" + selected + " and (" + age + " <= " + str(max_age_seconds) +
                ") and (" + age + " >= -10))")

    pending = 'kube_pod_status_phase{phase="Pending"}'
    waiting = 'kube_pod_container_status_waiting_reason{reason=~"' + _POD_FAILURE_REASONS + '"}'
    restart_count = "kube_pod_container_status_restarts_total"
    oom = 'kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}'
    job_failed = "kube_job_status_failed"
    job_active = "kube_job_status_active"
    job_start = "kube_job_status_start_time"
    cronjob_next = "kube_cronjob_next_schedule_time"
    cronjob_suspended = 'kube_cronjob_spec_suspend == 0'
    hpa_current = "kube_horizontalpodautoscaler_status_current_replicas"
    hpa_max = "kube_horizontalpodautoscaler_spec_max_replicas"
    hpa_limited = 'kube_horizontalpodautoscaler_status_condition{condition="ScalingLimited",status="true"}'
    hpa_scaling_inactive = 'kube_horizontalpodautoscaler_status_condition{condition="ScalingActive",status="false"}'
    keda_scaler_errors = "keda_scaler_detail_errors_total"
    keda_object_errors = "keda_scaled_object_errors_total"
    keda_empty_upstream = "keda_scaler_empty_upstream_responses_total"
    keda_latency = "keda_scaler_metrics_latency_seconds"
    keda_target = ("max by (namespace) (up{namespace=\"" + keda_namespace +
                   "\",service=\"keda-operator\"} == 0) or max by (namespace) ("
                   "absent_over_time(up{namespace=\"" + keda_namespace + "\",service=\"keda-operator\"}[" +
                   str(max_age_seconds) + "s]))"
                   if keda_namespace else "vector(0)")

    restart_increase = "increase(" + restart_count + "[" + str(window_seconds) + "s])"
    def counter_increase_fresh(metric, labels):
        increase = "increase(" + metric + "[" + str(window_seconds) + "s])"
        age = "(time() - timestamp(" + metric + "))"
        dimensions = ",".join(labels)
        fresh_source = ("max by (" + dimensions + ") ((" + age + " <= " + str(max_age_seconds) +
                        ") and (" + age + " >= -10))")
        return ("(max by (" + dimensions + ") (" + increase + " > 0) and on(" + dimensions + ") " +
                fresh_source + ")")
    restart_fresh = (
        "max by (namespace,pod,container) ((" + restart_increase + " >= " + str(restart_threshold) +
        ") and on(namespace,pod,container) ((time() - timestamp(" + restart_count + ") <= " +
        str(max_age_seconds) + ") and (time() - timestamp(" + restart_count + ") >= -10)))"
    )
    oom_fresh = (
        "max by (namespace,pod,container,reason) (" + fresh(oom) + ") and on(namespace,pod,container) "
        "max by (namespace,pod,container) ((" + restart_increase + " > 0) and on(namespace,pod,container) "
        "((time() - timestamp(" + restart_count + ") <= " + str(max_age_seconds) +
        ") and (time() - timestamp(" + restart_count + ") >= -10)))"
    )
    hpa_at_max = (
        "(max by (namespace,horizontalpodautoscaler) (" + fresh(hpa_current, "") +
        ") >= on(namespace,horizontalpodautoscaler) max by (namespace,horizontalpodautoscaler) (" +
        fresh(hpa_max, "") + "))"
    )
    hpa_limited_fresh = fresh(hpa_limited)

    entries = (
        ("AtlasPodPendingTooLong", "max by (namespace,pod) (" + fresh(pending) + ")", "R-WORK-02", "pending"),
        ("AtlasPodContainerStartFailure", "max by (namespace,pod,container,reason) (" + fresh(waiting) + ")", "R-WORK-02", "container"),
        ("AtlasPodContainerRestartBurst", restart_fresh, "R-WORK-03", "restarts"),
        ("AtlasPodOOMKilled", oom_fresh, "R-WORK-03", "oom"),
        ("AtlasJobFailure", "max by (namespace,job_name) (" + fresh(job_failed, "> 0") + ")", "R-WORK-05", "job"),
        ("AtlasJobRunningTooLong", "max by (namespace,job_name) (" + fresh(job_active, "> 0") +
         " and on(namespace,job_name) ((time() - " + job_start + " > " + str(job_timeout_seconds) +
         ") and ((time() - timestamp(" + job_start + ") <= " + str(max_age_seconds) +
         ") and (time() - timestamp(" + job_start + ") >= -10))))", "R-WORK-05", "job_timeout"),
        ("AtlasCronJobScheduleOverdue", "max by (namespace,cronjob) ((time() - " + cronjob_next +
         " > " + str(cronjob_overdue_seconds) + ") and on(namespace,cronjob) (" + cronjob_suspended +
         ") and ((time() - timestamp(" + cronjob_next + ") <= " + str(max_age_seconds) +
         ") and (time() - timestamp(" + cronjob_next + ") >= -10)))", "R-WORK-05", "cronjob_overdue"),
        ("AtlasHPAReplicaLimitOrScalingLimited", "max by (namespace,horizontalpodautoscaler) (" +
         hpa_at_max + " or " + hpa_limited_fresh + ")",
         "R-WORK-06", "hpa"),
        ("AtlasHPAMetricsUnavailable", "max by (namespace,horizontalpodautoscaler) (" +
         fresh(hpa_scaling_inactive) + ")", "R-WORK-06", "hpa_metrics_unavailable"),
        # Prometheus reserves the target's `namespace` label for the KEDA
        # namespace and renames the metric's workload namespace to
        # `exported_namespace` (honor_labels=false). Group on the latter so
        # notifications identify the ScaledObject's namespace.
        ("AtlasKEDAScalerErrors", counter_increase_fresh(keda_scaler_errors, ("exported_namespace", "scaledObject", "scaler")),
         "R-WORK-06", "keda_scaler_errors"),
        ("AtlasKEDAScaledObjectErrors", counter_increase_fresh(keda_object_errors, ("exported_namespace", "scaledObject")),
         "R-WORK-06", "keda_object_errors"),
        ("AtlasKEDAEmptyUpstreamResponses", counter_increase_fresh(
         keda_empty_upstream, ("exported_namespace", "scaledObject", "scaler")), "R-WORK-06", "keda_empty_response"),
        ("AtlasKEDAScalerMetricsSlow", "max by (exported_namespace,scaledObject,scaler) (" +
         fresh(keda_latency, "> " + str(keda_latency_seconds)) + ")", "R-WORK-06", "keda_latency"),
        ("AtlasKEDAOperatorTargetMissing", keda_target, "R-WORK-06", "keda_target"),
    )
    rules = [{"alert": name, "expr": expression, "for": str(hold_seconds) + "s",
              "labels": {"risk_id": risk, "severity": severity},
              "annotations": {"summary": summaries[key], "description": description}}
             for name, expression, risk, key in entries]
    return {"apiVersion": "monitoring.coreos.com/v1", "kind": "PrometheusRule",
            "metadata": {"name": "atlas-workload-health", "namespace": namespace,
                         "labels": {"release": release, "atlas-kubekit.io/managed-by": "monitoring"}},
            "spec": {"groups": [{"name": "atlas.workload.health", "rules": rules}]}}
