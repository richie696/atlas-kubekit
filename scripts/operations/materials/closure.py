# SPDX-License-Identifier: MIT
"""The native offline installation has a finite, mandatory material inventory."""
from ..models import OperationError
from scripts.lib.component_versions import catalog
import re


def require_complete(manifest):
    from .apt_repository import BASE_PACKAGES
    from .charts import HELM

    rows = manifest["artifacts"]
    names = {}
    for item in rows:
        key = (item["kind"], item["name"])
        if key in names:
            raise OperationError("ops.materials.invalid")
        names[key] = item
    packages = {item["name"]: item for item in rows if item["kind"] == "apt"}
    required = set(BASE_PACKAGES) | {"kubeadm", "kubelet", "kubectl", "cri-tools"}
    indexes = {"Packages", "Packages.gz", "Release", "InRelease", "Release.gpg", "repository.gpg"}
    if (
        not required <= set(packages)
        or not indexes
        <= {
            item["name"]
            for item in rows
            if item["kind"] in ("apt-index", "apt-signature", "apt-key")
        }
        or not {("tool", "helm"), ("tool", "atlas-kubekit"), ("chart", "cilium")} <= set(names)
    ):
        raise OperationError("ops.materials.incomplete")
    for name in ("kubeadm", "kubelet", "kubectl"):
        if (
            packages[name]["version"]
            != manifest["configuration"]["versions"]["kubernetes"][1:] + "-1.1"
        ):
            raise OperationError("ops.materials.invalid")
    if (
        packages["cri-tools"]["version"] != catalog()["criTools"][1:] + "-1.1"
        or names[("chart", "cilium")]["version"] != manifest["configuration"]["versions"]["cilium"]
        or names[("tool", "helm")]["version"] != HELM
        or names[("tool", "atlas-kubekit")]["version"] != manifest["toolVersion"]
    ):
        raise OperationError("ops.materials.invalid")
    images = [item["name"] for item in rows if item["kind"] == "image"]
    for item in rows:
        if item["kind"] != "image":
            continue
        digest = item["metadata"].get("digest")
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
            or item["version"] != digest
            or "@" in item["name"]
            and item["name"].rsplit("@", 1)[1] != digest
        ):
            raise OperationError("ops.materials.invalid")
    for path, version in [
        *[
            (name, manifest["configuration"]["versions"]["kubernetes"])
            for name in (
                "registry.k8s.io/kube-apiserver",
                "registry.k8s.io/kube-controller-manager",
                "registry.k8s.io/kube-scheduler",
                "registry.k8s.io/kube-proxy",
            )
        ],
        *[
            (name, "v" + manifest["configuration"]["versions"]["cilium"])
            for name in ("quay.io/cilium/cilium", "quay.io/cilium/operator-generic")
        ],
    ]:
        reference = path + ":" + version
        if not any(name == reference or name.startswith(reference + "@sha256:") for name in images):
            raise OperationError("ops.materials.incomplete")
    chart_images = names[("chart", "cilium")]["metadata"].get("imageReferences")
    if (
        not isinstance(chart_images, list)
        or not chart_images
        or len(chart_images) > 32
        or any(
            not isinstance(reference, str) or reference not in images for reference in chart_images
        )
    ):
        raise OperationError("ops.materials.incomplete")
    for prefix in (
        "registry.k8s.io/kube-apiserver:",
        "registry.k8s.io/kube-controller-manager:",
        "registry.k8s.io/kube-scheduler:",
        "registry.k8s.io/kube-proxy:",
        "registry.k8s.io/coredns/coredns:",
        "registry.k8s.io/etcd:",
        "registry.k8s.io/pause:",
        "quay.io/cilium/cilium:",
        "quay.io/cilium/operator-generic:",
        "quay.io/cilium/cilium-envoy:",
    ):
        if not any(name.startswith(prefix) for name in images):
            raise OperationError("ops.materials.incomplete")
