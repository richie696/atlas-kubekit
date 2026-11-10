# SPDX-License-Identifier: MIT
"""Pin the Cilium chart and verified upstream Helm distribution used by native 02."""
from pathlib import Path
import re
import tarfile
import urllib.request
from urllib.parse import urlsplit

from ..host_actions import common, private_process
from ..models import OperationError
from .schema import artifact, digest
from scripts.lib.component_versions import catalog

HELM = catalog()["helm"]


def image_references(rendered):
    try:
        import yaml
    except ImportError:
        raise OperationError("ops.error.tool", "python3-yaml") from None
    references = set()
    try:
        for index, row in enumerate(yaml.safe_load_all(rendered)):
            if index >= 512:
                raise OperationError("ops.error.limit")
            if row is None:
                continue
            if not isinstance(row, dict):
                raise OperationError("ops.materials.invalid")
            if row.get("kind") not in ("DaemonSet", "Deployment"):
                continue
            spec = row.get("spec", {}).get("template", {}).get("spec", {})
            references.update(
                container["image"]
                for field in ("containers", "initContainers")
                for container in spec.get(field, [])
            )
    except (yaml.YAMLError, TypeError, KeyError, AttributeError, RecursionError):
        raise OperationError("ops.materials.invalid") from None
    if (
        not references
        or len(references) > 32
        or any(
            not isinstance(ref, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{1,511}", ref)
            for ref in references
        )
    ):
        raise OperationError("ops.materials.incomplete")
    return sorted(references)


def download(url, path, limit):
    if urlsplit(url).scheme != "https":
        raise OperationError("ops.materials.invalid")
    total = 0
    try:
        with urllib.request.urlopen(url, timeout=60) as response, Path(path).open("xb") as output:
            if urlsplit(response.url).scheme != "https":
                raise OperationError("ops.materials.invalid")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise OperationError("ops.error.limit")
                output.write(chunk)
    except (OSError, ValueError):
        raise OperationError("ops.materials.download") from None


def collect(base, configuration, log_directory):
    base = Path(base)
    (base / "charts").mkdir()
    (base / "tools").mkdir()
    version = configuration["versions"]["cilium"]
    private_process.run(
        [
            "helm",
            "pull",
            "oci://quay.io/cilium/charts/cilium",
            "--version",
            version,
            "--destination",
            str(base / "charts"),
        ],
        Path(log_directory) / "chart.log",
        timeout=600,
    )
    chart = base / "charts" / ("cilium-" + version + ".tgz")
    _, metadata = common.run(["helm", "show", "chart", str(chart)])
    if not re.search(r"(?m)^name: cilium\s*$", metadata) or not re.search(
        r"(?m)^version: " + re.escape(version) + r"\s*$", metadata
    ):
        raise OperationError("ops.materials.invalid")
    _, rendered = common.run(
        [
            "helm",
            "template",
            "cilium",
            str(chart),
            "-n",
            "kube-system",
            "--set",
            "ipam.mode=kubernetes",
            "--set",
            "operator.replicas=2",
            "--kube-version",
            configuration["versions"]["kubernetes"],
        ]
    )
    manifest = Path(log_directory) / "chart-rendered.yaml"
    common.replace(manifest, rendered.encode())
    references = image_references(rendered)
    url = "https://get.helm.sh/helm-" + HELM + "-linux-amd64.tar.gz"
    archive = Path(log_directory) / "helm.tar.gz"
    checksum = Path(log_directory) / "helm.sha256"
    download(url, archive, 128 * 1024**2)
    download(url + ".sha256sum", checksum, 4096)
    expected = checksum.read_text().split()[0]
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or digest(archive) != expected:
        raise OperationError("ops.error.integrity")
    with tarfile.open(archive, "r:gz") as source:
        member = source.getmember("linux-amd64/helm")
        if not member.isfile() or not 0 < member.size < 128 * 1024**2:
            raise OperationError("ops.materials.invalid")
        with source.extractfile(member) as stream, (base / "tools/helm").open("xb") as output:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
    return [
        artifact(
            base,
            chart.relative_to(base).as_posix(),
            "chart",
            "cilium",
            version,
            "oci://quay.io/cilium/charts/cilium",
            dict(sha256=digest(chart), imageReferences=sorted(references)),
        ),
        artifact(base, "tools/helm", "tool", "helm", HELM, url, dict(distributionSha256=expected)),
    ]
