#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Maintenance entrypoint: map arguments to explicit, bounded operations."""
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import signal
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.lib.i18n import translate, LocalizationError
from scripts.operations import configuration_cli
from scripts.operations.commands import changes as change_commands, recovery as recovery_commands
from scripts.operations.models import Finding, OperationError
from scripts.operations.reporting import write_report
from scripts.operations.arguments import parser_for
from scripts.operations.navigation import menu
from scripts.operations.commands import inspection, diagnostics as diagnostic_commands
from scripts.operations.commands import materials as material_commands
from scripts.operations.commands import protection as protection_commands


def perform(args, tr, context):
    if args.action == "protection-status":
        return protection_commands.perform(args, tr, context)
    if args.action.startswith("materials-"):
        return material_commands.perform(args, tr, context)
    if args.action.startswith("config-"):
        return configuration_cli.perform(args, tr, context)
    if args.action.startswith(("change-", "lock-")):
        return change_commands.perform(args, tr, context)
    if args.action in ("restore-execute", "restore-rehearse"):
        return recovery_commands.perform(args, tr, context)
    if args.action == "diagnostics":
        return diagnostic_commands.perform(args, tr, context)
    return inspection.perform(args, tr, context)


def execute(args, tr):
    now = datetime.now(timezone.utc)
    context = (
        now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12],
        now.isoformat(),
        (ROOT / "VERSION").read_text().strip(),
    )
    try:
        return perform(args, tr, context)
    except (OperationError, OSError, ValueError, KeyError, KeyboardInterrupt) as error:
        key = (
            error.key
            if isinstance(error, OperationError)
            else (
                "ops.error.cancelled"
                if isinstance(error, KeyboardInterrupt)
                else "ops.error.filesystem"
            )
        )
        values = error.values if isinstance(error, OperationError) else ()
        target = (
            args.target
            if args.target
            and re.fullmatch(r"[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.-]{0,252}", args.target)
            else "local-or-unresolved"
        )
        finding = Finding("operation", "FAIL", key, values)
        try:
            destination, _ = write_report(
                args.output_dir,
                run_id=context[0],
                created_at=context[1],
                version=context[2],
                target=target,
                findings=(finding,),
                translate=tr,
                operation=args.action,
            )
            print(tr("ops.report.saved", str(destination)), file=sys.stderr)
        except (OSError, OperationError):
            print(tr("ops.error.report_failed", context[0]), file=sys.stderr)
        print(tr(key, *values), file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    def terminate(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, terminate)
    lang = os.environ.get("ATLAS_LANG", "en")
    for index, arg in enumerate(argv):
        if arg == "--lang" and index + 1 < len(argv):
            lang = argv[index + 1]
        elif arg.startswith("--lang="):
            lang = arg.split("=", 1)[1]
    try:
        parser = parser_for(lang)
        args = parser.parse_args(argv)
        tr = lambda key, *values: translate(args.lang, key, *values)
        if args.action is None:
            if not sys.stdin.isatty():
                parser.print_help()
                return 2
            return menu(args, tr, execute)
        return execute(args, tr)
    except OperationError as error:
        print(translate(lang, error.key, *error.values), file=sys.stderr)
        return 1
    except LocalizationError:
        print(translate("en", "ops.error.locale"), file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError):
        print(translate(lang, "ops.error.filesystem"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(translate(lang, "ops.error.cancelled"), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
