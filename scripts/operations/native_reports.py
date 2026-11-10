# SPDX-License-Identifier: MIT
"""Adapt native shell outcomes into reports without capturing raw command output."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import socket
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.lib.i18n import translate
from scripts.operations.models import Finding
from scripts.operations.reporting import write_report


def publish(entrypoint, action, result, errors, skips, output, lang):
    if entrypoint not in ("02", "04") or result not in ("passed", "failed", "cancelled"):
        raise ValueError("invalid native result")
    import re

    if not re.fullmatch(r"[a-z_]{1,40}", action) or any(
        type(count) is not int or not 0 <= count <= 100000 for count in (errors, skips)
    ):
        raise ValueError("invalid native counters")
    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    status = (
        "FAIL"
        if errors or result == "failed"
        else "WARN" if skips or result == "cancelled" else "PASS"
    )
    return write_report(
        output,
        run_id=run_id,
        version=(ROOT / "VERSION").read_text().strip(),
        created_at=now.isoformat(),
        target=socket.gethostname(),
        findings=(
            Finding(
                "phase" + entrypoint + "." + action,
                status,
                "ops.native.outcome",
                (
                    entrypoint,
                    action,
                    translate(lang, "ops.state." + ("succeeded" if result == "passed" else result)),
                    errors,
                    skips,
                ),
            ),
        ),
        translate=lambda key, *values: translate(lang, key, *values),
        operation="native-" + entrypoint,
    )[0]


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--entrypoint", required=True, choices=("02", "04"))
    parser.add_argument("--action", required=True)
    parser.add_argument("--result", required=True, choices=("passed", "failed", "cancelled"))
    parser.add_argument("--errors", type=int, default=0)
    parser.add_argument("--skips", type=int, default=0)
    parser.add_argument(
        "--output-dir",
        default=(
            "/var/lib/k8s-deploy/reports"
            if os.geteuid() == 0
            else str(Path.home() / ".local/state/atlas-kubekit/operations")
        ),
    )
    parser.add_argument("--lang", default=os.environ.get("ATLAS_LANG", "en"))
    args = parser.parse_args(argv)
    try:
        destination = publish(
            args.entrypoint,
            args.action,
            args.result,
            args.errors,
            args.skips,
            args.output_dir,
            args.lang,
        )
        print(translate(args.lang, "ops.report.saved", str(destination)))
        return 0
    except (OSError, ValueError, RuntimeError):
        print(translate(args.lang, "ops.native.report_failed"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
