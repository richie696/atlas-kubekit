# SPDX-License-Identifier: MIT
"""Package only repository-owned executable sources, binding material to code."""
from pathlib import Path
import gzip
import tarfile

from ..models import OperationError
from .schema import artifact


def collect(base):
    root = Path(__file__).resolve().parents[3]
    names = [
        "01-prepare-node.sh",
        "02-deploy-cluster.sh",
        "03-install-addons.sh",
        "04-verify-cluster.sh",
        "05-manage-lb.sh",
        "06-maintain-cluster.sh",
        "VERSION",
        "LICENSE",
        "licenses/NOTICE.md",
        "licenses/Helm.txt",
        "licenses/Cilium.txt",
    ]
    paths = [root / name for name in names]
    paths.append(root / "scripts/lib/component-versions.json")
    paths += sorted(path for path in (root / "scripts").rglob("*") if path.suffix in (".py", ".sh"))
    paths += sorted((root / "locales").glob("*.json"))
    target = Path(base) / "tools/atlas-kubekit.tar.gz"
    with (
        target.open("wb") as output,
        gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for file in paths:
            if file.is_symlink() or not file.is_file():
                raise OperationError("ops.materials.incomplete")
            info = tarfile.TarInfo(file.relative_to(root).as_posix())
            info.mode = 0o755 if file.suffix == ".sh" else 0o644
            info.size = file.stat().st_size
            with file.open("rb") as stream:
                archive.addfile(info, stream)
    return artifact(
        base,
        "tools/atlas-kubekit.tar.gz",
        "tool",
        "atlas-kubekit",
        (root / "VERSION").read_text().strip(),
        "current-source-package",
    )
