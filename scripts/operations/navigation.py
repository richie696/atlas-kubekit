# SPDX-License-Identifier: MIT
"""Navigation groups collect input; domain commands validate and execute."""
import argparse
from .commands.changes import CAPABILITIES
from .changes.plans import MONITORING_RULE_CAPABILITIES
from .capabilities import monitoring_node_network

GROUPS = (
    ("materials", ("materials-prepare", "materials-verify", "materials-import")),
    ("inspect", ("report", "security-check", "diagnostics", "monitoring-inspect", "monitoring-coverage", "monitoring-observer-status",
                  "monitoring-observer-install", "monitoring-observer-remove")),
    (
        "configuration",
        ("config-template", "config-check", "config-import", "config-export", "config-drift"),
    ),
    (
        "changes",
        (
            "change-plan",
            "change-continue",
            "change-apply",
            "change-status",
            "lock-status",
            "lock-recover",
        ),
    ),
    (
        "backup",
        (
            "backup-plan",
            "backup-create",
            "backup-verify",
            "restore-plan",
            "restore-execute",
            "restore-rehearse",
            "protection-status",
        ),
    ),
)


def ask(tr, key, current=None):
    value = input(tr("ops.prompt.value", tr(key), "" if current is None else str(current))).strip()
    return None if value == "-" else value or current


def select(tr, choices, label):
    print("\n" + tr(label))
    for index, (_, key) in enumerate(choices, 1):
        print(str(index) + ") " + tr(key))
    print("0) " + tr("common.menu.exit"))
    answer = input(tr("common.menu.choice") + ": ").strip()
    if answer == "0":
        return None
    if not answer.isdigit() or not 1 <= int(answer) <= len(choices):
        print(tr("ops.error.choice"))
        return False
    return choices[int(answer) - 1][0]


def connection(args, tr):
    for field in ("control_target", "kubeconfig", "ssh_config", "ssh_identity"):
        setattr(args, field, ask(tr, "ops.cli." + field, getattr(args, field)))


