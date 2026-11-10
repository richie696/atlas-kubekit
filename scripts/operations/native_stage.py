#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run an installed native stage under the same host lock as maintenance actions."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.host_actions.common import LOCK_DIRECTORY
from scripts.operations.locking import exclusive_lock
from scripts.lib.host_lock import host_operation, HostLockError
from scripts.operations.models import OperationError
from scripts.lib.i18n import translate

STAGES = frozenset(
    (
        "setup-k8s-node.sh",
        "setup-lb.sh",
        "configure-registry-mirror.sh",
        "init-cluster.sh",
        "install-cilium.sh",
        "create-join-credentials.sh",
        "join-nodes.sh",
        "configure-coredns-ha.sh",
    )
)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if os.geteuid() != 0:
            raise OperationError("ops.error.root")
        if not args or args[0] not in STAGES:
            raise OperationError("ops.change.invalid_parameters")
        source = ROOT / args[0]
        if not source.is_file():
            source = ROOT / "scripts/stages" / args[0]
        if (
            source.is_symlink()
            or not source.is_file()
            or source.stat().st_uid != 0
            or source.stat().st_mode & 0o022
        ):
            raise OperationError("ops.error.path")
        with host_operation() as descriptor:
            return subprocess.run(
                ["/bin/bash", str(source), *args[1:]], pass_fds=(descriptor,)
            ).returncode
    except HostLockError as error:
        print(translate(os.environ.get("ATLAS_LANG", "en"), str(error)), file=sys.stderr)
        return 1
    except OperationError as error:
        print(
            translate(os.environ.get("ATLAS_LANG", "en"), error.key, *error.values), file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
