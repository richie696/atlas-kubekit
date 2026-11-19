# SPDX-License-Identifier: MIT
"""Executable IDs and default scope classification from the production risk matrix."""

RISK_CATALOG = tuple(
    [(f"R-BIZ-{number:02d}", "extension") for number in range(1, 5)]
    + [("R-EDGE-01", "required")]
    + [(f"R-EDGE-{number:02d}", "in-use") for number in range(2, 5)]
    + [(f"R-LB-{number:02d}", "in-use") for number in range(1, 5)]
    + [(f"R-CP-{number:02d}", "required") for number in range(1, 5)]
    + [(f"R-ETCD-{number:02d}", "required") for number in range(1, 5)]
    + [(f"R-NODE-{number:02d}", "required") for number in range(1, 9)]
    + [("R-NET-01", "required"), ("R-NET-02", "in-use")]
    + [(f"R-NET-{number:02d}", "required") for number in range(3, 6)]
    + [(f"R-WORK-{number:02d}", "required") for number in range(1, 5)]
    + [("R-WORK-05", "in-use"), ("R-WORK-06", "in-use")]
    + [(f"R-CAP-{number:02d}", "required") for number in range(1, 3)]
    + [(f"R-STOR-{number:02d}", "in-use") for number in range(1, 5)]
    + [(f"R-DATA-{number:02d}", "extension") for number in range(1, 5)]
    + [(f"R-PROT-{number:02d}", "in-use") for number in range(1, 4)]
    + [("R-SEC-01", "required"), ("R-SEC-02", "in-use"), ("R-SEC-03", "required")]
    + [(f"R-OBS-{number:02d}", "required") for number in range(1, 6)]
    + [("R-INFRA-01", "in-use"), ("R-INFRA-02", "extension"), ("R-INFRA-03", "extension")]
)

RISK_IDS = frozenset(risk_id for risk_id, _ in RISK_CATALOG)

