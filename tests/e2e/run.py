#!/usr/bin/env python3
"""Opt-in lab acceptance; never run automatically on a pull request."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.e2e import lab, offline
from scripts.operations.reporting import private_directory, atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--scenario", required=True, choices=("phase-one", "deploy", "repeat"))
    parser.add_argument("--node")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    fixture = lab.read(args.lab)
    if args.confirm != fixture.id:
        parser.error("Confirmation must match the disposable lab ID")
    output = private_directory(Path(args.output).resolve())
    result = dict(schemaVersion=1, labId=fixture.id, scenario=args.scenario, state="failed")
    try:
        if args.scenario == "phase-one":
            node = next(node for node in fixture.nodes if node.name == args.node)
            details = offline.phase_one(fixture, node, output)
        else:
            details = offline.deployment(fixture, output, args.scenario == "repeat")
        result.update(
            state="awaiting-reboot-verification" if args.scenario == "phase-one" else "passed",
            details=details,
        )
        return 0
    finally:
        atomic_json(output / "result.json", result)
        print(json.dumps(result))


if __name__ == "__main__":
    raise SystemExit(main())
