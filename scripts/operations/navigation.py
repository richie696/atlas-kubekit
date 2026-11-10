# SPDX-License-Identifier: MIT
"""Navigation groups collect input; domain commands validate and execute."""
import argparse
from .commands.changes import CAPABILITIES

GROUPS = (
    ("materials", ("materials-prepare", "materials-verify", "materials-import")),
    ("inspect", ("report", "security-check", "diagnostics")),
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
    if action in ("diagnostics", "protection-status") or action.startswith(("change-", "lock-")):
        connection(args, tr)
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
        if args.operation == "lb-stats":
            args.stats_cidr = ask(tr, "ops.cli.stats_cidr", "")
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
