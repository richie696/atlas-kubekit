# SPDX-License-Identifier: MIT
"""Read-only inspection and explicitly confirmed local backup commands."""
import sys
from .. import backup
from ..models import (
    Finding,
    OperationError,
    cleanup_plan,
    exit_status,
    security_findings,
    service_findings,
)
from ..reporting import write_report
from ..transport import node_facts


def perform(args, tr, context):
    run_id, now, version = context
    plan = None
    if args.target and args.action not in ("report", "security-check"):
        raise OperationError("ops.error.local_backup")
    facts = node_facts(args.target, args.sudo, args.ssh_identity, args.ssh_config)
    target = args.target or facts["hostname"]
    if args.action in ("report", "security-check"):
        findings = security_findings(facts)
        if args.action == "report":
            findings += service_findings(facts)
        plan = cleanup_plan(facts)
    else:
        if args.target:
            raise OperationError("ops.error.local_backup")
        backup.require_root()
        if args.action == "backup-plan":
            records = backup.source_inventory(args.output_dir)
            plan = backup.backup_plan(facts, args.scope, records)
            findings = (
                Finding("backup-plan", "PASS", "ops.finding.plan_created"),
                Finding("coverage", "WARN", "ops.finding.partial_coverage"),
            )
        elif args.action == "backup-create":
            confirmation = args.confirm
            if confirmation is None and sys.stdin.isatty():
                confirmation = input(tr("ops.prompt.confirm_backup"))
            if confirmation != "BACKUP":
                raise OperationError("ops.error.confirm")
            destination, plan = backup.create_backup(
                args.output_dir,
                run_id=run_id,
                version=version,
                created_at=now,
                facts=facts,
                scope=args.scope,
                recipient=args.recipient,
            )
            print(tr("ops.backup.created", str(destination)))
            findings = (
                Finding("backup-created", "PASS", "ops.finding.backup_created"),
                Finding("coverage", "WARN", "ops.finding.partial_coverage"),
            )
        else:
            if not args.backup or not args.identity:
                raise OperationError("ops.error.backup_arguments")
            manifest = backup.verify_backup(args.backup, args.identity)
            findings = (
                Finding("backup-integrity", "PASS", "ops.finding.integrity_ok"),
                Finding("coverage", "WARN", "ops.finding.partial_coverage"),
            )
            if args.action == "restore-plan":
                if not args.restore_target:
                    raise OperationError("ops.error.restore_target")
                plan = backup.restore_plan(manifest, facts, args.restore_target)
                findings += (Finding("restore-execution", "SKIP", "ops.finding.restore_plan_only"),)
    try:
        destination, data = write_report(
            args.output_dir,
            run_id=run_id,
            version=version,
            created_at=now,
            target=target,
            findings=findings,
            translate=tr,
            plan=plan,
            operation=args.action,
        )
    except (OSError, OperationError):
        if args.action == "backup-create":
            raise OperationError("ops.error.backup_report_failed") from None
        raise
    for finding in findings:
        print(f"[{finding.status}] {finding.check}: " + tr(finding.message_key, *finding.values))
    if plan is not None:
        print(tr("ops.report.plan_summary", plan.operation, plan.scope or "-", len(plan.resources)))
    print(tr("ops.report.saved", str(destination)))
    return exit_status(findings)
