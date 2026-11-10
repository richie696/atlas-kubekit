# SPDX-License-Identifier: MIT
"""Resolve a full APT dependency closure and publish a locally signed repository."""
from email.parser import Parser
from email.utils import formatdate
import gzip
import json
from pathlib import Path
import tempfile

from ..host_actions import common, private_process
from ..models import OperationError
from .schema import artifact, digest
from . import apt_security
from scripts.lib.component_versions import catalog

BASE_PACKAGES = tuple(
    (
        "openssh-server openssh-client sudo netplan.io curl ca-certificates gnupg python3 python3-yaml "
        "wget git net-tools bind9-dnsutils lsb-release age iputils-ping netcat-openbsd socat conntrack ethtool "
        "xfsprogs e2fsprogs chrony containerd qemu-guest-agent apt-transport-https haproxy keepalived psmisc "
        "util-linux iproute2 iptables open-iscsi nfs-common"
    ).split()
)


def requirements(configuration):
    package = configuration["versions"]["kubernetes"][1:] + "-1.1"
    return [
        *BASE_PACKAGES,
        *[name + "=" + package for name in ("kubeadm", "kubelet", "kubectl")],
        "cri-tools=" + catalog()["criTools"][1:] + "-1.1",
    ]


def build(base, configuration, log):
    apt_security.require_configured()
    base = Path(base)
    repo = base / "apt"
    repo.mkdir()
    # An empty status database forces resolution of dependencies even when the
    # connected reference host already has them installed.
    private_process.run(
        [
            "apt-get",
            "-y",
            "--download-only",
            "--no-install-recommends",
            "-o",
            "Dir::State::status=/dev/null",
            "-o",
            "Dir::Cache::archives=" + str(repo),
            "-o",
            "APT::Get::List-Cleanup=0",
            *[item for option in apt_security.STRICT_OPTIONS for item in ("-o", option)],
            "install",
            *requirements(configuration),
        ],
        log,
        timeout=1800,
    )
    artifacts = []
    stanzas = []
    selected = {}
    for file in sorted(repo.glob("*.deb")):
        _, text = common.run(["dpkg-deb", "--field", str(file)])
        fields = Parser().parsestr(text)
        name, version, arch = (fields[key] for key in ("Package", "Version", "Architecture"))
        if not all((name, version, arch)) or arch not in ("amd64", "all") or name in selected:
            raise OperationError("ops.materials.invalid")
        if name == "open-iscsi" and version.split(":")[-1].startswith("2.1.12"):
            raise OperationError("ops.materials.invalid")
        selected[name] = version
        metadata = dict(
            architecture=arch,
            depends=" ".join(fields.get("Depends", "").split()),
            preDepends=" ".join(fields.get("Pre-Depends", "").split()),
        )
        artifacts.append(
            artifact(
                base, "apt/" + file.name, "apt", name, version, "configured-signed-apt", metadata
            )
        )
        stanzas.append(
            text.rstrip()
            + "\nFilename: ./"
            + file.name
            + "\nSize: "
            + str(file.stat().st_size)
            + "\nSHA256: "
            + digest(file)
            + "\n\n"
        )
    if not set(BASE_PACKAGES) | {"kubeadm", "kubelet", "kubectl", "cri-tools"} <= set(selected):
        raise OperationError("ops.materials.incomplete")
    for path in repo.iterdir():
        if path.name == "lock":
            path.unlink()
        elif path.is_dir():
            if any(path.iterdir()):
                raise OperationError("ops.materials.incomplete")
            path.rmdir()
    packages = repo / "Packages"
    packages.write_text("".join(stanzas))
    (repo / "Packages.gz").write_bytes(gzip.compress(packages.read_bytes(), mtime=0))
    release = (
        "Origin: Atlas KubeKit\nLabel: Atlas Offline Materials\nSuite: atlas-offline\n"
        "Codename: atlas-offline\nDate: "
        + formatdate(usegmt=True)
        + "\nArchitectures: amd64 all\nSHA256:\n"
    )
    for name in ("Packages", "Packages.gz"):
        file = repo / name
        release += " " + digest(file) + " " + str(file.stat().st_size) + " " + name + "\n"
    (repo / "Release").write_text(release)
    with tempfile.TemporaryDirectory(prefix=".atlas-material-key-") as temporary:
        prefix = [
            "gpg",
            "--batch",
            "--homedir",
            temporary,
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
        ]
        try:
            common.run(
                [
                    *prefix,
                    "--quick-gen-key",
                    "Atlas Offline Materials <materials@localhost>",
                    "ed25519",
                    "sign",
                    "0",
                ]
            )
            common.run([*prefix, "--output", str(repo / "repository.gpg"), "--export"])
            common.run(
                [
                    *prefix,
                    "--output",
                    str(repo / "Release.gpg"),
                    "--detach-sign",
                    str(repo / "Release"),
                ]
            )
            common.run(
                [*prefix, "--output", str(repo / "InRelease"), "--clearsign", str(repo / "Release")]
            )
        finally:
            common.run(["gpgconf", "--homedir", temporary, "--kill", "all"], check=False)
    for name in (
        "Packages",
        "Packages.gz",
        "Release",
        "InRelease",
        "Release.gpg",
        "repository.gpg",
    ):
        kind = (
            "apt-key"
            if name == "repository.gpg"
            else "apt-signature" if name in ("InRelease", "Release.gpg") else "apt-index"
        )
        artifacts.append(artifact(base, "apt/" + name, kind, name, "1", "local-signed-repository"))
    return artifacts
