# SPDX-License-Identifier: MIT
"""Validate payload formats before a host is activated for offline installation."""
from pathlib import Path, PurePosixPath
import re
import tarfile

from ..host_actions import common
from ..models import OperationError
from . import images
from scripts.lib.archive import gzip_tar, ArchiveLimitError


def safe_tar(path, limit, prefixes, required):
    try:
        with Path(path).open("rb") as stream, gzip_tar(stream, limit) as archive:
            names = set()
            total = 0
            for item in archive:
                total += item.size
                if len(names) >= 20000 or item.name in names or total > limit:
                    raise OperationError("ops.error.limit")
                names.add(item.name)
                name = PurePosixPath(item.name)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or not item.isfile()
                    and not item.isdir()
                    or not any(
                        (
                            (item.name.startswith(prefix) or item.name == prefix.rstrip("/"))
                            if prefix.endswith("/")
                            else item.name == prefix
                        )
                        for prefix in prefixes
                    )
                ):
                    raise OperationError("ops.error.integrity")
            if not set(required) <= names:
                raise OperationError("ops.materials.incomplete")
    except ArchiveLimitError:
        raise OperationError("ops.error.limit") from None
    except (tarfile.TarError, OSError, EOFError):
        raise OperationError("ops.error.integrity") from None


def payloads(base, manifest, *, signature=False):
    base = Path(base)
    for item in manifest["artifacts"]:
        file = base / item["path"]
        if item["kind"] == "image":
            images.verify(file, item["metadata"].get("digest"))
        elif item["kind"] == "chart":
            safe_tar(file, 128 * 1024**2, ("cilium/",), ("cilium/Chart.yaml",))
        elif item["kind"] == "tool" and item["name"] == "helm":
            with file.open("rb") as stream:
                header = stream.read(64)
            if (
                len(header) != 64
                or header[:6] != b"\x7fELF\x02\x01"
                or header[18:20] != b"\x3e\x00"
            ):
                raise OperationError("ops.materials.platform")
        elif item["kind"] == "tool" and item["name"] == "atlas-kubekit":
            safe_tar(
                file,
                64 * 1024**2,
                (
                    "scripts/",
                    "locales/",
                    "01-prepare-node.sh",
                    "02-deploy-cluster.sh",
                    "03-install-addons.sh",
                    "04-verify-cluster.sh",
                    "05-manage-lb.sh",
                    "06-maintain-cluster.sh",
                    "VERSION",
                    "LICENSE",
                    "licenses/",
                ),
                (
                    "01-prepare-node.sh",
                    "02-deploy-cluster.sh",
                    "06-maintain-cluster.sh",
                    "VERSION",
                    "LICENSE",
                    "licenses/NOTICE.md",
                    "licenses/Helm.txt",
                    "licenses/Cilium.txt",
                ),
            )
    if signature:
        common.run(
            [
                "gpgv",
                "--keyring",
                str(base / "apt/repository.gpg"),
                str(base / "apt/Release.gpg"),
                str(base / "apt/Release"),
            ]
        )
