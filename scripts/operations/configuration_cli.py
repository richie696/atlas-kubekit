# SPDX-License-Identifier: MIT
"""Configuration use cases; no deployment or service changes occur here."""
from concurrent.futures import ThreadPoolExecutor

from .configuration import load_config, normalize, template, revision, PINNED_VERSIONS
from .drift import findings as drift_findings, plan as drift_plan
from .models import Finding, OperationError, OperationPlan, fingerprint, exit_status
from .profiles import import_profile, read_profile, export_config
from .reporting import write_report
from .transport import node_facts


def selected(args):
    if args.config and args.profile_name and args.action != "config-import":
        raise OperationError("ops.config.arguments")
    if args.config:
        return load_config(args.config)
    if args.profile_name:
        return read_profile(args.state_dir, args.profile_name)[0]
    raise OperationError("ops.config.arguments")


def collect_nodes(config, privileged, identity, ssh_config=None):
    def one(node):
        try:
            return node["name"], node_facts(
                config["ssh"]["user"] + "@" + node["address"], privileged, identity, ssh_config
            )
        except OperationError:
            return node["name"], {}

    with ThreadPoolExecutor(max_workers=min(4, len(config["nodes"]))) as pool:
        return dict(pool.map(one, config["nodes"]))


def perform(args, tr, context):
    if args.target:
        raise OperationError("ops.config.no_target")
    action = args.action
    if action == "config-template":
        config = template()
        if args.profile_name:
            config["cluster"]["name"] = args.profile_name
            config = normalize(config)
    else:
        config = selected(args)
    name = args.profile_name or config["cluster"]["name"]
    rev = revision(config)
    rows = [Finding("config-valid", "PASS", "ops.config.valid", (rev,))]
    plan = OperationPlan(
        action,
        fingerprint(config),
        (action,),
        scope="local-profile",
        parameters=(("configRevision", rev),),
    )
    if action in ("config-template", "config-export"):
        if not args.export_file:
            raise OperationError("ops.config.arguments")
        path = export_config(args.export_file, config)
        print(tr("ops.config.exported", str(path)))
        rows.append(Finding("config-export", "PASS", "ops.config.file_written"))
    elif action == "config-import":
        rev, changed = import_profile(args.state_dir, name, config, args.expected_revision)
        rows.append(
            Finding(
                "config-import",
                "PASS" if changed else "SKIP",
                "ops.config.imported" if changed else "ops.config.unchanged",
                (name, rev),
            )
        )
    elif action == "config-drift":
        observations = collect_nodes(
            config, args.sudo, args.ssh_identity, getattr(args, "ssh_config", None)
        )
        rows.extend(drift_findings(config, observations))
        plan = drift_plan(config, observations)
    elif action != "config-check":
        raise OperationError("ops.config.arguments")
    if config["versions"] != PINNED_VERSIONS:
        rows.append(Finding("deployment-compatibility", "WARN", "ops.config.unsupported_versions"))
    try:
        path, _ = write_report(
            args.output_dir,
            run_id=context[0],
            created_at=context[1],
            version=context[2],
            target=config["cluster"]["name"],
            findings=rows,
            translate=tr,
            plan=plan,
            operation=action,
        )
    except (OSError, OperationError):
        if action in ("config-template", "config-export", "config-import"):
            raise OperationError("ops.config.report_failed") from None
        raise
    for row in rows:
        print(f"[{row.status}] {row.check}: " + tr(row.message_key, *row.values))
    print(tr("ops.report.saved", str(path)))
    return exit_status(rows)
