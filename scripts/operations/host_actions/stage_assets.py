# SPDX-License-Identifier: MIT
"""Reuse native prerequisites without accepting caller-provided executable text."""
from contextlib import contextmanager
from pathlib import Path
import pkgutil
import tempfile

from ..models import OperationError

ASSETS = (
    "stages/setup-k8s-node.sh",
    "stages/configure-registry-mirror.sh",
    "stages/storage.py",
    "stages/containerd.py",
    "lib/stage-runtime.sh",
    "lib/versions.sh",
    "lib/component_versions.py",
    "lib/component-versions.json",
    "lib/packages.sh",
    "lib/i18n.sh",
    "lib/i18n.py",
    "locales/en.json",
    "locales/zh_cn.json",
    "locales/zh_tw.json",
)


@contextmanager
def installed():
    with tempfile.TemporaryDirectory(prefix=".atlas-stages-") as directory:
        root = Path(directory)
        for name in ASSETS:
            data = pkgutil.get_data("scripts.operations.assets", name)
            if data is None:
                raise OperationError("ops.host.failed")
            path = root / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)
        yield root