# Exact kube-prometheus-stack alert names documented by our production risk
# matrix. These establish rule presence for part of a risk only; the coverage
# report still requires independent data and delivery evidence.
CHART_DEFAULT_ALERT_RISKS = {
    "KubeAPIDown": "R-CP-01",
    "KubeAPIInstanceUnreachable": "R-CP-01",
    "KubeAPIErrorBudgetBurn": "R-CP-02",
    "KubeAPITerminatedRequests": "R-CP-02",
    "KubeSchedulerDown": "R-CP-03",
    "KubeSchedulerInstanceUnreachable": "R-CP-03",
    "KubeControllerManagerDown": "R-CP-04",
    "KubeControllerManagerInstanceUnreachable": "R-CP-04",
    "etcdMembersDown": "R-ETCD-01",
    "etcdInsufficientMembers": "R-ETCD-01",
    "etcdNoLeader": "R-ETCD-01",
    "etcdHighNumberOfLeaderChanges": "R-ETCD-01",
    "etcdGRPCRequestsSlow": "R-ETCD-02",
    "etcdHighFsyncDurations": "R-ETCD-02",
    "etcdHighCommitDurations": "R-ETCD-02",
    "etcdDatabaseQuotaLowSpace": "R-ETCD-03",
    "etcdExcessiveDatabaseGrowth": "R-ETCD-03",
    "etcdDatabaseHighFragmentationRatio": "R-ETCD-03",
    "etcdHighNumberOfFailedGRPCRequests": "R-ETCD-04",
    "etcdHighNumberOfFailedProposals": "R-ETCD-04",
    "etcdMemberCommunicationSlow": "R-ETCD-04",
    "KubeNodeNotReady": "R-NODE-01",
    "KubeNodeUnreachable": "R-NODE-01",
    "KubeNodeReadinessFlapping": "R-NODE-01",
    "NodeCPUHighUsage": "R-NODE-02",
    "NodeSystemSaturation": "R-NODE-02",
    "CPUThrottlingHigh": "R-NODE-02",
    "NodeMemoryHighUtilization": "R-NODE-03",
    "NodeMemoryMajorPagesFaults": "R-NODE-03",
    "KubeNodePressure": "R-NODE-03",
    "NodeFilesystemSpaceFillingUp": "R-NODE-04",
    "NodeFilesystemFilesFillingUp": "R-NODE-04",
    "NodeFilesystemAlmostOutOfSpace": "R-NODE-04",
    "NodeFilesystemAlmostOutOfFiles": "R-NODE-04",
    "NodeDiskIOSaturation": "R-NODE-05",
    "NodeRAIDDegraded": "R-NODE-05",
    "NodeRAIDDiskFailure": "R-NODE-05",
    "NodeSystemdServiceCrashlooping": "R-NODE-06",
    "NodeSystemdServiceFailed": "R-NODE-06",
    "KubeletDown": "R-NODE-06",
    "KubeletInstanceUnreachable": "R-NODE-06",
    "KubeletPlegDurationHigh": "R-NODE-06",
    "KubeletPodStartUpLatencyHigh": "R-NODE-06",
    "KubeletTooManyPods": "R-NODE-06",
    "NodeFileDescriptorLimit": "R-NODE-07",
    "NodeHighNumberConntrackEntriesUsed": "R-NODE-07",
    "NodeClockNotSynchronising": "R-NODE-08",
    "NodeClockSkewDetected": "R-NODE-08",
    "KubeDeploymentReplicasMismatch": "R-WORK-01",
    "KubeStatefulSetReplicasMismatch": "R-WORK-01",
    "KubeDaemonSetNotScheduled": "R-WORK-01",
    "KubeDaemonSetMisScheduled": "R-WORK-01",
    "KubeDeploymentGenerationMismatch": "R-WORK-04",
    "KubeDeploymentRolloutStuck": "R-WORK-04",
    "KubeStatefulSetGenerationMismatch": "R-WORK-04",
    "KubeStatefulSetUpdateNotRolledOut": "R-WORK-04",
    "KubeDaemonSetRolloutStuck": "R-WORK-04",
    "KubeCPUOvercommit": "R-CAP-01",
    "KubeMemoryOvercommit": "R-CAP-01",
    "KubeCPUQuotaOvercommit": "R-CAP-01",
    "KubeMemoryQuotaOvercommit": "R-CAP-01",
    "KubeQuotaAlmostFull": "R-CAP-01",
    "KubeQuotaExceeded": "R-CAP-01",
    "KubeQuotaFullyUsed": "R-CAP-01",
    "KubePdbNotEnoughHealthyPods": "R-CAP-02",
    "KubeClientCertificateExpiration": "R-SEC-01",
    "KubeletClientCertificateExpiration": "R-SEC-01",
    "KubeletServerCertificateExpiration": "R-SEC-01",
    "PrometheusNotIngestingSamples": "R-OBS-02",
    "PrometheusRuleFailures": "R-OBS-02",
    "PrometheusTSDBCompactionsFailing": "R-OBS-02",
    "PrometheusTSDBReloadsFailing": "R-OBS-02",
    "PrometheusBadConfig": "R-OBS-02",
    "AlertmanagerClusterDown": "R-OBS-02",
    "AlertmanagerFailedReload": "R-OBS-02",
    "AlertmanagerFailedToSendAlerts": "R-OBS-03",
    "AlertmanagerClusterFailedToSendAlerts": "R-OBS-03",
    "PrometheusErrorSendingAlertsToAnyAlertmanager": "R-OBS-03",
    "PrometheusErrorSendingAlertsToSomeAlertmanagers": "R-OBS-03",
    "PrometheusNotificationQueueRunningFull": "R-OBS-03",
    "PrometheusHighQueryLoad": "R-OBS-05",
    "PrometheusTargetLimitHit": "R-OBS-05",
    "PrometheusScrapeSampleLimitHit": "R-OBS-05",
    "PrometheusScrapeBodySizeLimitHit": "R-OBS-05",
    "NodeNetworkReceiveErrs": "R-NET-05",
    "NodeNetworkTransmitErrs": "R-NET-05",
    "NodeNetworkInterfaceFlapping": "R-NET-05",
    "NodeBondingDegraded": "R-NET-05",
}

# Alerts installed by Atlas values alongside kube-prometheus-stack defaults.
ATLAS_DEFAULT_ALERT_RISKS = {
    "AtlasKubeletServiceUnavailable": "R-NODE-06",
    "AtlasKubeletSystemdTelemetryMissing": "R-NODE-06",
    "AtlasContainerdServiceUnavailable": "R-NODE-06",
    "AtlasContainerdSystemdTelemetryMissing": "R-NODE-06",
}

KNOWN_ALERT_RISKS = {**CHART_DEFAULT_ALERT_RISKS, **ATLAS_DEFAULT_ALERT_RISKS}
