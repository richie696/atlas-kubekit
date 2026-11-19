#!/usr/bin/env python3
"""Validate generated node rules with real promtool in an isolated container."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.monitoring.rules import node_readiness_rule
from scripts.operations.monitoring.scrape_rules import target_rules, etcd_leader_rules, control_plane_rules
from scripts.operations.monitoring.workload_rules import deployment_rules, controller_rules
from scripts.operations.monitoring.storage_rules import pvc_capacity_rules
from scripts.operations.monitoring.longhorn_rules import volume_robustness_rules
from scripts.operations.monitoring.workload_health_rules import workload_health_rules
from scripts.operations.monitoring.certificate_rules import certificate_rules
from scripts.addons.monitoring_values import safe_monitoring_values
from scripts.addons.sources import yaml_module
from tests.e2e.monitoring_prometheus import cleanup_owned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    if parser.parse_args().confirm != "atlas-monitor-e2e":
        parser.error("Explicit confirmation atlas-monitor-e2e is required")
    suffix = uuid.uuid4().hex[:12]
    name, network = "atlas-promtool-" + suffix, "atlas-unused-" + suffix
    summary, description = "Node or telemetry unavailable", "Check the node and metrics source."
    rule = node_readiness_rule("monitoring", "monitoring-stack", ["cp1"], 1, 5,
                               "warning", summary, description)
    scrape = target_rules("monitoring", "monitoring-stack", [{"job": "api", "instance": "cp1:6443"}],
                          1, 5, "warning", summary, description)
    kubelet_targets = [
        {"job": "kubelet", "instance": "w1:10250", "labels": {"metrics_path": path}}
        for path in ("/metrics", "/metrics/cadvisor", "/metrics/probes")
    ]
    kubelet_scrape = target_rules("monitoring", "monitoring-stack", kubelet_targets,
                                  1, 5, "warning", summary, description)
    scrape["spec"]["groups"][0]["rules"].extend(kubelet_scrape["spec"]["groups"][0]["rules"])
    rule["spec"]["groups"].extend(scrape["spec"]["groups"])
    etcd = etcd_leader_rules("monitoring", "monitoring-stack", [{"job": "etcd", "instance": "cp1:2381"}],
                             1, 5, "warning", summary, description)
    rule["spec"]["groups"].extend(etcd["spec"]["groups"])
    etcd_three = etcd_leader_rules("monitoring", "monitoring-stack", [
        {"job": "etcd-e2e", "instance": "cp1:2381"},
        {"job": "etcd-e2e", "instance": "cp2:2381"},
        {"job": "etcd-e2e", "instance": "cp3:2381"},
    ], 1, 5, "critical", "etcd 多数派成员不足", "检查成员状态、对等网络和磁盘延迟。")
    etcd_group = next(group for group in rule["spec"]["groups"] if group["name"] == "atlas.etcd.leader")
    etcd_group["rules"].extend(etcd_three["spec"]["groups"][0]["rules"])
    control_plane = control_plane_rules("monitoring", "monitoring-stack", [{"job": "apiserver", "instance": "cp1:6443"}],
                                        1, 5, "warning", "控制面端点不可用", "检查授权采集目标。")
    rule["spec"]["groups"].extend(control_plane["spec"]["groups"])
    workloads = deployment_rules("monitoring", "monitoring-stack", [{"namespace": "prod", "deployment": "web"}],
                                 1, 5, "warning", summary, description,
                                 "Deployment rollout stalled", "Check rollout progress.")
    rule["spec"]["groups"].extend(workloads["spec"]["groups"])
    for kind in ("statefulset", "daemonset"):
        extra = controller_rules(kind, "monitoring", "monitoring-stack", [{"namespace": "prod", kind: "app"}],
                                 1, 5, "warning", summary, description)
        rule["spec"]["groups"].extend(extra["spec"]["groups"])
    storage = pvc_capacity_rules("monitoring", "monitoring-stack", [{"namespace": "prod", "claim": "data"}],
                                 15, 1, 5, "warning", summary, description)
    rule["spec"]["groups"].extend(storage["spec"]["groups"])
    longhorn = volume_robustness_rules("monitoring", "monitoring-stack", [{"namespace": "prod", "claim": "data"}],
                                      1, 5, "warning", summary, description)
    rule["spec"]["groups"].extend(longhorn["spec"]["groups"])
    certificates = certificate_rules("monitoring", "monitoring-stack", [{"namespace": "prod", "name": "web-tls"}],
        30, 1, 5, "warning", "Certificate expiring", "Renew certificate.", "Certificate not ready",
        "Check issuer.", "Certificate metrics missing", "Check scraping.")
    rule["spec"]["groups"].extend(certificates["spec"]["groups"])
    workload_health = workload_health_rules(
        "monitoring", "monitoring-stack", 1, 60, 2, 60, 60, 60, 5, "keda", "warning",
        {"pending": "Pod pending", "container": "Container start failure", "restarts": "Restart burst",
         "oom": "Container OOMKilled", "job": "Job failed", "job_timeout": "Job timeout",
         "cronjob_overdue": "CronJob schedule overdue", "hpa": "HPA at replica limit",
         "hpa_metrics_unavailable": "HPA scaling metrics unavailable",
         "keda_scaler_errors": "KEDA scaler query errors", "keda_object_errors": "KEDA scaling errors",
         "keda_empty_response": "KEDA scaler upstream metrics response is empty",
         "keda_latency": "KEDA scaler metrics are slow",
         "keda_target": "KEDA Operator metrics target is unavailable or missing"},
        "Check workload events, resource limits, scheduling and scaling configuration.")
    rule["spec"]["groups"].extend(workload_health["spec"]["groups"])
    safe_values = yaml_module().safe_load(safe_monitoring_values("zh_CN"))
    csi_group = safe_values["additionalPrometheusRulesMap"]["atlas-csi-attachments"]["groups"][0]
    csi_group["rules"][0]["for"] = "2s"
    csi_group["rules"][1]["for"] = "2s"
    rule["spec"]["groups"].append(csi_group)
    expected = {"exp_labels": {"node": "cp1", "risk_id": "R-NODE-01", "severity": "warning"},
                "exp_annotations": {"summary": summary, "description": description}}

    def checks(times):
        return [{"eval_time": str(second) + "s", "alertname": "AtlasNodeReadinessOrTelemetryMissing",
                 "exp_alerts": [expected] if firing else []} for second, firing in times]

    def rollout_checks(times):
        expected_alert = {"exp_labels": {"namespace": "prod", "deployment": "web",
                                          "risk_id": "R-WORK-04", "severity": "warning"},
                          "exp_annotations": {"summary": "Deployment rollout stalled",
                                              "description": "Check rollout progress."}}
        return [{"eval_time": str(t) + "s", "alertname": "AtlasDeploymentRolloutStalled",
                 "exp_alerts": [expected_alert] if firing else []} for t, firing in times]

    def certificate_checks(alert, labels, summary, description, times, observed_value=None):
        annotations = {"summary": summary, "description": description}
        if observed_value is not None:
            annotations["observed_value"] = observed_value
        expected_alert = {"exp_labels": {"namespace": "prod", "name": "web-tls",
                                          "risk_id": "R-EDGE-04", "severity": "warning", **labels},
                          "exp_annotations": annotations}
        return [{"eval_time": str(t) + "s", "alertname": alert,
                 "exp_alerts": [expected_alert] if firing else []} for t, firing in times]

    metric = 'kube_node_status_condition{condition="Ready",status="true",node="cp1",job="state"}'
    fixture = {
        "rule_files": ["rules.json"], "evaluation_interval": "1s",
        "tests": [
            {"name": "failure, stale marker and recovery", "interval": "1s",
             "input_series": [{"series": metric, "values": "1 0 0 stale _ _ 1 1"}],
             "alert_rule_test": checks([(0, False), (1, False), (2, True), (3, True),
                                        (4, True), (5, True), (6, False), (7, False)])},
            {"name": "old but not explicitly stale samples", "interval": "1s",
             "input_series": [{"series": metric, "values": "1 _ _ _ _ _ _ _ _"}],
             "alert_rule_test": checks([(5, False), (6, False), (7, True)])},
            {"name": "duplicate sources cannot imply health", "interval": "1s",
             "input_series": [{"series": metric, "values": "1 1 1"},
                              {"series": metric.replace('job="state"', 'job="duplicate"'), "values": "1 1 stale"}],
             "alert_rule_test": checks([(0, False), (1, True), (2, False)])},
            {"name": "invalid duplicate cannot be ignored", "interval": "1s",
             "input_series": [{"series": metric, "values": "1 1 1"},
                              {"series": metric.replace('job="state"', 'job="duplicate"'), "values": "2 2 stale"}],
             "alert_rule_test": checks([(0, False), (1, True), (2, False)])},
            {"name": "invalid value cannot resolve a firing alert", "interval": "1s",
             "input_series": [{"series": metric, "values": "0 0 2 2 1"}],
             "alert_rule_test": checks([(0, False), (1, True), (2, True), (3, True), (4, False)])},
            {"name": "scrape down, missing and recovery", "interval": "1s",
             "input_series": [
                 {"series": 'up{job="api",instance="cp1:6443"}', "values": "1 0 0 stale _ 1"},
                 {"series": 'up{job="kubelet",instance="w1:10250",metrics_path="/metrics"}', "values": "1 1 1 1 1 1"},
                 {"series": 'up{job="kubelet",instance="w1:10250",metrics_path="/metrics/cadvisor"}', "values": "1 1 1 1 1 1"},
                 {"series": 'up{job="kubelet",instance="w1:10250",metrics_path="/metrics/probes"}', "values": "1 1 1 1 1 1"}],
             "alert_rule_test": [{"eval_time": str(t)+"s", "alertname": "AtlasScrapeTargetOrTelemetryMissing",
                 "exp_alerts": ([{"exp_labels": {"job": "api", "instance": "cp1:6443", "risk_id": "R-OBS-01",
                                                   "severity": "warning"},
                                "exp_annotations": {"summary": summary, "description": description}}] if firing else [])}
                 for t, firing in [(0, False), (1, False), (2, True), (3, True), (4, True), (5, False)]]},
            {"name": "kubelet scrape paths sharing job and instance are independently monitored", "interval": "1s",
             "input_series": [
                 {"series": 'up{job="api",instance="cp1:6443"}', "values": "1 1 1 1 1 1"},
                 {"series": 'up{job="kubelet",instance="w1:10250",metrics_path="/metrics"}', "values": "1 0 0 stale _ 1"},
                 {"series": 'up{job="kubelet",instance="w1:10250",metrics_path="/metrics/cadvisor"}', "values": "1 1 1 1 1 1"},
                 {"series": 'up{job="kubelet",instance="w1:10250",metrics_path="/metrics/probes"}', "values": "1 1 1 1 1 1"}],
             "alert_rule_test": [{"eval_time": str(t)+"s", "alertname": "AtlasScrapeTargetOrTelemetryMissing",
                 "exp_alerts": ([{"exp_labels": dict(next(
                     rule["labels"] for rule in kubelet_scrape["spec"]["groups"][0]["rules"]
                     if 'metrics_path="/metrics"' in rule["expr"])),
                                 "exp_annotations": {"summary": summary, "description": description}}]
                                if firing else [])}
                 for t, firing in [(0, False), (1, False), (2, True), (3, True), (4, True), (5, False)]]},
            {"name": "control-plane endpoint down, missing and recovery", "interval": "1s",
             "input_series": [{"series": 'up{job="apiserver",instance="cp1:6443"}', "values": "1 0 0 stale _ 1"}],
             "alert_rule_test": [{"eval_time": str(t)+"s", "alertname": "AtlasControlPlaneTargetOrTelemetryMissing",
                 "exp_alerts": ([{"exp_labels": {"job": "apiserver", "instance": "cp1:6443", "risk_id": "R-CP-01",
                                                   "severity": "warning"},
                                 "exp_annotations": {"summary": "控制面端点不可用", "description": "检查授权采集目标。"}}] if firing else [])}
                 for t, firing in [(0, False), (1, False), (2, True), (3, True), (4, True), (5, False)]]},
            {"name": "etcd member leader loss, missing, recovery", "interval": "1s",
             "input_series": [
                 {"series": 'etcd_server_has_leader{job="etcd",instance="cp1:2381"}', "values": "1 0 0 stale _ 1"},
                 {"series": 'etcd_server_has_leader{job="etcd-e2e",instance="cp1:2381"}', "values": "1 1 1 1 1 1"},
                 {"series": 'etcd_server_has_leader{job="etcd-e2e",instance="cp2:2381"}', "values": "1 1 1 1 1 1"},
                 {"series": 'etcd_server_has_leader{job="etcd-e2e",instance="cp3:2381"}', "values": "1 1 1 1 1 1"}],
             "alert_rule_test": [{"eval_time": str(t)+"s", "alertname": "AtlasEtcdMemberLeaderOrTelemetryMissing",
                 "exp_alerts": ([{"exp_labels": {"job": "etcd", "instance": "cp1:2381", "risk_id": "R-ETCD-01",
                                                   "severity": "warning"},
                                 "exp_annotations": {"summary": summary, "description": description}}] if firing else [])}
                 for t, firing in [(0, False), (1, False), (2, True), (3, True), (4, True), (5, False)]]},
            {"name": "etcd registered majority survives one member and alerts below majority", "interval": "1s",
             "input_series": [
                 {"series": 'etcd_server_has_leader{job="etcd",instance="cp1:2381"}', "values": "1 1 1 1 1"},
                 {"series": 'etcd_server_has_leader{job="etcd-e2e",instance="cp1:2381"}', "values": "1 1 1 1 1"},
                 {"series": 'etcd_server_has_leader{job="etcd-e2e",instance="cp2:2381"}', "values": "1 0 0 0 1"},
                 {"series": 'etcd_server_has_leader{job="etcd-e2e",instance="cp3:2381"}', "values": "1 1 0 0 1"}],
             "alert_rule_test": [{"eval_time": str(t) + "s", "alertname": "AtlasEtcdQuorumOrTelemetryMissing",
                 "exp_alerts": ([{"exp_labels": {"risk_id": "R-ETCD-01", "severity": "critical",
                                                   "registered_members": "3", "quorum_required": "2"},
                                  "exp_annotations": {"summary": "etcd 多数派成员不足",
                                                      "description": "检查成员状态、对等网络和磁盘延迟。",
                                                      "observed_value": "1"}}] if firing else [])}
                 for t, firing in [(0, False), (1, False), (2, False), (3, True), (4, False)]]},
            {"name": "deployment degraded, missing, recovered and scaled to zero", "interval": "1s",
             "input_series": [
                 {"series": 'kube_deployment_spec_replicas{namespace="prod",deployment="web",job="ksm"}',
                  "values": "3 3 3 3 3 3 0"},
                 {"series": 'kube_deployment_status_replicas_available{namespace="prod",deployment="web",job="ksm"}',
                  "values": "3 2 2 stale _ 3 0"}],
             "alert_rule_test": [{"eval_time": str(t)+"s", "alertname": "AtlasDeploymentAvailabilityOrTelemetryMissing",
                 "exp_alerts": ([{"exp_labels": {"namespace": "prod", "deployment": "web", "risk_id": "R-WORK-01",
                                                   "severity": "warning"},
                                 "exp_annotations": {"summary": summary, "description": description}}] if firing else [])}
                 for t, firing in [(0, False), (1, False), (2, True), (3, True), (4, True), (5, False), (6, False)]]},
            {"name": "deployment duplicate and invalid samples cannot resolve", "interval": "1s",
             "input_series": [
                 {"series": 'kube_deployment_spec_replicas{namespace="prod",deployment="web",job="ksm"}', "values": "2 2 2 2 2"},
                 {"series": 'kube_deployment_status_replicas_available{namespace="prod",deployment="web",job="ksm"}', "values": "1 1 NaN 2 2"},
                 {"series": 'kube_deployment_status_replicas_available{namespace="prod",deployment="web",job="duplicate"}', "values": "_ _ _ 2 stale"}],
             "alert_rule_test": [{"eval_time": str(t)+"s", "alertname": "AtlasDeploymentAvailabilityOrTelemetryMissing",
                 "exp_alerts": ([{"exp_labels": {"namespace": "prod", "deployment": "web", "risk_id": "R-WORK-01",
                                                   "severity": "warning"},
                                 "exp_annotations": {"summary": summary, "description": description}}] if firing else [])}
                 for t, firing in [(0, False), (1, True), (2, True), (3, True), (4, False)]]},
            {"name": "deployment metrics from different sources are not healthy", "interval": "1s",
             "input_series": [
                 {"series": 'kube_deployment_spec_replicas{namespace="prod",deployment="web",job="one"}', "values": "1 1 1"},
                 {"series": 'kube_deployment_status_replicas_available{namespace="prod",deployment="web",job="other"}', "values": "1 1 1"}],
             "alert_rule_test": [{"eval_time": str(t)+"s", "alertname": "AtlasDeploymentAvailabilityOrTelemetryMissing",
                 "exp_alerts": ([{"exp_labels": {"namespace": "prod", "deployment": "web", "risk_id": "R-WORK-01",
                                                   "severity": "warning"},
                                 "exp_annotations": {"summary": summary, "description": description}}] if firing else [])}
                 for t, firing in [(0, False), (1, True), (2, True)]]},
            {"name": "deployment progress deadline exceeded fires and resolves", "interval": "1s",
             "input_series": [{"series": 'kube_deployment_status_condition{namespace="prod",deployment="web",condition="Progressing",status="false",reason="ProgressDeadlineExceeded",job="ksm"}',
                               "values": "0 1 1 0 stale"}],
             "alert_rule_test": rollout_checks([(0, False), (1, False), (2, True), (3, False), (4, False)])},
            {"name": "deployment rollout wrong reason and stale condition do not alert", "interval": "1s",
             "input_series": [
                 {"series": 'kube_deployment_status_condition{namespace="prod",deployment="web",condition="Progressing",status="false",reason="ReplicaSetUpdated",job="ksm"}',
                  "values": "1 1 1 1"},
                 {"series": 'kube_deployment_status_condition{namespace="prod",deployment="web",condition="Progressing",status="false",reason="ProgressDeadlineExceeded",job="ksm"}',
                  "values": "0 1 stale _"}],
             "alert_rule_test": rollout_checks([(0, False), (1, False), (2, False), (3, False)])},
            {"name": "PVC low free, missing telemetry, recovery", "interval": "1s",
             "input_series": [
                 {"series": 'kubelet_volume_stats_capacity_bytes{namespace="prod",persistentvolumeclaim="data",instance="node1"}', "values": "100 100 100 100 100 100"},
                 {"series": 'kubelet_volume_stats_available_bytes{namespace="prod",persistentvolumeclaim="data",instance="node1"}', "values": "50 10 10 stale _ 50"}],
            "alert_rule_test": [{"eval_time": str(t)+"s", "alertname": "AtlasPVCFreeSpaceOrTelemetryMissing",
                "exp_alerts": ([{"exp_labels": {"namespace": "prod", "persistentvolumeclaim": "data", "risk_id": "R-STOR-01",
                                                  "severity": "warning"},
                                "exp_annotations": {"summary": summary, "description": description,
                                                    "persistentvolumeclaim": "data"}}] if firing else [])}
                 for t, firing in [(0, False), (1, False), (2, True), (3, True), (4, True), (5, False)]]},
            {"name": "PVC multi-node missing pair and zero capacity", "interval": "1s",
             "input_series": [
                 {"series": 'kubelet_volume_stats_capacity_bytes{namespace="prod",persistentvolumeclaim="data",instance="one"}', "values": "100 100 100 100"},
                 {"series": 'kubelet_volume_stats_available_bytes{namespace="prod",persistentvolumeclaim="data",instance="one"}', "values": "50 50 50 50"},
                 {"series": 'kubelet_volume_stats_capacity_bytes{namespace="prod",persistentvolumeclaim="data",instance="two"}', "values": "100 100 0 0"},
                 {"series": 'kubelet_volume_stats_available_bytes{namespace="prod",persistentvolumeclaim="data",instance="two"}', "values": "50 stale 50 50"}],
            "alert_rule_test": [{"eval_time": str(t)+"s", "alertname": "AtlasPVCFreeSpaceOrTelemetryMissing",
                "exp_alerts": ([{"exp_labels": {"namespace": "prod", "persistentvolumeclaim": "data", "risk_id": "R-STOR-01",
                                                  "severity": "warning"},
                                "exp_annotations": {"summary": summary, "description": description,
                                                    "persistentvolumeclaim": "data"}}] if firing else [])}
                 for t, firing in [(0, False), (1, False), (2, True), (3, True)]]},
            {"name": "PVC invalid and impossible values cannot resolve", "interval": "1s",
             "input_series": [
                 {"series": 'kubelet_volume_stats_capacity_bytes{namespace="prod",persistentvolumeclaim="data",instance="one"}', "values": "100 100 100 100 100 100"},
                 {"series": 'kubelet_volume_stats_available_bytes{namespace="prod",persistentvolumeclaim="data",instance="one"}', "values": "10 10 NaN 200 +Inf 50"}],
            "alert_rule_test": [{"eval_time": str(t)+"s", "alertname": "AtlasPVCFreeSpaceOrTelemetryMissing",
                "exp_alerts": ([{"exp_labels": {"namespace": "prod", "persistentvolumeclaim": "data", "risk_id": "R-STOR-01",
                                                  "severity": "warning"},
                                "exp_annotations": {"summary": summary, "description": description,
                                                    "persistentvolumeclaim": "data"}}] if firing else [])}
                 for t, firing in [(0, False), (1, True), (2, True), (3, True), (4, True), (5, False)]]},
        ],
    }
    for kind, desired, available, alert in (
        ("statefulset", "kube_statefulset_replicas", "kube_statefulset_status_replicas_available", "AtlasStatefulSetAvailabilityOrTelemetryMissing"),
        ("daemonset", "kube_daemonset_status_desired_number_scheduled", "kube_daemonset_status_number_available", "AtlasDaemonSetAvailabilityOrTelemetryMissing"),
    ):
        label = '{namespace="prod",' + kind + '="app",job="ksm"}'
        expectation = {"exp_labels": {"namespace": "prod", kind: "app", "risk_id": "R-WORK-01", "severity": "warning"},
                       "exp_annotations": {"summary": summary, "description": description}}
        fixture["tests"].append({"name": kind + " shortage, missing, recovery and zero", "interval": "1s",
            "input_series": [{"series": desired + label, "values": "2 2 2 2 2 2 0"},
                             {"series": available + label, "values": "2 1 1 stale _ 2 0"}],
            "alert_rule_test": [{"eval_time": str(t) + "s", "alertname": alert,
                                 "exp_alerts": [expectation] if firing else []}
                                for t, firing in ((0, False), (1, False), (2, True), (3, True),
                                                  (4, True), (5, False), (6, False))]})
    longhorn_expected = {"exp_labels": {"namespace": "prod", "persistentvolumeclaim": "data",
                                       "risk_id": "R-STOR-03", "severity": "warning"},
                         "exp_annotations": {"summary": summary, "description": description,
                                             "persistentvolumeclaim": "data"}}
    def longhorn_series(state, values, source="one"):
        return {"series": 'longhorn_volume_robustness{pvc_namespace="prod",pvc="data",volume="vol",node="node1",job="' + source + '",state="' + state + '"}', "values": values}
    def longhorn_checks(times):
        return [{"eval_time": str(t) + "s", "alertname": "AtlasLonghornVolumeRobustnessOrTelemetryMissing",
                 "exp_alerts": [longhorn_expected] if firing else []} for t, firing in times]
    fixture["tests"].extend([
        {"name": "Longhorn degraded, missing and recovery", "interval": "1s", "input_series": [
            longhorn_series("healthy", "1 0 0 stale _ 1"), longhorn_series("degraded", "0 1 1 stale _ 0"),
            longhorn_series("faulted", "0 0 0 stale _ 0"), longhorn_series("unknown", "0 0 0 stale _ 0")],
         "alert_rule_test": longhorn_checks([(0, False), (1, False), (2, True), (3, True), (4, True), (5, False)])},
        {"name": "Longhorn duplicate, invalid and mismatched source", "interval": "1s", "input_series": [
            longhorn_series("healthy", "1 1 1 1 1 1 1"), longhorn_series("degraded", "0 0 NaN NaN 0 0 0"),
            longhorn_series("degraded", "_ 0 stale _ _ _ _", "other"),
            longhorn_series("faulted", "0 0 0 stale stale 0 0"),
            longhorn_series("faulted", "_ _ _ 0 0 stale _", "other"),
            longhorn_series("unknown", "0 0 0 0 0 0 0")],
         "alert_rule_test": longhorn_checks([(0, False), (1, False), (2, False), (3, True), (4, False), (5, False)])},
        {"name": "Longhorn stale data", "interval": "1s", "input_series": [
            longhorn_series("healthy", "1 _ _ _ _ _ _ _"), longhorn_series("degraded", "0 _ _ _ _ _ _ _"),
            longhorn_series("faulted", "0 _ _ _ _ _ _ _"), longhorn_series("unknown", "0 _ _ _ _ _ _ _")],
         "alert_rule_test": longhorn_checks([(5, False), (6, False), (7, True)])},
        {"name": "Longhorn incompatible old numeric metric", "interval": "1s", "input_series": [
            {"series": 'longhorn_volume_robustness{pvc_namespace="prod",pvc="data",volume="vol",node="node1",job="one"}', "values": "1 1 1"}],
         "alert_rule_test": longhorn_checks([(0, False), (1, True), (2, True)])},
        {"name": "Longhorn unrecognized state fails closed", "interval": "1s", "input_series": [
            longhorn_series("healthy", "1 1 1 1"), longhorn_series("degraded", "0 0 0 0"),
            longhorn_series("faulted", "0 0 0 0"), longhorn_series("unknown", "0 0 0 0"),
            longhorn_series("new-state", "_ 1 1 stale")],
         "alert_rule_test": longhorn_checks([(0, False), (1, False), (2, True), (3, False)])},
    ])
    workload_expectations = {
        "AtlasPodPendingTooLong": {"exp_labels": {"namespace": "prod", "pod": "api-0",
            "risk_id": "R-WORK-02", "severity": "warning"},
            "exp_annotations": {"summary": "Pod pending", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasPodContainerStartFailure": {"exp_labels": {"namespace": "prod", "pod": "api-0",
            "container": "api", "reason": "ImagePullBackOff", "risk_id": "R-WORK-02", "severity": "warning"},
            "exp_annotations": {"summary": "Container start failure", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasPodContainerRestartBurst": {"exp_labels": {"namespace": "prod", "pod": "api-0",
            "container": "api", "risk_id": "R-WORK-03", "severity": "warning"},
            "exp_annotations": {"summary": "Restart burst", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasPodOOMKilled": {"exp_labels": {"namespace": "prod", "pod": "api-0", "container": "api",
            "reason": "OOMKilled", "risk_id": "R-WORK-03", "severity": "warning"},
            "exp_annotations": {"summary": "Container OOMKilled", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasJobFailure": {"exp_labels": {"namespace": "prod", "job_name": "daily", "risk_id": "R-WORK-05",
            "severity": "warning"},
            "exp_annotations": {"summary": "Job failed", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasDeploymentRolloutStalled": {"exp_labels": {"namespace": "prod", "deployment": "web",
            "risk_id": "R-WORK-04", "severity": "warning"},
            "exp_annotations": {"summary": "Deployment rollout stalled", "description": "Check rollout progress."}},
        "AtlasCertificateExpiringSoon": {"exp_labels": {"namespace": "prod", "name": "web-tls",
            "risk_id": "R-EDGE-04", "severity": "warning"},
            "exp_annotations": {"summary": "Certificate expiring", "description": "Renew certificate.",
                                 "observed_value": "{{ printf \"%.0f\" $value }} seconds remaining"}},
        "AtlasCertificateNotReady": {"exp_labels": {"namespace": "prod", "name": "web-tls",
            "risk_id": "R-EDGE-04", "severity": "warning"},
            "exp_annotations": {"summary": "Certificate not ready", "description": "Check issuer."}},
        "AtlasCertificateTelemetryMissing": {"exp_labels": {"namespace": "prod", "name": "web-tls",
            "risk_id": "R-EDGE-04", "severity": "warning"},
            "exp_annotations": {"summary": "Certificate metrics missing", "description": "Check scraping."}},
        "AtlasJobRunningTooLong": {"exp_labels": {"namespace": "prod", "job_name": "daily",
            "risk_id": "R-WORK-05", "severity": "warning"},
            "exp_annotations": {"summary": "Job timeout", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasCronJobScheduleOverdue": {"exp_labels": {"namespace": "prod", "cronjob": "daily",
            "risk_id": "R-WORK-05", "severity": "warning"},
            "exp_annotations": {"summary": "CronJob schedule overdue", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasHPAReplicaLimitOrScalingLimited": {"exp_labels": {"namespace": "prod", "horizontalpodautoscaler": "api",
            "risk_id": "R-WORK-06", "severity": "warning"},
            "exp_annotations": {"summary": "HPA at replica limit", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasHPAMetricsUnavailable": {"exp_labels": {"namespace": "prod", "horizontalpodautoscaler": "api",
            "risk_id": "R-WORK-06", "severity": "warning"},
            "exp_annotations": {"summary": "HPA scaling metrics unavailable", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasKEDAScalerErrors": {"exp_labels": {"exported_namespace": "prod", "scaledObject": "api", "scaler": "prometheusScaler",
            "risk_id": "R-WORK-06", "severity": "warning"},
            "exp_annotations": {"summary": "KEDA scaler query errors", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasKEDAScaledObjectErrors": {"exp_labels": {"exported_namespace": "prod", "scaledObject": "api",
            "risk_id": "R-WORK-06", "severity": "warning"},
            "exp_annotations": {"summary": "KEDA scaling errors", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasKEDAEmptyUpstreamResponses": {"exp_labels": {"exported_namespace": "prod", "scaledObject": "api", "scaler": "prometheusScaler",
            "risk_id": "R-WORK-06", "severity": "warning"},
            "exp_annotations": {"summary": "KEDA scaler upstream metrics response is empty", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasKEDAScalerMetricsSlow": {"exp_labels": {"exported_namespace": "prod", "scaledObject": "api", "scaler": "prometheusScaler",
            "risk_id": "R-WORK-06", "severity": "warning"},
            "exp_annotations": {"summary": "KEDA scaler metrics are slow", "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
        "AtlasKEDAOperatorTargetMissing": {"exp_labels": {"namespace": "keda",
            "risk_id": "R-WORK-06", "severity": "warning"},
            "exp_annotations": {"summary": "KEDA Operator metrics target is unavailable or missing",
                                 "description": "Check workload events, resource limits, scheduling and scaling configuration."}},
    }
    def workload_checks(alert, times):
        expected_alert = workload_expectations[alert]
        return [{"eval_time": str(t) + "s", "alertname": alert,
                 "exp_alerts": [expected_alert] if firing else []} for t, firing in times]

    csi_expectations = {
        item["alert"]: {
            "exp_labels": {"attacher": "driver.longhorn.io", "node": "w1", "volumeattachment": "va-1",
                           "risk_id": "R-STOR-02", "severity": "warning"},
            "exp_annotations": {"summary": item["annotations"]["summary"],
                                "description": item["annotations"]["description"]},
        }
        for item in csi_group["rules"]
    }

    def csi_checks(alert, times):
        expected_alert = csi_expectations[alert]
        return [{"eval_time": str(t) + "s", "alertname": alert,
                 "exp_alerts": [expected_alert] if firing else []} for t, firing in times]

    fixture["tests"].extend([
        {"name": "CSI attachment false status fires and recovers", "interval": "1s", "input_series": [
            {"series": 'kube_volumeattachment_info{attacher="driver.longhorn.io",node="w1",volumeattachment="va-1"}',
             "values": "1 1 1 1 1 1 1"},
            {"series": 'kube_volumeattachment_status_attached{volumeattachment="va-1"}',
             "values": "1 0 0 0 0 1 1"}],
         "alert_rule_test": csi_checks("AtlasCSIVolumeAttachmentNotReady",
             [(0, False), (1, False), (2, False), (3, True), (4, True), (5, False)])},
        {"name": "CSI attachment status telemetry missing fires and recovers", "interval": "1s", "input_series": [
            {"series": 'kube_volumeattachment_info{attacher="driver.longhorn.io",node="w1",volumeattachment="va-1"}',
             "values": "1 1 1 1 1 1 1"},
            {"series": 'kube_volumeattachment_status_attached{volumeattachment="va-1"}',
             "values": "1 1 stale _ _ _ 1"}],
         "alert_rule_test": csi_checks("AtlasCSIVolumeAttachmentTelemetryMissing",
             [(0, False), (1, False), (2, False), (3, False), (4, True), (5, True), (6, False)])},
        {"name": "registered certificate expiry threshold and recovery", "interval": "1s",
         "input_series": [{"series": 'certmanager_certificate_expiration_timestamp_seconds{namespace="cert-manager",exported_namespace="prod",name="web-tls",issuer_name="ca"}',
                           "values": "2678400 2592001 2592001 2678403 stale"}],
         "alert_rule_test": certificate_checks("AtlasCertificateExpiringSoon", {}, "Certificate expiring",
             "Renew certificate.", [(0, False), (1, False), (2, True), (3, False), (4, False)],
             "2591999 seconds remaining")},
        {"name": "registered certificate not ready and recovery", "interval": "1s",
         "input_series": [{"series": 'certmanager_certificate_ready_status{namespace="cert-manager",exported_namespace="prod",name="web-tls",condition="False",issuer_name="ca"}',
                           "values": "0 1 1 0 stale"}],
         "alert_rule_test": certificate_checks("AtlasCertificateNotReady", {}, "Certificate not ready",
             "Check issuer.", [(0, False), (1, False), (2, True), (3, False), (4, False)])},
        {"name": "registered certificate missing all fresh metrics", "interval": "1s",
         "input_series": [{"series": 'certmanager_certificate_expiration_timestamp_seconds{namespace="cert-manager",exported_namespace="prod",name="web-tls",issuer_name="ca"}',
                           "values": "2678400 stale _ _"}],
         "alert_rule_test": certificate_checks("AtlasCertificateTelemetryMissing", {}, "Certificate metrics missing",
             "Check scraping.", [(0, False), (1, False), (2, True), (3, True)])},
        {"name": "certificate without expiration only reports not-ready", "interval": "1s",
         "input_series": [
             {"series": 'certmanager_certificate_expiration_timestamp_seconds{namespace="cert-manager",exported_namespace="prod",name="web-tls",issuer_name="missing"}',
              "values": "0 0 0 0"},
             {"series": 'certmanager_certificate_ready_status{namespace="cert-manager",exported_namespace="prod",name="web-tls",condition="False",issuer_name="missing"}',
              "values": "0 1 1 1"}],
         "alert_rule_test": [
             *certificate_checks("AtlasCertificateExpiringSoon", {}, "Certificate expiring", "Renew certificate.",
                                [(0, False), (1, False), (2, False), (3, False)]),
             *certificate_checks("AtlasCertificateNotReady", {}, "Certificate not ready", "Check issuer.",
                                [(0, False), (1, False), (2, True), (3, True)])]},
        {"name": "Pending pod threshold, fire and recovery", "interval": "1s", "input_series": [
            {"series": 'kube_pod_status_phase{namespace="prod",pod="api-0",phase="Pending"}', "values": "0 1 1 1 stale"}],
         "alert_rule_test": workload_checks("AtlasPodPendingTooLong", [(0, False), (1, False), (2, True), (3, True), (4, False)])},
        {"name": "container pull failure, fire and recovery", "interval": "1s", "input_series": [
            {"series": 'kube_pod_container_status_waiting_reason{namespace="prod",pod="api-0",container="api",reason="ImagePullBackOff"}',
             "values": "0 1 1 1 stale"}],
         "alert_rule_test": workload_checks("AtlasPodContainerStartFailure", [(0, False), (1, False), (2, True), (3, True), (4, False)])},
        {"name": "restart burst threshold and stale recovery", "interval": "1s", "input_series": [
            {"series": 'kube_pod_container_status_restarts_total{namespace="prod",pod="api-0",container="api"}',
             "values": "0 1 3 4 stale"}],
         "alert_rule_test": workload_checks("AtlasPodContainerRestartBurst", [(0, False), (1, False), (2, False), (3, True), (4, False)])},
        {"name": "OOMKilled requires a recent restart and recovers", "interval": "1s", "input_series": [
            {"series": 'kube_pod_container_status_last_terminated_reason{namespace="prod",pod="api-0",container="api",reason="OOMKilled"}',
             "values": "0 0 1 1 stale"},
            {"series": 'kube_pod_container_status_restarts_total{namespace="prod",pod="api-0",container="api"}',
             "values": "0 0 1 1 stale"}],
         "alert_rule_test": workload_checks("AtlasPodOOMKilled", [(0, False), (1, False), (2, False), (3, True), (4, False)])},
        {"name": "historical OOM without restart does not alert", "interval": "1s", "input_series": [
            {"series": 'kube_pod_container_status_last_terminated_reason{namespace="prod",pod="api-0",container="api",reason="OOMKilled"}',
             "values": "0 1 1 1"},
            {"series": 'kube_pod_container_status_restarts_total{namespace="prod",pod="api-0",container="api"}',
             "values": "0 0 0 0"}],
         "alert_rule_test": workload_checks("AtlasPodOOMKilled", [(0, False), (1, False), (2, False), (3, False)])},
        {"name": "failed Job alert and recovery", "interval": "1s", "input_series": [
            {"series": 'kube_job_status_failed{namespace="prod",job_name="daily"}', "values": "0 1 1 stale"}],
         "alert_rule_test": workload_checks("AtlasJobFailure", [(0, False), (1, False), (2, True), (3, False)])},
        {"name": "active Job timeout, success window and stale recovery", "interval": "1s", "input_series": [
            {"series": 'kube_job_status_active{namespace="prod",job_name="daily"}', "values": "1+0x62 0+0x3"},
            {"series": 'kube_job_status_start_time{namespace="prod",job_name="daily"}', "values": "0+0x65"}],
         "alert_rule_test": workload_checks("AtlasJobRunningTooLong", [(0, False), (60, False), (61, False), (62, True), (63, False)])},
        {"name": "missing Job start time fails closed", "interval": "1s", "input_series": [
            {"series": 'kube_job_status_active{namespace="prod",job_name="daily"}', "values": "1+0x65"},
            {"series": 'kube_job_status_start_time{namespace="prod",job_name="daily"}', "values": "0 0 stale"}],
         "alert_rule_test": workload_checks("AtlasJobRunningTooLong", [(60, False), (62, False)])},
        {"name": "CronJob next schedule overdue, suspended and recovery", "interval": "1s", "input_series": [
            {"series": 'kube_cronjob_next_schedule_time{namespace="prod",cronjob="daily"}', "values": "0+0x65"},
            {"series": 'kube_cronjob_spec_suspend{namespace="prod",cronjob="daily"}', "values": "0+0x62 1 0+0x2"}],
         "alert_rule_test": workload_checks("AtlasCronJobScheduleOverdue", [(0, False), (60, False), (61, False), (62, True), (63, False), (64, False), (65, True)])},
        {"name": "missing CronJob schedule sample fails closed", "interval": "1s", "input_series": [
            {"series": 'kube_cronjob_next_schedule_time{namespace="prod",cronjob="daily"}', "values": "0 0 stale"},
            {"series": 'kube_cronjob_spec_suspend{namespace="prod",cronjob="daily"}', "values": "0+0x65"}],
         "alert_rule_test": workload_checks("AtlasCronJobScheduleOverdue", [(60, False), (62, False)])},
        {"name": "HPA at max replica count and recovery", "interval": "1s", "input_series": [
            {"series": 'kube_horizontalpodautoscaler_status_current_replicas{namespace="prod",horizontalpodautoscaler="api"}',
             "values": "2 4 4 3 stale"},
            {"series": 'kube_horizontalpodautoscaler_spec_max_replicas{namespace="prod",horizontalpodautoscaler="api"}',
             "values": "4 4 4 4 stale"}],
         "alert_rule_test": workload_checks("AtlasHPAReplicaLimitOrScalingLimited", [(0, False), (1, False), (2, True), (3, False), (4, False)])},
        {"name": "HPA ScalingLimited condition", "interval": "1s", "input_series": [
            {"series": 'kube_horizontalpodautoscaler_status_current_replicas{namespace="prod",horizontalpodautoscaler="api"}',
             "values": "2 2 2 2"},
            {"series": 'kube_horizontalpodautoscaler_spec_max_replicas{namespace="prod",horizontalpodautoscaler="api"}',
             "values": "4 4 4 4"},
            {"series": 'kube_horizontalpodautoscaler_status_condition{namespace="prod",horizontalpodautoscaler="api",condition="ScalingLimited",status="true"}',
             "values": "0 1 1 0"}],
         "alert_rule_test": workload_checks("AtlasHPAReplicaLimitOrScalingLimited", [(0, False), (1, False), (2, True), (3, False)])},
        {"name": "HPA ScalingActive false indicates metrics unavailable", "interval": "1s", "input_series": [
            {"series": 'kube_horizontalpodautoscaler_status_condition{namespace="prod",horizontalpodautoscaler="api",condition="ScalingActive",status="false"}',
             "values": "0 1 1 0 stale"}],
         "alert_rule_test": workload_checks("AtlasHPAMetricsUnavailable", [(0, False), (1, False), (2, True), (3, False), (4, False)])},
        {"name": "KEDA scaler error counter increase and stale recovery", "interval": "30s", "input_series": [
            {"series": 'keda_scaler_detail_errors_total{namespace="keda",exported_namespace="prod",scaledObject="api",scaler="prometheusScaler",pod="operator-0"}',
             "values": "0 10 100 200 stale"},
            {"series": 'keda_scaler_detail_errors_total{namespace="keda",exported_namespace="prod",scaledObject="api",scaler="prometheusScaler",pod="operator-1"}',
             "values": "0 10 100 200 stale"}],
         "alert_rule_test": workload_checks("AtlasKEDAScalerErrors", [(0, False), (30, False), (60, True), (90, True), (120, False)])},
        {"name": "KEDA ScaledObject reconciliation error counter increase and recovery", "interval": "30s", "input_series": [
            {"series": 'keda_scaled_object_errors_total{namespace="keda",exported_namespace="prod",scaledObject="api",pod="operator-0"}',
             "values": "0 10 100 200 stale"},
            {"series": 'keda_scaled_object_errors_total{namespace="keda",exported_namespace="prod",scaledObject="api",pod="operator-1"}',
             "values": "0 10 100 200 stale"}],
         "alert_rule_test": workload_checks("AtlasKEDAScaledObjectErrors", [(0, False), (30, False), (60, True), (90, True), (120, False)])},
        {"name": "KEDA empty upstream counter increase and stale recovery", "interval": "30s", "input_series": [
            {"series": 'keda_scaler_empty_upstream_responses_total{namespace="keda",exported_namespace="prod",scaledObject="api",scaler="prometheusScaler",pod="operator-0"}',
             "values": "0 10 100 200 stale"},
            {"series": 'keda_scaler_empty_upstream_responses_total{namespace="keda",exported_namespace="prod",scaledObject="api",scaler="prometheusScaler",pod="operator-1"}',
             "values": "0 10 100 200 stale"}],
         "alert_rule_test": workload_checks("AtlasKEDAEmptyUpstreamResponses", [(0, False), (30, False), (60, True), (90, True), (120, False)])},
        {"name": "KEDA scaler latency threshold and recovery", "interval": "1s", "input_series": [
            {"series": 'keda_scaler_metrics_latency_seconds{namespace="keda",exported_namespace="prod",scaledObject="api",scaler="prometheusScaler"}',
             "values": "0 6 6 2 stale"}],
         "alert_rule_test": workload_checks("AtlasKEDAScalerMetricsSlow", [(0, False), (1, False), (2, True), (3, False), (4, False)])},
        {"name": "KEDA operator scrape failure and recovery", "interval": "1s", "input_series": [
            {"series": 'up{namespace="keda",service="keda-operator",pod="operator-0"}',
             "values": "1 0 0 1 1"}],
         "alert_rule_test": workload_checks("AtlasKEDAOperatorTargetMissing",
             [(0, False), (1, False), (2, True), (3, False), (4, False)])},
        {"name": "KEDA operator target disappearance after bounded absence window, then recovery",
         "interval": "30s", "input_series": [
            {"series": 'up{namespace="keda",service="keda-operator",pod="operator-0"}',
             "values": "1 stale _ _ 1"}],
         "alert_rule_test": workload_checks("AtlasKEDAOperatorTargetMissing",
             [(0, False), (30, False), (60, False), (90, True), (120, False)])},
    ])
    try:
        temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="atlas-rules-", dir=temp_root) as directory:
            root = Path(directory).resolve()
            (root / "rules.json").write_text(json.dumps(rule["spec"]))
            (root / "test.json").write_text(json.dumps(fixture))
            result = subprocess.run([
                "docker", "run", "--rm", "--pull=never", "--name", name,
                "--label", "atlas-kubekit.e2e=" + suffix, "--network", "none",
                "-v", str(root) + ":/tmp/atlas-rules:ro", "--entrypoint=/bin/promtool",
                "prom/prometheus:v3.14.0", "test", "rules", "/tmp/atlas-rules/test.json",
            ], capture_output=True, text=True, timeout=45)
            print(result.stdout, end="")
            if result.returncode:
                print(result.stderr, file=sys.stderr, end="")
                raise RuntimeError("promtool rule verification failed")
    finally:
        cleanup_owned(suffix, network, (name,))
        print("Owned temporary container cleaned up.")


if __name__ == "__main__":
    main()
