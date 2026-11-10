# SPDX-License-Identifier: MIT
"""Map change CLI input to validated plans or a registered capability executor."""
from pathlib import Path
import json
import sys

from ..adapters.kubernetes import Kubernetes
from ..changes import plans, store, execution, continuation, preview
from ..changes.coordinator import ClusterCoordinator
from .. import configuration_cli, configuration_cluster
from ..capabilities.registry import CAPABILITIES
from ..environment import Environment
from ..models import Finding, OperationError, exit_status
from ..reporting import write_report


def environment(args):
    client = Kubernetes(args.kubeconfig, args.control_target, args.ssh_identity, args.ssh_config)
    return Environment(client, args.ssh_identity, args.ssh_config)


def configuration(args, context):
    if args.config or args.profile_name:
        return configuration_cli.selected(args)
    value = configuration_cluster.read(context.client)
    if value is None:
        raise OperationError("ops.config.arguments")
    return value


def plan_parameters(args, context):
    config = configuration(args, context)
    return CAPABILITIES[args.operation].from_arguments(args, config, context)


def perform(args, tr, run_context):
    if args.target:
        raise OperationError("ops.config.no_target")
    context = environment(args)
    client = context.client
    coordinator = ClusterCoordinator(client, args.lock_namespace)
    details = None
    rows = []
    if args.action in ("change-plan", "change-continue"):
        if args.action == "change-continue":
            if not args.plan_file:
                raise OperationError("ops.change.invalid_parameters")
            previous = store.read(args.plan_file, history=True)
            operation = previous.capability
            raw = continuation.parameters(previous, coordinator)
        else:
            operation = args.operation
            raw = plan_parameters(args, context) if operation in CAPABILITIES else None
        if operation not in CAPABILITIES:
            raise OperationError("ops.change.invalid_parameters")
        capability = CAPABILITIES[operation]
        if args.action == "change-continue" and hasattr(capability, "continue_parameters"):
            raw = capability.continue_parameters(raw, context)
        values = capability.parameters(raw)
        observation = capability.observe(values, context)
        plan = plans.make(operation, client.cluster_identity(), observation, values)
        path = (
            Path(args.plan_file)
            if args.plan_file and args.action != "change-continue"
            else Path(args.output_dir) / "plans" / (plan.id + ".json")
        )
        store.save(path, plan)
        preview.show(plan, capability, tr)
        details = dict(state="planned", planId=plan.id, planFile=str(path))
        rows.append(Finding("change-plan", "PASS", "ops.change.planned", (plan.id, str(path))))
    elif args.action == "change-apply":
        if not args.plan_file:
            raise OperationError("ops.change.invalid_parameters")
        plan = store.read(args.plan_file)
        capability = CAPABILITIES.get(plan.capability)
        if capability is None:
            raise OperationError("ops.change.invalid_parameters")
        confirmation = args.confirm
        preview.show(plan, capability, tr)
        if confirmation is None and sys.stdin.isatty():
            confirmation = input(tr("ops.change.confirm", plan.id)).strip()

        def apply(values, checkpoint):
            return capability.apply(values, context, checkpoint, plan.id)

        details = execution.execute(
            plan.record(),
            confirmation,
            coordinator,
            lambda values: capability.observe(values, context),
            apply,
            capability.parameters,
            progress=lambda step: print(tr("ops.change.progress", step), flush=True),
        )
        rows.append(
            Finding(
                "change-execution",
                "PASS",
                "ops.change.completed",
                (tr("ops.state." + details["state"].replace("-", "_")),),
            )
        )
    elif args.action == "change-status":
        if not args.plan_id:
            raise OperationError("ops.change.invalid_parameters")
        details = coordinator.journal(args.plan_id)
        if details is None:
            raise OperationError("ops.change.journal")
        status = (
            "PASS"
            if details["state"] == "succeeded"
            else "FAIL" if details["state"] == "failed" else "WARN"
        )
        rows.append(
            Finding(
                "change-status",
                status,
                "ops.change.completed",
                (tr("ops.state." + details["state"].replace("-", "_")),),
            )
        )
    elif args.action == "lock-status":
        value = coordinator.status()
        details = dict(state="unlocked", owner=None, resourceVersion=None)
        if value:
            details.update(
                state="locked" if value.get("data", {}).get("owner") else "unlocked",
                owner=value.get("data", {}).get("owner"),
                planId=value.get("data", {}).get("planId"),
                resourceVersion=value["metadata"]["resourceVersion"],
            )
        rows.append(
            Finding(
                "cluster-lock",
                "WARN" if details["owner"] else "PASS",
                "ops.change.completed",
                (tr("ops.state." + details["state"]),),
            )
        )
    elif args.action == "lock-recover":
        coordinator.recover(args.lock_owner, args.resource_version, args.confirm)
        details = dict(state="unlocked")
        rows.append(
            Finding("cluster-lock", "PASS", "ops.change.completed", (tr("ops.state.unlocked"),))
        )
    else:
        raise OperationError("ops.change.invalid_parameters")
    try:
        destination, _ = write_report(
            args.output_dir,
            run_id=run_context[0],
            created_at=run_context[1],
            version=run_context[2],
            target=client.cluster_identity(),
            findings=rows,
            translate=tr,
            operation=args.action,
            execution=details,
        )
    except (OSError, OperationError):
        if args.action in ("change-apply", "lock-recover"):
            raise OperationError("ops.change.report_failed") from None
        if args.action in ("change-plan", "change-continue"):
            raise OperationError("ops.change.plan_report_failed") from None
        raise
    for row in rows:
        print(f"[{row.status}] " + tr(row.message_key, *row.values))
    if args.action in ("change-status", "lock-status"):
        print(json.dumps(details, ensure_ascii=False, indent=2))
    print(tr("ops.report.saved", str(destination)))
    return exit_status(rows)
