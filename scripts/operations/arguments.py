# SPDX-License-Identifier: MIT
"""Localized argument contracts, independent of command execution."""
import argparse
import sys
from pathlib import Path
from scripts.lib.i18n import translate, available_locales
from .capabilities.registry import CAPABILITIES
from .capabilities.monitoring_node_network import OPTIONS as NETWORK_RULE_OPTIONS


def parser_for(lang):
    tr = lambda k: translate(lang, k)

    class Help(argparse.HelpFormatter):
        def _format_usage(self, usage, actions, groups, prefix):
            return super()._format_usage(usage, actions, groups, tr("ops.cli.usage") + ": ")

    class Parser(argparse.ArgumentParser):
        def error(self, message):
            self.print_usage(sys.stderr)
            self.exit(2, tr("ops.error.arguments") + "\n")

    parser = Parser(description=tr("ops.cli.description"), add_help=False, formatter_class=Help)
    parser._optionals.title = tr("ops.cli.options")
    parser.add_argument("-h", "--help", action="help", help=tr("ops.cli.help"))
    parser.add_argument(
        "--lang", default=lang, choices=list(available_locales()), help=tr("ops.cli.lang")
    )
    group = parser.add_mutually_exclusive_group()
    for name in (
        "report",
        "security-check",
        "backup-plan",
        "backup-create",
        "backup-verify",
        "restore-plan",
        "config-template",
        "config-check",
        "config-import",
        "config-export",
        "config-drift",
        "change-plan",
        "change-continue",
        "change-apply",
        "change-status",
        "lock-status",
        "lock-recover",
        "restore-execute",
        "restore-rehearse",
        "diagnostics",
        "materials-prepare",
        "materials-verify",
        "materials-import",
        "protection-status",
        "monitoring-inspect",
        "monitoring-coverage",
        "monitoring-observer-status",
        "monitoring-observer-install",
        "monitoring-observer-remove",
    ):
        group.add_argument(
            "--" + name,
            dest="action",
            action="store_const",
            const=name,
            help=tr("ops.cli." + name.replace("-", "_")),
        )
    parser.add_argument("--target", help=tr("ops.cli.target"))
    parser.add_argument("--prometheus-url", help=tr("ops.monitoring.url"))
    parser.add_argument("--observer-state-directory", help=tr("ops.monitoring.observer_directory"))
    parser.add_argument("--observer-name", help=tr("ops.monitoring.observer_name"))
    parser.add_argument("--observer-url", help=tr("ops.monitoring.observer_url"))
    parser.add_argument("--observer-ca-file", help=tr("ops.monitoring.observer_ca_file"))
    parser.add_argument("--observer-dns-server", help=tr("ops.monitoring.observer_dns_server"))
    parser.add_argument("--observer-dns-port", type=int, default=53, help=tr("ops.monitoring.observer_dns_port"))
    parser.add_argument("--observer-dns-name", help=tr("ops.monitoring.observer_dns_name"))
    parser.add_argument("--observer-dns-expected", action="append", help=tr("ops.monitoring.observer_dns_expected"))
    parser.add_argument("--observer-wecom-endpoint-file", help=tr("ops.monitoring.observer_wecom_file"))
    parser.add_argument("--observer-interval", type=int, default=30, help=tr("ops.monitoring.observer_interval"))
    parser.add_argument("--observer-fail-after", type=int, default=2, help=tr("ops.monitoring.observer_fail_after"))
    parser.add_argument("--observer-recover-after", type=int, default=2, help=tr("ops.monitoring.observer_recover_after"))
    parser.add_argument("--monitoring-max-age", type=int, default=120, help=tr("ops.monitoring.max_age"))
    parser.add_argument("--monitoring-scope-file", help=tr("ops.monitoring.coverage_scope_file"))
    parser.add_argument("--monitoring-namespace", default="monitoring", help=tr("ops.monitoring.namespace"))
    parser.add_argument("--monitoring-prometheus-name", help=tr("ops.monitoring.prometheus_name"))
    for field in ("alertmanager-name", "alertmanager-version", "smtp-host", "email-sender",
                  "email-recipient", "email-username", "email-secret", "email-secret-key"):
        parser.add_argument("--monitoring-" + field, help=tr("ops.monitoring." + field.replace('-', '_')))
    parser.add_argument("--monitoring-email-receiver", default="atlas-email-relay",
                        help=tr("ops.monitoring.email_receiver"))
    for field in ("relay-image", "relay-auth-secret", "relay-auth-key", "robot-receiver",
                  "robot-secret", "robot-endpoint-key", "robot-signing-key"):
        parser.add_argument("--monitoring-" + field, help=tr("ops.monitoring." + field.replace('-', '_')))
    parser.add_argument("--monitoring-robot-kind", choices=("wecom", "dingtalk", "feishu"),
                        help=tr("ops.monitoring.robot_kind"))
    parser.add_argument("--monitoring-smtp-port", type=int, help=tr("ops.monitoring.smtp_port"))
    parser.add_argument("--monitoring-loki-endpoint", help=tr("ops.monitoring.loki_endpoint"))
    parser.add_argument("--monitoring-log-before-seconds", type=int, default=300,
                        help=tr("ops.monitoring.log_before_seconds"))
    parser.add_argument("--monitoring-log-after-seconds", type=int, default=60,
                        help=tr("ops.monitoring.log_after_seconds"))
    parser.add_argument("--monitoring-log-max-lines", type=int, default=20,
                        help=tr("ops.monitoring.log_max_lines"))
    parser.add_argument("--monitoring-log-max-message-bytes", type=int, default=8192,
                        help=tr("ops.monitoring.log_max_message_bytes"))
    parser.add_argument("--monitoring-grafana-explore-url", help=tr("ops.monitoring.grafana_explore_url"))
    parser.add_argument("--monitoring-loki-datasource-uid", help=tr("ops.monitoring.loki_datasource_uid"))
    parser.add_argument("--monitoring-grafana-org-id", type=int, default=1,
                        help=tr("ops.monitoring.grafana_org_id"))
    parser.add_argument("--monitoring-route-matcher", action="append", default=None,
                        help=tr("ops.monitoring.route_matcher"))
    parser.add_argument("--monitoring-group-by", action="append", default=None,
                        help=tr("ops.monitoring.group_by"))
    parser.add_argument("--monitoring-group-wait", default="30s", help=tr("ops.monitoring.group_wait"))
    parser.add_argument("--monitoring-group-interval", default="5m",
                        help=tr("ops.monitoring.group_interval"))
    parser.add_argument("--monitoring-repeat-interval", default="12h",
                        help=tr("ops.monitoring.repeat_interval"))
    parser.add_argument("--monitoring-alertmanager-service",
                        help=tr("ops.monitoring.alertmanager_service"))
    parser.add_argument("--monitoring-alertmanager-service-port", default="http-web",
                        help=tr("ops.monitoring.alertmanager_service_port"))
    parser.add_argument("--monitoring-silence-action", choices=("create", "expire"), default="create",
                        help=tr("ops.monitoring.silence_action"))
    parser.add_argument("--monitoring-silence-matcher", action="append", default=None,
                        help=tr("ops.monitoring.silence_matcher"))
    parser.add_argument("--monitoring-silence-duration", default="2h",
                        help=tr("ops.monitoring.silence_duration"))
    parser.add_argument("--monitoring-silence-reason", help=tr("ops.monitoring.silence_reason"))
    parser.add_argument("--monitoring-silence-id", help=tr("ops.monitoring.silence_id"))
    parser.add_argument("--monitoring-inhibition-enabled", action="store_true",
                        help=tr("ops.monitoring.inhibition_enabled"))
    parser.add_argument("--monitoring-inhibition-source-severity", choices=("critical",), default="critical",
                        help=tr("ops.monitoring.inhibition_source_severity"))
    parser.add_argument("--monitoring-inhibition-target-severity", choices=("warning", "info"), default="warning",
                        help=tr("ops.monitoring.inhibition_target_severity"))
    parser.add_argument("--monitoring-inhibition-equal-label", action="append", default=None,
                        help=tr("ops.monitoring.inhibition_equal_label"))
    parser.add_argument("--monitoring-longhorn-namespace", default="longhorn-system", help=tr("ops.monitoring.longhorn_namespace"))
    parser.add_argument("--monitoring-longhorn-disk-used-percent", type=int, default=70,
                        help=tr("ops.monitoring.longhorn_disk_used_percent"))
    parser.add_argument("--monitoring-release", help=tr("ops.monitoring.release"))
    parser.add_argument("--monitoring-target", action="append", help=tr("ops.monitoring.target"))
    parser.add_argument("--monitoring-network-ruleset", default="default", help=tr("ops.monitoring.node_network_ruleset"))
    for _, option, default in NETWORK_RULE_OPTIONS:
        parser.add_argument("--monitoring-" + option.replace("_", "-"), type=int, default=default,
                            help=tr("ops.monitoring." + option).replace("%", "%%"))
    parser.add_argument("--monitoring-network-target", action="append",
                        help=tr("ops.monitoring.network_target"))
    parser.add_argument("--monitoring-resource-kind",
                        choices=tuple(CAPABILITIES["monitoring-resource-remove"].RESOURCE_KINDS),
                        help=tr("ops.monitoring.resource_kind"))
    parser.add_argument("--monitoring-resource-namespace", help=tr("ops.monitoring.resource_namespace"))
    parser.add_argument("--monitoring-resource-name", help=tr("ops.monitoring.resource_name"))
    parser.add_argument("--monitoring-expected-target", action="append",
                        help=tr("ops.monitoring.expected_target"))
    parser.add_argument("--monitoring-risk-target", action="append",
                        help=tr("ops.monitoring.expected_risk_target"))
    parser.add_argument("--monitoring-observer-risk", action="append",
                        help=tr("ops.monitoring.expected_observer_risk"))
    parser.add_argument("--monitoring-deployment", action="append", help=tr("ops.monitoring.deployment"))
    parser.add_argument("--monitoring-controller", action="append", help=tr("ops.monitoring.controller"))
    parser.add_argument("--monitoring-pvc", action="append", help=tr("ops.monitoring.pvc"))
    parser.add_argument("--monitoring-free-percent", type=int, help=tr("ops.monitoring.free_percent"))
    parser.add_argument("--monitoring-dns-error-percent", type=int, default=5, help=tr("ops.monitoring.dns_error_percent"))
    parser.add_argument("--monitoring-dns-window-seconds", type=int, default=300, help=tr("ops.monitoring.dns_window_seconds"))
    parser.add_argument("--monitoring-dns-latency-ms", type=int, default=100, help=tr("ops.monitoring.dns_latency_ms"))
    parser.add_argument("--monitoring-dns-min-requests", type=int, default=100,
                        help=tr("ops.monitoring.dns_min_requests"))
    parser.add_argument("--monitoring-dns-deployment", default="kube-system/coredns",
                        help=tr("ops.monitoring.dns_deployment"))
    parser.add_argument("--monitoring-dns-min-replicas", type=int, default=2,
                        help=tr("ops.monitoring.dns_min_replicas"))
    parser.add_argument("--monitoring-certificate", action="append", help=tr("ops.monitoring.certificate"))
    parser.add_argument("--monitoring-certificate-expiry-days", type=int, default=30,
                        help=tr("ops.monitoring.certificate_expiry_days"))
    parser.add_argument("--monitoring-lb-capacity-percent", type=int, default=80, help=tr("ops.monitoring.lb_capacity_percent"))
    parser.add_argument("--monitoring-lb-connection-error-threshold", type=int, default=5,
                        help=tr("ops.monitoring.lb_connection_error_threshold"))
    parser.add_argument("--monitoring-lb-drift-threshold", type=int, default=4,
                        help=tr("ops.monitoring.lb_drift_threshold"))
    parser.add_argument("--monitoring-restart-threshold", type=int, default=3,
                        help=tr("ops.monitoring.restart_threshold"))
    parser.add_argument("--monitoring-restart-window-seconds", type=int, default=300,
                        help=tr("ops.monitoring.restart_window_seconds"))
    parser.add_argument("--monitoring-job-timeout-seconds", type=int, default=3600,
                        help=tr("ops.monitoring.job_timeout_seconds"))
    parser.add_argument("--monitoring-cronjob-overdue-seconds", type=int, default=900,
                        help=tr("ops.monitoring.cronjob_overdue_seconds"))
    parser.add_argument("--monitoring-keda-latency-seconds", type=int, default=5,
                        help=tr("ops.monitoring.keda_latency_seconds"))
    parser.add_argument("--monitoring-keda-namespace", default="",
                        help=tr("ops.monitoring.keda_namespace"))
    parser.add_argument("--monitoring-hold-seconds", type=int, help=tr("ops.monitoring.hold_seconds"))
    parser.add_argument("--monitoring-node-cpu-percent", type=int, default=90,
                        help=tr("ops.monitoring.node_cpu_percent"))
    parser.add_argument("--monitoring-node-memory-percent", type=int, default=90,
                        help=tr("ops.monitoring.node_memory_percent"))
    parser.add_argument("--monitoring-node-filesystem-percent", type=int, default=85,
                        help=tr("ops.monitoring.node_filesystem_percent"))
    parser.add_argument("--monitoring-node-inode-percent", type=int, default=85,
                        help=tr("ops.monitoring.node_inode_percent"))
    parser.add_argument("--monitoring-cilium-ipam-percent", type=int, default=80,
                        help=tr("ops.monitoring.cilium_ipam_percent"))
    parser.add_argument("--monitoring-cilium-bpf-pressure-percent", type=int, default=80,
                        help=tr("ops.monitoring.cilium_bpf_pressure_percent"))
    parser.add_argument("--monitoring-severity", choices=("critical", "warning", "info"), default="warning", help=tr("ops.monitoring.severity"))
    parser.add_argument("--monitoring-notification-language", choices=("en", "zh_CN", "zh_TW"),
                        default="zh_CN", help=tr("ops.monitoring.notification_language"))
    parser.add_argument("--sudo", action="store_true", help=tr("ops.cli.sudo"))
    parser.add_argument("--ssh-identity", help=tr("ops.cli.ssh_identity"))
    parser.add_argument(
        "--output-dir",
        default=str(Path.home() / ".local/state/atlas-kubekit/operations"),
        help=tr("ops.cli.output"),
    )
    parser.add_argument(
        "--scope",
        choices=("configuration", "control-plane"),
        default="configuration",
        help=tr("ops.cli.scope"),
    )
    parser.add_argument("--recipient", help=tr("ops.cli.recipient"))
    parser.add_argument("--backup", help=tr("ops.cli.backup"))
    parser.add_argument("--identity", help=tr("ops.cli.identity"))
    parser.add_argument("--restore-target", help=tr("ops.cli.restore_target"))
    parser.add_argument("--confirm", help=tr("ops.cli.confirm"))
    parser.add_argument("--config", help=tr("ops.cli.config"))
    parser.add_argument("--profile-name", help=tr("ops.cli.profile_name"))
    parser.add_argument(
        "--state-dir",
        default=str(Path.home() / ".local/state/atlas-kubekit/configurations"),
        help=tr("ops.cli.state_dir"),
    )
    parser.add_argument("--export-file", help=tr("ops.cli.export_file"))
    parser.add_argument("--expected-revision", help=tr("ops.cli.expected_revision"))
    for name in (
        "ssh-config",
        "control-target",
        "plan-file",
        "plan-id",
        "node",
        "lock-owner",
        "resource-version",
        "source-fingerprint",
        "stats-cidr",
        "retained-user",
        "to-version",
    ):
        parser.add_argument("--" + name, help=tr("ops.cli." + name.replace("-", "_")))
    parser.add_argument(
        "--kubeconfig", default="/etc/kubernetes/admin.conf", help=tr("ops.cli.kubeconfig")
    )
    parser.add_argument(
        "--lock-namespace", default="kube-system", help=tr("ops.cli.lock_namespace")
    )
    parser.add_argument("--operation", choices=list(CAPABILITIES), help=tr("ops.cli.operation"))
    parser.add_argument(
        "--maintenance-action",
        choices=("cordon", "drain", "uncordon"),
        help=tr("ops.cli.maintenance_action"),
    )
    parser.add_argument(
        "--delete-emptydir", action="store_true", help=tr("ops.cli.delete_emptydir")
    )
    parser.add_argument(
        "--bump-revision", type=int, default=1000000000, help=tr("ops.cli.bump_revision")
    )
    for name in (
        "node-address",
        "node-interface",
        "data-device",
        "replacement-name",
        "materials",
        "material-id",
        "registry",
        "ca-file",
        "chart",
        "evidence-file",
    ):
        parser.add_argument("--" + name, help=tr("ops.cli." + name.replace("-", "_")))
    return parser
