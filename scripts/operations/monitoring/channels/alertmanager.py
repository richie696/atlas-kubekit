# SPDX-License-Identifier: MIT
"""Validate Alertmanager v4 grouped notifications and render bounded robot text.

This module performs no I/O and intentionally does not accept destinations.
Alert labels and annotations are untrusted: only explicitly selected fields are
forwarded. The caller must separately authenticate requests and select a route.
"""
import json
import re
from dataclasses import dataclass
from scripts.lib.i18n import available_locales, translate

MAX_BODY = 262144
MAX_ALERTS = 100
_FINGERPRINT = re.compile(r"[0-9a-fA-F]{1,64}\Z")
ALERT_NAME_KEYS = {
    "AtlasNodeReadinessOrTelemetryMissing": "node_readiness",
    "AtlasNodeCPUUsageHigh": "node_cpu",
    "AtlasNodeCPUTelemetryMissing": "node_cpu_missing",
    "AtlasNodeMemoryUsageHigh": "node_memory",
    "AtlasNodeMemoryTelemetryMissing": "node_memory_missing",
    "AtlasNodeFilesystemUsageHigh": "node_filesystem",
    "AtlasNodeFilesystemTelemetryMissing": "node_filesystem_missing",
    "AtlasNodeInodeUsageHigh": "node_inode",
    "AtlasNodeInodeTelemetryMissing": "node_inode_missing",
    "AtlasScrapeTargetOrTelemetryMissing": "scrape_target",
    "AtlasEtcdMemberLeaderOrTelemetryMissing": "etcd_leader",
    "AtlasEtcdQuorumOrTelemetryMissing": "etcd_quorum",
    "AtlasControlPlaneTargetOrTelemetryMissing": "control_plane_target",
    "AtlasCoreDNSTargetOrTelemetryMissing": "coredns_target",
    "AtlasCoreDNSResponseErrorRateHigh": "coredns_error_rate",
    "AtlasCoreDNSP95LatencyHigh": "coredns_p95_latency",
    "AtlasCoreDNSReplicasBelowMinimum": "coredns_replicas",
    "AtlasPVCFreeSpaceOrTelemetryMissing": "pvc_free_space",
    "AtlasLonghornVolumeRobustnessOrTelemetryMissing": "longhorn_volume",
    "AtlasLonghornDiskUsageHigh": "longhorn_disk_capacity",
    "AtlasLonghornDiskNotReady": "longhorn_disk_not_ready",
    "AtlasLonghornDiskMetricsMissing": "longhorn_disk_missing",
    "AtlasLonghornDiskTelemetryPartial": "longhorn_disk_partial",
    "AtlasLBTargetOrTelemetryMissing": "lb_target",
    "AtlasLBBackendUnavailableOrMetricsMissing": "lb_backend",
    "AtlasLBBackendConnectionErrors": "lb_connection_errors",
    "AtlasLBBackendQueueNonZero": "lb_queue",
    "AtlasLBFrontendCapacityHigh": "lb_frontend_capacity",
    "AtlasLBKeepalivedInactive": "lb_keepalived_inactive",
    "AtlasLBVipTelemetryMissing": "lb_vip_telemetry_missing",
    "AtlasLBVipNoOwner": "lb_vip_no_owner",
    "AtlasLBVipMultipleOwners": "lb_vip_multiple_owners",
    "AtlasLBVipOwnershipDrift": "lb_vip_ownership_drift",
    "AtlasDeploymentAvailabilityOrTelemetryMissing": "deployment_availability",
    "AtlasDeploymentRolloutStalled": "deployment_rollout_stalled",
    "AtlasCertificateExpiringSoon": "certificate_expiring",
    "AtlasCertificateTelemetryMissing": "certificate_telemetry_missing",
    "AtlasCertificateNotReady": "certificate_not_ready",
    "AtlasStatefulSetAvailabilityOrTelemetryMissing": "statefulset_availability",
    "AtlasDaemonSetAvailabilityOrTelemetryMissing": "daemonset_availability",
    "AtlasPodPendingTooLong": "pod_pending",
    "AtlasPodContainerStartFailure": "pod_container_start",
    "AtlasPodContainerRestartBurst": "pod_restart_burst",
    "AtlasPodOOMKilled": "pod_oom_killed",
    "AtlasJobFailure": "job_failure",
    "AtlasHPAReplicaLimitOrScalingLimited": "hpa_limit",
    "AtlasHPAMetricsUnavailable": "hpa_metrics_unavailable",
    "AtlasKEDAScalerErrors": "keda_scaler_errors",
    "AtlasKEDAScaledObjectErrors": "keda_object_errors",
    "AtlasKEDAEmptyUpstreamResponses": "keda_empty_response",
    "AtlasKEDAScalerMetricsSlow": "keda_latency",
    "AtlasKEDAOperatorTargetMissing": "keda_target",
}


@dataclass(frozen=True)
class Notification:
    receiver: str
    text: str
    status: str = "firing"
    alerts: tuple = ()


def localized_alert_name(name, lang):
    key = ALERT_NAME_KEYS.get(name)
    if key is None:
        return translate(lang, "ops.monitoring.mail_alert_rule_unknown", name)
    return translate(lang, "ops.monitoring.mail_alert_rule_" + key)


