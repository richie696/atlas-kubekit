#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Repository checks; never connect to or mutate a deployment environment."""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    bash = shutil.which("bash")
    if bash is None:
        raise SystemExit("Bash is required for syntax validation")
    sources = sorted((ROOT / "scripts").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py"))
    for source in sources:
        ast.parse(source.read_bytes(), filename=str(source))
    shells = sorted(ROOT.glob("*.sh")) + sorted((ROOT / "scripts").rglob("*.sh"))
    checks = [[bash, "-n", str(source)] for source in shells]
    checks += [
        [sys.executable, "scripts/lib/i18n.py", "--validate"],
        [sys.executable, "scripts/update-version.py", "--check"],
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
    ]
    for command in checks:
        subprocess.run(
            command, cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=True
        )
    print("Source, shell syntax, locale catalogs, version metadata and regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
