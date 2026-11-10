# SPDX-License-Identifier: MIT
"""Protection status has a distinct result from backup or restore execution."""
from ..adapters.kubernetes import Kubernetes
from ..models import Finding, OperationError
from ..protection import coverage
from ..reporting import write_report


def perform(args, tr, context):
    if args.target:
        raise OperationError("ops.config.no_target")
    client = Kubernetes(args.kubeconfig, args.control_target, args.ssh_identity, args.ssh_config)
    details = coverage.collect(client)
    findings = [
        Finding(
            "protection-coverage",
            "WARN",
            "ops.protection.summary",
            (len(details["objects"]), details["unregistered"], details["stale"]),
        )
    ]
    for row in details["objects"]:
        findings.append(
            Finding(
                "protection-" + row["kind"] + "/" + row["namespace"] + "/" + row["name"],
                (
                    "FAIL"
                    if row["state"] == "identity-stale"
                    else "SKIP" if row["state"] == "superseded-identity" else "UNVERIFIED"
                ),
                "ops.protection.object",
                (
                    row["kind"] + "/" + row["namespace"] + "/" + row["name"],
                    tr("ops.protection.state." + row["state"]),
                ),
            )
        )
    path, _ = write_report(
        args.output_dir,
        run_id=context[0],
        created_at=context[1],
        version=context[2],
        target=details["clusterIdentity"],
        findings=findings,
        translate=tr,
        operation=args.action,
        execution=dict(state="observed", result=details),
    )
    for finding in findings:
        print("[" + finding.status + "] " + tr(finding.message_key, *finding.values))
    print(tr("ops.report.saved", str(path)))
    return 1 if details["stale"] else 2