def _field(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid alert field")
    # Prevent control characters and line breaks from impersonating extra fields.
    return " ".join(value.split())


def parse(body, lang="zh_CN"):
    """Return one grouped notification; malformed or oversized input fails closed."""
    if not isinstance(body, bytes) or len(body) > MAX_BODY:
        raise ValueError("invalid notification body")
    try:
        doc = json.loads(body)
        if (lang not in available_locales() or not isinstance(doc, dict) or
                doc.get("version") != "4"):
            raise ValueError()
        receiver = _field(doc["receiver"], 256)
        status = doc["status"]
        alerts = doc["alerts"]
        truncated = doc.get("truncatedAlerts", 0)
        if (not receiver or status not in ("firing", "resolved") or
                type(truncated) is not int or truncated < 0 or
                not isinstance(alerts, list) or not 1 <= len(alerts) <= MAX_ALERTS):
            raise ValueError()
        localized = lambda key: translate(lang, "ops.monitoring.mail_" + key)
        state_name = {"firing": localized("firing"), "resolved": localized("resolved")}
        lines = [localized("status") + ": " + state_name[status], localized("receiver") + ": " + receiver]
        states = []
        diagnostic_alerts = []
        for alert in alerts:
            if not isinstance(alert, dict) or alert.get("status") not in ("firing", "resolved"):
                raise ValueError()
            labels, annotations = alert["labels"], alert["annotations"]
            if not isinstance(labels, dict) or not isinstance(annotations, dict):
                raise ValueError()
            fingerprint = _field(alert["fingerprint"], 64)
            if not _FINGERPRINT.fullmatch(fingerprint):
                raise ValueError()
            rule_name = _field(labels.get("alertname", ""), 256)
            if not rule_name:
                raise ValueError()
            name = localized_alert_name(rule_name, lang)
            states.append(alert["status"])
            diagnostic_labels = {}
            for key in ("namespace", "exported_namespace", "pod", "container", "node", "deployment",
                        "statefulset", "daemonset", "job", "persistentvolumeclaim"):
                if key in labels:
                    diagnostic_labels[key] = _field(labels[key], 256)
            diagnostic_alerts.append({"name": rule_name, "status": alert["status"], "labels": diagnostic_labels,
                                      "startsAt": _field(alert["startsAt"], 64),
                                      "endsAt": _field(alert.get("endsAt", ""), 64)})
            lines.append("[" + state_name[alert["status"]] + "] " + localized("name") + ": " + name +
                         " (" + localized("fingerprint") + ": " + fingerprint + ")")
            namespace = labels.get("exported_namespace", labels.get("namespace"))
            if namespace is not None:
                lines.append(localized("namespace") + ": " + _field(namespace, 256))
            label_fields = (("cluster", "cluster"), ("node", "node"),
                            ("disk", "disk"),
                            ("pod", "pod"), ("container", "container"), ("reason", "reason"),
                            ("scaledObject", "keda_scaled_object"), ("scaler", "keda_scaler"),
                            ("horizontalpodautoscaler", "hpa"),
                            ("deployment", "deployment"), ("statefulset", "statefulset"),
                            ("daemonset", "daemonset"),
                            ("service", "service"), ("job", "job"), ("instance", "instance"),
                            ("registered_members", "etcd_members"), ("quorum_required", "etcd_quorum"),
                            ("severity", "severity"))
            for field, label_key in label_fields:
                if field in labels:
                    value = _field(labels[field], 256)
                    if field == "severity":
                        value = {"critical": localized("critical"), "warning": localized("warning"),
                                 "info": localized("info")}.get(value, value)
                    lines.append(localized(label_key) + ": " + value)
            pvc = annotations.get("persistentvolumeclaim", labels.get("persistentvolumeclaim"))
            if pvc is not None:
                lines.append(localized("pvc") + ": " + _field(pvc, 256))
            if "risk_id" in labels:
                lines.append(localized("risk_id") + ": " + _field(labels["risk_id"], 64))
            for key in ("summary", "description"):
                value = annotations.get(key)
                if value is None:
                    value = localized("no_summary" if key == "summary" else "no_description")
                lines.append(localized(key) + ": " + _field(value, 1024))
            if "observed_value" in annotations:
                lines.append(localized("observed_value") + ": " + _field(annotations["observed_value"], 256))
            lines.append(localized("start") + ": " + _field(alert["startsAt"], 64))
            if alert["status"] == "resolved":
                lines.append(localized("end") + ": " + _field(alert["endsAt"], 64))
        if ((status == "resolved" and "firing" in states) or
                (status == "firing" and "firing" not in states and not truncated)):
            raise ValueError()
        if truncated:
            lines.append(localized("additional_omitted") % truncated)
        result = "\n".join(lines)
        result.encode("utf-8")  # Reject invalid Unicode before dispatch.
        return Notification(receiver, result, status, tuple(diagnostic_alerts))
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise ValueError("invalid Alertmanager notification") from None


def response_code(outcome):
    """Map platform acknowledgement to HTTP status; unknown may have delivered."""
    if outcome == "accepted":
        return 200
    if outcome in ("retryable", "unknown", "rejected"):
        return 503
    raise ValueError("invalid delivery outcome")
