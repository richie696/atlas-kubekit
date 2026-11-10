# SPDX-License-Identifier: MIT
"""Fixed patch-upgrade actions; package and kubeadm output stays private on the host."""
import re
from pathlib import Path

from ..collector import identity_facts
from ..models import OperationError
from . import common, private_process, stage_assets

PACKAGES = ("kubeadm", "kubelet", "kubectl")
MATERIAL_STATE = Path("/var/lib/k8s-deploy/materials.json")


def desired(value):
    if not isinstance(value, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", value):
        raise OperationError("ops.upgrade.version")
    return value[1:] + "-1.1"


def observe(parameters):
    if not isinstance(parameters, dict) or set(parameters) != {"version"}:
        raise OperationError("ops.change.invalid_parameters")
    package = desired(parameters["version"])
    # Cross-ID activation is not an upgrade capability. Refuse before drain;
    # global apt-cache entries cannot prove the active bundle has target packages.
    if MATERIAL_STATE.exists() or MATERIAL_STATE.is_symlink():
        raise OperationError("ops.materials.invalid")
    installed = {}
    available = {}
    for name in PACKAGES:
        _, version = common.run(["dpkg-query", "-W", "-f=${Version}", name])
        installed[name] = version.strip()
        _, choices = common.run(["apt-cache", "madison", name])
        available[name] = any(
            row.split("|")[1].strip() == package
            for row in choices.splitlines()
            if len(row.split("|")) == 3
        )
    if not all(available.values()):
        raise OperationError("ops.upgrade.package")
    return dict(identity=identity_facts(), installed=installed, available=available)


def apt(arguments, log):
    # Existing 0.2.0 nodes need not have the new installed runtime layout.
    # Use the exact fixed helpers distributed in the maintenance code bundle.
    script = """set -euo pipefail
export ATLAS_LOCALES_DIR="$1/locales"
source "$1/lib/i18n.sh"
source "$1/lib/packages.sh"
shift
atlas_i18n_init en
export DEBIAN_FRONTEND=noninteractive
atlas_apt "$@"
"""
    with stage_assets.installed() as assets:
        private_process.run(
            ["bash", "-c", script, "atlas-package", str(assets), *arguments], log, timeout=900
        )


def apply(parameters, run_id):
    if (
        not isinstance(parameters, dict)
        or set(parameters) != {"version", "firstControlPlane"}
        or type(parameters["firstControlPlane"]) is not bool
    ):
        raise OperationError("ops.change.invalid_parameters")
    version = parameters["version"]
    package = desired(version)
    before = observe({"version": version})
    directory = common.journal(run_id, "cluster-upgrade")
    try:
        apt(
            ["install", "-y", "--allow-change-held-packages", "kubeadm=" + package],
            directory / "apt-kubeadm.log",
        )
        common.run(["apt-mark", "hold", "kubeadm"])
        if parameters["firstControlPlane"]:
            private_process.run(
                ["kubeadm", "upgrade", "apply", version, "--yes"],
                directory / "kubeadm.log",
                timeout=900,
            )
        else:
            private_process.run(
                ["kubeadm", "upgrade", "node"], directory / "kubeadm.log", timeout=600
            )
        apt(
            [
                "install",
                "-y",
                "--allow-change-held-packages",
                "kubelet=" + package,
                "kubectl=" + package,
            ],
            directory / "apt-kubelet.log",
        )
        common.run(["apt-mark", "hold", "kubelet", "kubectl"])
        common.run(["systemctl", "daemon-reload"])
        common.run(["systemctl", "restart", "kubelet"])
        common.run(["systemctl", "is-active", "--quiet", "kubelet"])
        after = observe({"version": version})
        if any(after["installed"][name] != package for name in PACKAGES):
            raise OperationError("ops.upgrade.package")
        common.record(directory, "cluster-upgrade", "succeeded", version=version)
        return dict(version=version, before=before["installed"], after=after["installed"])
    except BaseException:
        common.record(directory, "cluster-upgrade", "failed", version=version)
        raise
