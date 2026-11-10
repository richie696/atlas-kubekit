# SPDX-License-Identifier: MIT
"""Isolated recovery CLI mapping and explicit scope/result reporting."""
from .. import recovery
from .. import recovery_rehearsal
from ..models import Finding, OperationError
from ..reporting import write_report


def perform(args, tr, context):
    if args.target:
        raise OperationError("ops.error.local_backup")
    if not args.backup or not args.identity or not args.restore_target:
        raise OperationError("ops.error.backup_arguments")
    result = recovery.restore(
        args.backup,
        args.identity,
        args.restore_target,
        expected_source=args.source_fingerprint,
        confirmation=args.confirm,
        bump_revision=args.bump_revision,
        require_etcd=args.action == "restore-rehearse",
    )
    if args.action == "restore-rehearse":
        result = recovery_rehearsal.run(args.restore_target, args.source_fingerprint)
    rows = (
        Finding("isolated-restore", "PASS", "ops.restore.completed", (args.restore_target,)),
        Finding("coverage", "WARN", "ops.finding.partial_coverage"),
        Finding(
            "restored-services",
            "PASS" if args.action == "restore-rehearse" else "UNVERIFIED",
            (
                "ops.restore.rehearsed"
                if args.action == "restore-rehearse"
                else "ops.restore.serving_unverified"
            ),
        ),
    )
    try:
        path, _ = write_report(
            args.output_dir,
            run_id=context[0],
            created_at=context[1],
            version=context[2],
            target=args.restore_target,
            findings=rows,
            translate=tr,
            operation=args.action,
            execution=dict(state="succeeded", result=result),
        )
    except (OSError, OperationError):
        raise OperationError("ops.restore.report_failed") from None
    for row in rows:
        print(f"[{row.status}] " + tr(row.message_key, *row.values))
    print(tr("ops.report.saved", str(path)))
    return 2
