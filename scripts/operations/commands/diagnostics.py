# SPDX-License-Identifier: MIT
"""Diagnostics command, separate from interactive navigation and projections."""
from ..adapters.kubernetes import Kubernetes
from ..diagnostics.bundle import publish
from ..models import exit_status
from ..reporting import write_report


def perform(args, tr, context):
    client = Kubernetes(args.kubeconfig, args.control_target, args.ssh_identity, args.ssh_config)
    target, findings = publish(args.output_dir, context[0], context[2], context[1], client)
    destination, _ = write_report(
        args.output_dir,
        run_id=context[0],
        created_at=context[1],
        version=context[2],
        target=client.cluster_identity(),
        findings=findings,
        translate=tr,
        operation="diagnostics",
    )
    for finding in findings:
        print("[" + finding.status + "] " + tr(finding.message_key, *finding.values))
    print(tr("ops.diagnostics.saved", str(target)))
    print(tr("ops.diagnostics.review"))
    print(tr("ops.report.saved", str(destination)))
    return exit_status(findings)
