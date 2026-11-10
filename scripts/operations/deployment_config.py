#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Restricted phase-two bridge. Emit validated fields, never shell source."""
from pathlib import Path
import os
import inspect
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.configuration import load_config, deployment_rows
from scripts.operations.models import OperationError
from scripts.lib.i18n import translate
from scripts.operations.collector import identity_facts


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if args == ["--identity-source"]:
            print(
                "import hashlib, re, socket\nfrom pathlib import Path\n"
                + inspect.getsource(identity_facts)
                + "\nvalue=identity_facts()\nif value['identityState']!='ok':raise SystemExit(1)\nprint(value['identityFingerprint'])"
            )
            return 0
        if len(args) != 1:
            raise OperationError("ops.config.arguments")
        for row in deployment_rows(load_config(args[0])):
            print("\t".join(row))
    except OperationError as error:
        print(
            translate(os.environ.get("ATLAS_LANG", "en"), error.key, *error.values), file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