def configure(args, tr):
    action = args.action
    if action.startswith("materials-"):
        if action == "materials-prepare":
            args.config = ask(tr, "ops.cli.config", args.config)
        else:
            args.materials = ask(tr, "ops.cli.materials", args.materials)
            args.material_id = ask(tr, "ops.cli.material_id", args.material_id)
            if action == "materials-import":
                args.confirm = ask(tr, "ops.cli.confirm")
        return True
    if action in ("diagnostics", "protection-status", "monitoring-inspect", "monitoring-coverage") or action.startswith(("change-", "lock-")):
        connection(args, tr)
    if action in ("monitoring-inspect", "monitoring-coverage"):
        args.prometheus_url = ask(tr, "ops.monitoring.url", args.prometheus_url)
    if action == "monitoring-coverage":
        args.monitoring_scope_file = ask(tr, "ops.monitoring.coverage_scope_file", args.monitoring_scope_file)
        args.observer_state_directory = ask(tr, "ops.monitoring.observer_directory",
                                            args.observer_state_directory)
    if action == "monitoring-observer-status":
        args.observer_state_directory = ask(tr, "ops.monitoring.observer_directory", args.observer_state_directory)
    if action == "monitoring-observer-install":
        args.observer_name = ask(tr, "ops.monitoring.observer_name", args.observer_name)
        target = select(tr, (("http", "ops.monitoring.observer_http"),
                             ("dns", "ops.monitoring.observer_dns")), "ops.monitoring.observer_target")
        if not target:
            return False
        if target == "http":
            args.observer_url = ask(tr, "ops.monitoring.observer_url", args.observer_url)
            args.observer_ca_file = ask(tr, "ops.monitoring.observer_ca_file", args.observer_ca_file)
            args.observer_dns_server = args.observer_dns_name = None
            args.observer_dns_expected = None
        else:
            args.observer_url = None
            args.observer_ca_file = None
            args.observer_dns_server = ask(tr, "ops.monitoring.observer_dns_server", args.observer_dns_server)
            args.observer_dns_name = ask(tr, "ops.monitoring.observer_dns_name", args.observer_dns_name)
            expected = ask(tr, "ops.monitoring.observer_dns_expected_list", None)
            args.observer_dns_expected = expected.split() if expected else None
            try:
                args.observer_dns_port = int(ask(tr, "ops.monitoring.observer_dns_port", args.observer_dns_port))
            except (TypeError, ValueError):
                print(tr("ops.error.choice"))
                return False
        args.observer_state_directory = ask(tr, "ops.monitoring.observer_directory", args.observer_state_directory)
        args.observer_wecom_endpoint_file = ask(tr, "ops.monitoring.observer_wecom_file", args.observer_wecom_endpoint_file)
        try:
            args.observer_interval = int(ask(tr, "ops.monitoring.observer_interval", args.observer_interval))
            args.observer_fail_after = int(ask(tr, "ops.monitoring.observer_fail_after", args.observer_fail_after))
            args.observer_recover_after = int(ask(tr, "ops.monitoring.observer_recover_after", args.observer_recover_after))
        except (TypeError, ValueError):
            print(tr("ops.error.choice"))
            return False
    if action == "monitoring-observer-remove":
        args.observer_name = ask(tr, "ops.monitoring.observer_name", args.observer_name)
        args.confirm = ask(tr, "ops.monitoring.observer_remove_confirm")
    if action.startswith("config-"):
        if action == "config-export":
            args.config = None
            args.profile_name = ask(tr, "ops.cli.profile_name", args.profile_name)
        elif action != "config-template":
            args.config = ask(tr, "ops.cli.config", args.config)
        if action in ("config-template", "config-export"):
            args.export_file = ask(tr, "ops.cli.export_file", args.export_file)
        if action == "config-import":
            args.expected_revision = ask(tr, "ops.cli.expected_revision", args.expected_revision)
        if action == "config-drift":
            args.sudo = True
            args.ssh_config = ask(tr, "ops.cli.ssh_config", args.ssh_config)
            args.ssh_identity = ask(tr, "ops.cli.ssh_identity", args.ssh_identity)
    elif action == "change-plan":
        args.operation = select(
            tr,
            tuple((name, "ops.operation." + name.replace("-", "_")) for name in CAPABILITIES),
            "ops.cli.operation",
        )
        if not args.operation:
            return False
        args.config = ask(tr, "ops.cli.config", args.config)
        if args.operation == "monitoring-resource-remove":
            kinds = CAPABILITIES[args.operation].RESOURCE_KINDS
            args.monitoring_resource_kind = select(
                tr, tuple((name, "ops.monitoring.resource_kind." + name) for name in kinds),
                "ops.monitoring.resource_kind")
            if not args.monitoring_resource_kind:
                return False
            args.monitoring_resource_namespace = ask(
                tr, "ops.monitoring.resource_namespace", args.monitoring_resource_namespace)
            args.monitoring_resource_name = ask(
                tr, "ops.monitoring.resource_name", args.monitoring_resource_name)
        elif args.operation in ("lb-stats", "monitoring-lb"):
            args.stats_cidr = ask(tr, "ops.cli.stats_cidr", args.stats_cidr or "")
        elif args.operation == "security-finalize":
            print(tr("ops.security.handoff_guide"))
            args.retained_user = ask(tr, "ops.cli.retained_user", args.retained_user)
        elif args.operation == "registry-configure":
            args.registry = ask(tr, "ops.cli.registry", args.registry)
            args.ca_file = ask(tr, "ops.cli.ca_file", args.ca_file)
        elif args.operation == "node-maintenance":
            args.node = ask(tr, "ops.cli.node", args.node)
            args.maintenance_action = select(
                tr,
                tuple(
                    (name, "ops.maintenance." + name) for name in ("cordon", "drain", "uncordon")
                ),
                "ops.cli.maintenance_action",
            )
            args.delete_emptydir = False
        elif args.operation == "cluster-upgrade":
            args.to_version = ask(tr, "ops.cli.to_version", args.to_version)
            args.delete_emptydir = False
        elif args.operation == "component-upgrade":
            args.to_version = ask(tr, "ops.cli.to_version", args.to_version)
            args.chart = ask(tr, "ops.cli.chart", args.chart)
        elif args.operation == "protection-register":
            args.evidence_file = ask(tr, "ops.cli.evidence_file", args.evidence_file)
        elif args.operation == "monitoring-email-diagnostics":
            for field in ("namespace", "release", "alertmanager_name", "alertmanager_version",
                          "relay_image", "relay_auth_secret", "relay_auth_key"):
                name = "monitoring_" + field
                setattr(args, name, ask(tr, "ops.monitoring." + field, getattr(args, name)))
            args.monitoring_email_receiver = ask(tr, "ops.monitoring.email_receiver", args.monitoring_email_receiver)
            for field in ("smtp_host", "email_sender", "email_recipient", "email_username",
                          "email_secret", "email_secret_key"):
                name = "monitoring_" + field
                setattr(args, name, ask(tr, "ops.monitoring." + field, getattr(args, name)))
            try:
                args.monitoring_smtp_port = int(ask(tr, "ops.monitoring.smtp_port", args.monitoring_smtp_port))
            except (TypeError, ValueError):
                print(tr("ops.error.choice"))
                return False
            args.monitoring_severity = select(tr, tuple((item, "ops.monitoring.severity." + item)
                for item in ("critical", "warning", "info")), "ops.monitoring.severity")
            if not args.monitoring_severity:
                return False
            args.monitoring_notification_language = select(tr, tuple(
                (item, "ops.monitoring.notification_language." + item.casefold())
                for item in ("zh_CN", "zh_TW", "en")), "ops.monitoring.notification_language")
            if not args.monitoring_notification_language:
                return False
            args.monitoring_loki_endpoint = ask(tr, "ops.monitoring.loki_endpoint", args.monitoring_loki_endpoint or "")
            if args.monitoring_loki_endpoint:
                for field, default in (("monitoring_log_before_seconds", 300), ("monitoring_log_after_seconds", 60),
                                       ("monitoring_log_max_lines", 20), ("monitoring_log_max_message_bytes", 8192)):
                    raw = ask(tr, "ops.monitoring." + field.removeprefix("monitoring_"),
                              getattr(args, field, default))
                    try:
                        setattr(args, field, int(raw))
                    except (TypeError, ValueError):
                        print(tr("ops.error.choice"))
                        return False
                args.monitoring_grafana_explore_url = ask(
                    tr, "ops.monitoring.grafana_explore_url", args.monitoring_grafana_explore_url or "")
                if args.monitoring_grafana_explore_url:
                    args.monitoring_loki_datasource_uid = ask(
                        tr, "ops.monitoring.loki_datasource_uid", args.monitoring_loki_datasource_uid or "")
                    try:
                        args.monitoring_grafana_org_id = int(ask(
                            tr, "ops.monitoring.grafana_org_id", args.monitoring_grafana_org_id))
                    except (TypeError, ValueError):
                        print(tr("ops.error.choice"))
                        return False
        elif args.operation == "monitoring-email":
            for field in ("namespace", "release", "alertmanager_name", "alertmanager_version", "smtp_host",
                          "email_sender", "email_recipient", "email_username", "email_secret", "email_secret_key"):
                name = "monitoring_" + field
                setattr(args, name, ask(tr, "ops.monitoring." + field, getattr(args, name)))
            try:
                args.monitoring_smtp_port = int(ask(tr, "ops.monitoring.smtp_port", args.monitoring_smtp_port))
            except (TypeError, ValueError):
                print(tr("ops.error.choice"))
                return False
            args.monitoring_severity = select(tr, tuple((item, "ops.monitoring.severity." + item)
                for item in ("critical", "warning", "info")), "ops.monitoring.severity")
            if not args.monitoring_severity:
                return False
            args.monitoring_notification_language = select(tr, tuple(
                (item, "ops.monitoring.notification_language." + item.casefold())
                for item in ("zh_CN", "zh_TW", "en")),
                "ops.monitoring.notification_language")
            if not args.monitoring_notification_language:
                return False
        elif args.operation == "monitoring-robot":
            for field in ("namespace", "release", "alertmanager_name", "alertmanager_version", "relay_image",
                          "relay_auth_secret", "relay_auth_key", "robot_receiver", "robot_secret",
                          "robot_endpoint_key", "robot_signing_key"):
                name = "monitoring_" + field
                setattr(args, name, ask(tr, "ops.monitoring." + field, getattr(args, name)))
            args.monitoring_robot_kind = select(tr, tuple(
                (kind, "ops.monitoring.robot_kind_" + kind) for kind in ("wecom", "dingtalk", "feishu")),
                "ops.monitoring.robot_kind")
            if not args.monitoring_robot_kind:
                return False
            args.monitoring_notification_language = select(tr, tuple(
                (item, "ops.monitoring.notification_language." + item.casefold())
                for item in ("zh_CN", "zh_TW", "en")),
                "ops.monitoring.notification_language")
            if not args.monitoring_notification_language:
                return False
            args.monitoring_severity = select(tr, tuple((item, "ops.monitoring.severity." + item)
                for item in ("critical", "warning", "info")), "ops.monitoring.severity")
            if not args.monitoring_severity:
                return False
            args.monitoring_loki_endpoint = ask(tr, "ops.monitoring.loki_endpoint", args.monitoring_loki_endpoint or "")
            if args.monitoring_loki_endpoint:
                for field, default in (("monitoring_log_before_seconds", 300),
                                       ("monitoring_log_after_seconds", 60),
                                       ("monitoring_log_max_lines", 20),
                                       ("monitoring_log_max_message_bytes", 8192)):
                    raw = ask(tr, "ops.monitoring." + field.removeprefix("monitoring_"),
                              getattr(args, field, default))
                    try:
                        setattr(args, field, int(raw))
                    except (TypeError, ValueError):
                        print(tr("ops.error.choice"))
                        return False
                args.monitoring_grafana_explore_url = ask(
                    tr, "ops.monitoring.grafana_explore_url", args.monitoring_grafana_explore_url or "")
                if args.monitoring_grafana_explore_url:
                    args.monitoring_loki_datasource_uid = ask(
                        tr, "ops.monitoring.loki_datasource_uid", args.monitoring_loki_datasource_uid or "")
                    raw_org_id = ask(tr, "ops.monitoring.grafana_org_id", args.monitoring_grafana_org_id)
                    try:
                        args.monitoring_grafana_org_id = int(raw_org_id)
                    except (TypeError, ValueError):
                        print(tr("ops.error.choice"))
                        return False
        elif args.operation == "monitoring-systemd-collection":
            return True
        elif args.operation == "monitoring-systemd-remove":
            args.node = ask(tr, "ops.cli.node", args.node)
            return bool(args.node)
        elif args.operation in MONITORING_RULE_CAPABILITIES or args.operation in ("monitoring-longhorn-collection", "monitoring-lb-collection", "monitoring-lb", "monitoring-cilium-collection", "monitoring-network-probes"):
            args.monitoring_namespace = ask(tr, "ops.monitoring.namespace", args.monitoring_namespace)
            args.monitoring_prometheus_name = ask(tr, "ops.monitoring.prometheus_name", args.monitoring_prometheus_name)
            args.monitoring_release = ask(tr, "ops.monitoring.release", args.monitoring_release)
            if args.operation == "monitoring-longhorn-collection":
                args.monitoring_longhorn_namespace = ask(tr, "ops.monitoring.longhorn_namespace", args.monitoring_longhorn_namespace)
                return True
            if args.operation == "monitoring-lb-collection":
                return True
            if args.operation == "monitoring-cilium-collection":
                try:
                    args.monitoring_hold_seconds = int(ask(
                        tr, "ops.monitoring.hold_seconds", args.monitoring_hold_seconds))
                except (TypeError, ValueError):
                    print(tr("ops.error.choice"))
                    return False
                return True
            if args.operation == "monitoring-network-probes":
                targets = []
                while len(targets) < 32:
                    value = ask(tr, "ops.monitoring.network_target", None)
                    if not value:
                        break
                    targets.append(value)
                args.monitoring_network_target = targets
                return bool(targets)
            if args.operation in ("monitoring-scrape-rules", "monitoring-etcd-rules", "monitoring-control-plane-rules", "monitoring-dns-rules"):
                args.monitoring_target = ask(tr, "ops.monitoring.targets", "").split()
            if args.operation == monitoring_node_network.NAME:
                args.monitoring_network_ruleset = ask(tr, "ops.monitoring.node_network_ruleset",
                                                      args.monitoring_network_ruleset)
                targets = ask(tr, "ops.monitoring.node_network_targets", "")
                if not targets:
                    return False
                args.monitoring_target = targets.split()
                try:
                    for _, option, _ in monitoring_node_network.OPTIONS:
                        field = "monitoring_" + option
                        setattr(args, field, int(ask(tr, "ops.monitoring." + option, getattr(args, field))))
                except (TypeError, ValueError):
                    print(tr("ops.error.choice"))
                    return False
                if args.monitoring_hold_seconds is None:
                    args.monitoring_hold_seconds = monitoring_node_network.DEFAULT_HOLD
            if args.operation == "monitoring-dns-rules":
                try:
                    args.monitoring_dns_error_percent = int(ask(tr, "ops.monitoring.dns_error_percent", args.monitoring_dns_error_percent))
                    args.monitoring_dns_window_seconds = int(ask(tr, "ops.monitoring.dns_window_seconds", args.monitoring_dns_window_seconds))
                    args.monitoring_dns_latency_ms = int(ask(tr, "ops.monitoring.dns_latency_ms", args.monitoring_dns_latency_ms))
                    args.monitoring_dns_min_requests = int(ask(
                        tr, "ops.monitoring.dns_min_requests", args.monitoring_dns_min_requests))
                    args.monitoring_dns_deployment = ask(tr, "ops.monitoring.dns_deployment", args.monitoring_dns_deployment)
                    args.monitoring_dns_min_replicas = int(ask(tr, "ops.monitoring.dns_min_replicas", args.monitoring_dns_min_replicas))
                except (TypeError, ValueError):
                    print(tr("ops.error.choice"))
                    return False
            if args.operation == "monitoring-certificate-rules":
                args.monitoring_certificate = ask(tr, "ops.monitoring.certificates", "").split()
                try:
                    args.monitoring_certificate_expiry_days = int(ask(
                        tr, "ops.monitoring.certificate_expiry_days", args.monitoring_certificate_expiry_days))
                except (TypeError, ValueError):
                    print(tr("ops.error.choice"))
                    return False
            if args.operation in ("monitoring-lb-rules", "monitoring-lb"):
                try:
                    args.monitoring_lb_capacity_percent = int(ask(
                        tr, "ops.monitoring.lb_capacity_percent", args.monitoring_lb_capacity_percent
                    ))
                    args.monitoring_lb_connection_error_threshold = int(ask(
                        tr, "ops.monitoring.lb_connection_error_threshold",
                        args.monitoring_lb_connection_error_threshold
                    ))
                    args.monitoring_lb_drift_threshold = int(ask(
                        tr, "ops.monitoring.lb_drift_threshold", args.monitoring_lb_drift_threshold
                    ))
                except (TypeError, ValueError):
                    print(tr("ops.error.choice"))
                    return False
            if args.operation == "monitoring-longhorn-disk-rules":
                try:
                    args.monitoring_longhorn_disk_used_percent = int(ask(
                        tr, "ops.monitoring.longhorn_disk_used_percent", args.monitoring_longhorn_disk_used_percent
                    ))
                except (TypeError, ValueError):
                    print(tr("ops.error.choice"))
                    return False
            if args.operation == "monitoring-workload-health-rules":
                try:
                    args.monitoring_restart_threshold = int(ask(
                        tr, "ops.monitoring.restart_threshold", args.monitoring_restart_threshold
                    ))
                    args.monitoring_restart_window_seconds = int(ask(
                        tr, "ops.monitoring.restart_window_seconds", args.monitoring_restart_window_seconds
                    ))
                except (TypeError, ValueError):
                    print(tr("ops.error.choice"))
                    return False
            if args.operation == "monitoring-workload-rules":
                args.monitoring_deployment = ask(tr, "ops.monitoring.deployments", "").split()
            if args.operation in ("monitoring-statefulset-rules", "monitoring-daemonset-rules"):
                args.monitoring_controller = ask(tr, "ops.monitoring.controllers", "").split()
            if args.operation in ("monitoring-storage-rules", "monitoring-longhorn-rules"):
                args.monitoring_pvc = ask(tr, "ops.monitoring.pvcs", "").split()
            if args.operation == "monitoring-storage-rules":
                try:
                    args.monitoring_free_percent = int(ask(tr, "ops.monitoring.free_percent", args.monitoring_free_percent))
                except (TypeError, ValueError):
                    print(tr("ops.error.choice"))
                    return False
            hold = ask(tr, "ops.monitoring.hold_seconds", args.monitoring_hold_seconds)
            age = ask(tr, "ops.monitoring.max_age", args.monitoring_max_age)
            try:
                args.monitoring_hold_seconds = int(hold)
                args.monitoring_max_age = int(age)
            except (TypeError, ValueError):
                print(tr("ops.error.choice"))
                return False
            args.monitoring_severity = select(
                tr, tuple((item, "ops.monitoring.severity." + item)
                          for item in ("critical", "warning", "info")), "ops.monitoring.severity")
            if not args.monitoring_severity:
                return False
        elif args.operation == "node-join":
            for field in ("node", "node_address", "node_interface", "data_device"):
                setattr(args, field, ask(tr, "ops.cli." + field, getattr(args, field)))
        elif args.operation in ("node-retire", "node-replace"):
            args.node = ask(tr, "ops.cli.node", args.node)
            if args.operation == "node-replace":
                for field in ("replacement_name", "node_address", "node_interface", "data_device"):
                    setattr(args, field, ask(tr, "ops.cli." + field, getattr(args, field)))
        if args.operation in ("node-maintenance", "node-retire", "node-replace", "cluster-upgrade"):
            answer = select(
                tr,
                ((False, "ops.choice.keep"), (True, "ops.choice.allow")),
                "ops.cli.delete_emptydir",
            )
            args.delete_emptydir = answer is True
    elif action in ("change-apply", "change-continue"):
        args.plan_file = ask(tr, "ops.cli.plan_file", args.plan_file)
    elif action == "change-status":
        args.plan_id = ask(tr, "ops.cli.plan_id", args.plan_id)
    elif action == "lock-recover":
        print(tr("ops.lock.recovery_guide"))
        args.lock_owner = ask(tr, "ops.cli.lock_owner", args.lock_owner)
        args.resource_version = ask(tr, "ops.cli.resource_version", args.resource_version)
        args.confirm = ask(tr, "ops.cli.confirm")
    elif action in ("backup-plan", "backup-create"):
        args.scope = select(
            tr,
            tuple(
                (name, "ops.scope." + name.replace("-", "_"))
                for name in ("configuration", "control-plane")
            ),
            "ops.cli.scope",
        )
        if not args.scope:
            return False
        if action == "backup-create":
            args.recipient = ask(tr, "ops.cli.recipient", args.recipient)
    elif action in ("backup-verify", "restore-plan", "restore-execute", "restore-rehearse"):
        args.backup = ask(tr, "ops.cli.backup", args.backup)
        args.identity = ask(tr, "ops.cli.identity", args.identity)
        if action != "backup-verify":
            args.restore_target = ask(tr, "ops.cli.restore_target", args.restore_target)
        if action in ("restore-execute", "restore-rehearse"):
            args.source_fingerprint = ask(tr, "ops.cli.source_fingerprint", args.source_fingerprint)
            args.confirm = ask(tr, "ops.cli.confirm")
    elif action in ("report", "security-check"):
        args.target = ask(tr, "ops.cli.target", args.target)
        if args.target:
            args.sudo = True
            args.ssh_config = ask(tr, "ops.cli.ssh_config", args.ssh_config)
    return True


def menu(args, tr, execute):
    defaults = vars(args).copy()
    while True:
        group = select(
            tr, tuple((name, "ops.menu." + name) for name, _ in GROUPS), "ops.menu.title"
        )
        if group is None:
            return 0
        if group is False:
            continue
        actions = next(items for name, items in GROUPS if name == group)
        action = select(
            tr,
            tuple((name, "ops.cli." + name.replace("-", "_")) for name in actions),
            "ops.menu." + group,
        )
        if not action:
            continue
        current = argparse.Namespace(**defaults)
        current.action = action
        if configure(current, tr):
            execute(current, tr)
            # Remember connection preferences, never destructive confirmations.
            for field in (
                "control_target",
                "kubeconfig",
                "ssh_config",
                "ssh_identity",
                "config",
                "profile_name",
            ):
                defaults[field] = getattr(current, field)
