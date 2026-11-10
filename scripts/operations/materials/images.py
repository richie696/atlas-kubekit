# SPDX-License-Identifier: MIT
"""Export the declared base images and prove their Linux OCI dependency closure."""
from pathlib import Path
import re
import tarfile

from .. import oci
from ..host_actions import common, private_process
from ..models import OperationError
from .schema import artifact


def cached_reference(reference):
    if "@" not in reference:
        return reference
    repository, digest = reference.split("@", 1)
    prefix, separator, name = repository.rpartition("/")
    return prefix + separator + name.split(":", 1)[0] + "@" + digest


def verify(path, digest):
    with oci.open_archive(path) as archive:
        members = archive.getmembers()
        names = [item.name for item in members]
        if (
            len(names) > 20000
            or len(names) != len(set(names))
            or sum(item.size for item in members) > oci.MAX_IMAGE_BYTES
        ):
            raise OperationError("ops.error.limit")
        for member in members:
            if member.isdir():
                if member.name.rstrip("/") not in ("blobs", "blobs/sha256"):
                    raise OperationError("ops.error.integrity")
                continue
            if (
                not member.isfile()
                or member.name not in ("index.json", "oci-layout", "manifest.json")
                and not re.fullmatch(r"blobs/sha256/[a-f0-9]{64}", member.name)
            ):
                raise OperationError("ops.error.integrity")
        image = oci.manifest(archive, digest)
        for layer in image["layers"]:
            oci.verified_blob(archive, layer["digest"])


def collect(base, configuration, log_directory, component_references):
    base = Path(base)
    directory = base / "images"
    directory.mkdir()
    _, raw = common.run(
        [
            "kubeadm",
            "config",
            "images",
            "list",
            "--kubernetes-version",
            configuration["versions"]["kubernetes"],
        ]
    )
    references = set(raw.splitlines())
    if (
        not isinstance(component_references, list)
        or not component_references
        or len(component_references) > 32
    ):
        raise OperationError("ops.materials.invalid")
    references.update(component_references)
    if not references or len(references) > 100:
        raise OperationError("ops.materials.incomplete")
    artifacts = []
    for index, reference in enumerate(sorted(references)):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{1,511}", reference):
            raise OperationError("ops.materials.invalid")
        cached = cached_reference(reference)
        _, raw = common.run(["ctr", "-n", "k8s.io", "images", "list", "name==" + cached])
        rows = [row.split() for row in raw.splitlines() if row.split() and row.split()[0] == cached]
        if (
            len(rows) != 1
            or len(rows[0]) < 3
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", rows[0][2])
        ):
            raise OperationError("ops.materials.incomplete")
        digest = rows[0][2]
        relative = "images/" + str(index) + ".oci.tar"
        file = base / relative
        private_process.run(
            [
                "ctr",
                "-n",
                "k8s.io",
                "images",
                "export",
                "--platform",
                "linux/amd64",
                str(file),
                cached,
            ],
            Path(log_directory) / ("image-" + str(index) + ".log"),
            timeout=600,
        )
        verify(file, digest)
        artifacts.append(
            artifact(
                base,
                relative,
                "image",
                reference,
                digest,
                reference,
                dict(digest=digest, platform="linux/amd64"),
            )
        )
    return artifacts
