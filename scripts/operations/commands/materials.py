# SPDX-License-Identifier: MIT
"""Material command mapping and unified reports; no provider code in the CLI."""
from .. import configuration_cli
from ..materials import builder, installer, schema
from ..materials.verification import payloads
from ..models import Finding, OperationError
from ..reporting import write_report


def perform(args, tr, context):
    if args.target or args.control_target:
        raise OperationError("ops.config.no_target")
    if args.action == "materials-prepare":
        target, manifest = builder.prepare(args.output_dir, configuration_cli.selected(args))
    elif args.action == "materials-verify":
        if not args.materials:
            raise OperationError("ops.materials.invalid")
        manifest = schema.verify(args.materials, args.material_id)
        payloads(args.materials,manifest)
        target = args.materials
    elif args.action == "materials-import":
        if not args.materials or not args.material_id or args.confirm != args.material_id:
            raise OperationError("ops.error.confirm")
        target, manifest = installer.activate(args.materials, args.material_id)
    else:
        raise OperationError("ops.materials.invalid")
    rows = (Finding("materials", "PASS", "ops.materials.complete", (manifest["id"], str(target))),)
    destination, _ = write_report(
        args.output_dir,
        run_id=context[0],
        created_at=context[1],
        version=context[2],
        target="material-" + manifest["id"],
        findings=rows,
        translate=tr,
        operation=args.action,
        execution=dict(
            state="succeeded", materialId=manifest["id"], artifacts=len(manifest["artifacts"])
        ),
    )
    print(tr("ops.materials.complete", manifest["id"], str(target)))
    print(tr("ops.report.saved", str(destination)))
    return 0
