"""Portable integrity checks reject missing closure, mutation and unsafe paths."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.operations.configuration import normalize, template
from scripts.operations.materials import schema, images, source_package, apt_security, charts
from scripts.operations.materials.apt_repository import BASE_PACKAGES
from scripts.operations.models import fingerprint, OperationError


class MaterialTests(unittest.TestCase):
    def test_chart_images_are_resolved_without_a_live_kubernetes_api(self):
        rendered = """apiVersion: apps/v1
kind: DaemonSet
spec:
  template:
    spec:
      containers:
      - image: quay.io/cilium/cilium:v1.20.4@sha256:aaaaaaaa
      initContainers:
      - image: quay.io/cilium/envoy:v1
---
kind: ConfigMap
data:
  image: ignored
"""
        self.assertEqual(
            charts.image_references(rendered),
            ["quay.io/cilium/cilium:v1.20.4@sha256:aaaaaaaa", "quay.io/cilium/envoy:v1"],
        )
        with self.assertRaises(OperationError):
            charts.image_references("kind: DaemonSet\nspec: [invalid]\n")

    def fixture(self, base):
        artifacts = []
        config = normalize(template())
        version = "0.3.0-dev.2"

        def add(kind, name, release):
            path = kind + "/" + str(len(artifacts)) + ".bin"
            file = base / path
            file.parent.mkdir(exist_ok=True)
            file.write_bytes(name.encode())
            metadata = {}
            if kind == "image":
                metadata = dict(digest=release, platform="linux/amd64")
            if kind == "chart":
                metadata = dict(
                    imageReferences=[
                        "quay.io/cilium/"
                        + component
                        + ":v"
                        + config["versions"]["cilium"]
                        + "@sha256:"
                        + "a" * 64
                        for component in ("cilium", "operator-generic", "cilium-envoy")
                    ]
                )
            artifacts.append(
                schema.artifact(base, path, kind, name, release, "test-fixture", metadata)
            )

        for name in sorted(set(BASE_PACKAGES) | {"kubeadm", "kubelet", "kubectl", "cri-tools"}):
            add(
                "apt",
                name,
                (
                    config["versions"]["kubernetes"][1:] + "-1.1"
                    if name in ("kubeadm", "kubelet", "kubectl")
                    else "1.36.0-1.1" if name == "cri-tools" else "1"
                ),
            )
        for name in (
            "Packages",
            "Packages.gz",
            "Release",
            "InRelease",
            "Release.gpg",
            "repository.gpg",
        ):
            add("apt-key" if name == "repository.gpg" else "apt-index", name, "1")
        add("tool", "helm", "v3.18.6")
        add("tool", "atlas-kubekit", version)
        add("chart", "cilium", config["versions"]["cilium"])
        for name in (
            "kube-apiserver",
            "kube-controller-manager",
            "kube-scheduler",
            "kube-proxy",
            "coredns/coredns",
            "etcd",
            "pause",
        ):
            add(
                "image",
                "registry.k8s.io/" + name + ":" + config["versions"]["kubernetes"],
                "sha256:" + "a" * 64,
            )
        for name in ("cilium", "operator-generic", "cilium-envoy"):
            add(
                "image",
                "quay.io/cilium/"
                + name
                + ":v"
                + config["versions"]["cilium"]
                + "@sha256:"
                + "a" * 64,
                "sha256:" + "a" * 64,
            )
        value = dict(
            schemaVersion=1,
            toolVersion=version,
            platform=dict(schema.PLATFORM),
            configuration=config,
            artifacts=artifacts,
        )
        value["id"] = fingerprint(value)
        (base / "materials.lock.json").write_text(json.dumps(value))
        return value

    def test_mutation_missing_artifact_and_unlisted_content_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            value = self.fixture(base)
            self.assertEqual(schema.verify(base, value["id"])["id"], value["id"])
            file = base / value["artifacts"][0]["path"]
            data = file.read_bytes()
            file.write_bytes(b"changed")
            with self.assertRaises(OperationError):
                schema.verify(base, value["id"])
            file.write_bytes(data)
            (base / "unlisted-secret").write_text("exclude")
            with self.assertRaises(OperationError):
                schema.verify(base, value["id"])
            (base / "unlisted-secret").unlink()
            file.unlink()
            with self.assertRaises(OperationError):
                schema.verify(base, value["id"])

    def test_recomputed_lock_cannot_hide_a_missing_dependency_or_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            value = self.fixture(base)
            value["artifacts"] = [
                row
                for row in value["artifacts"]
                if row["name"] != "libc6" and row["name"] != "containerd"
            ]
            value["id"] = fingerprint({key: item for key, item in value.items() if key != "id"})
            with self.assertRaises(OperationError):
                schema.validate(value)
            for name in ("../escape", "/etc/shadow", "tools/../../x", "tools//x"):
                with self.assertRaises(OperationError):
                    schema.relative(name)
            self.assertEqual(
                schema.relative("apt/pkg_1%3a2.3~ubuntu+1_amd64.deb"),
                "apt/pkg_1%3a2.3~ubuntu+1_amd64.deb",
            )

    def test_digest_reference_normalization_preserves_registry_port(self):
        digest = "sha256:" + "a" * 64
        self.assertEqual(
            images.cached_reference("registry.example:5443/cilium/cilium:v1@" + digest),
            "registry.example:5443/cilium/cilium@" + digest,
        )
        self.assertEqual(
            images.cached_reference("registry.example:5443/team/image:v1"),
            "registry.example:5443/team/image:v1",
        )

    def test_source_package_has_deterministic_compression_and_no_private_test_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            for path in (first, second):
                (path / "tools").mkdir(parents=True)
            a = source_package.collect(first)
            b = source_package.collect(second)
            self.assertEqual(a["sha256"], b["sha256"])
            import tarfile

            with tarfile.open(first / a["path"]) as archive:
                self.assertIn("LICENSE", archive.getnames())
                self.assertIn(b"MIT License", archive.extractfile("LICENSE").read())
                for name in ("licenses/Helm.txt", "licenses/Cilium.txt"):
                    self.assertIn(b"Apache License", archive.extractfile(name).read())
                self.assertIn("licenses/NOTICE.md", archive.getnames())
                self.assertTrue(
                    all(
                        not name.startswith(("tests/", ".git/")) and "__pycache__" not in name
                        for name in archive.getnames()
                    )
                )

    def test_explicit_apt_trust_or_disabled_tls_cannot_be_labelled_signed_material(self):
        apt_security.check_source(
            "deb [signed-by=/usr/share/keyrings/example.gpg] https://repo.example/ stable main"
        )
        apt_security.check_source("Enabled: no\nTrusted: yes\n", True)
        for text, deb822 in (
            ("deb [trusted=yes] http://repo.example/ stable main", False),
            ("Types: deb\nURIs: https://repo.example/\nTrusted: yes\n", True),
        ):
            with self.assertRaises(OperationError):
                apt_security.check_source(text, deb822)
        with self.assertRaises(OperationError):
            apt_security.check_transport('Acquire::https::repo.example::Verify-Peer "false";')


if __name__ == "__main__":
    unittest.main()
