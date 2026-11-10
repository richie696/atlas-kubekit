"""Sandbox preparation preserves unrelated TOML and rolls back a failed restart."""

from pathlib import Path
import tempfile
import unittest

from scripts.stages import containerd


class SandboxTests(unittest.TestCase):
    def source(self, kind="v2"):
        header = (
            'plugins."io.containerd.cri.v1.images".pinned_images'
            if kind == "v2"
            else 'plugins."io.containerd.grpc.v1.cri"'
        )
        field = "sandbox" if kind == "v2" else "sandbox_image"
        return (
            '[unrelated]\n  sandbox = "preserve-private-value"\n['
            + header
            + "]\n  "
            + field
            + ' = "registry.k8s.io/pause:old"\n'
        )

    def test_both_supported_layouts_preserve_unrelated_values_and_are_idempotent(self):
        for kind in ("v1", "v2"):
            source = self.source(kind)
            changed = containerd.candidate(source, kind, "registry.k8s.io/pause:target")
            self.assertEqual(containerd.projection(changed), (kind, "registry.k8s.io/pause:target"))
            self.assertIn('sandbox = "preserve-private-value"', changed)
            self.assertEqual(
                containerd.candidate(changed, kind, "registry.k8s.io/pause:target"), changed
            )

    def test_failed_restart_restores_configuration_and_keeps_private_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "config.toml"
            before = self.source()
            path.write_text(before)
            path.chmod(0o600)
            services = []

            def run(args, check=True):
                if args[0] == "containerd":
                    return Path(args[2]).read_text()
                services.append(args)
                if len(services) == 1:
                    raise containerd.ConfigurationError("ops.containerd.failed")
                return ""

            with self.assertRaises(containerd.ConfigurationError):
                containerd.align("registry.k8s.io/pause:target", path, run)
            self.assertEqual(path.read_text(), before)
            self.assertEqual(len(services), 2)
            backup = next(path.parent.glob("*.atlas-before-sandbox-*"))
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.read_text(), before)
            self.assertFalse(list(path.parent.glob(".atlas-sandbox-*")))

    def test_effective_import_override_is_rejected_before_publish_or_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "config.toml"
            before = self.source()
            path.write_text(before)
            calls = []

            def run(args, check=True):
                calls.append(args)
                return before

            with self.assertRaises(containerd.ConfigurationError):
                containerd.align("registry.k8s.io/pause:target", path, run)
            self.assertEqual(path.read_text(), before)
            self.assertTrue(all(args[0] == "containerd" for args in calls))
            self.assertFalse(list(path.parent.glob("*.atlas-before-sandbox-*")))

    def test_target_is_derived_from_kubeadm_without_accepting_command_text(self):
        calls = []

        def run(args):
            calls.append(args)
            return "registry.k8s.io/kube-apiserver:v1.36.6\nregistry.k8s.io/pause:target\n"

        self.assertEqual(containerd.desired("1.36.6", run), "registry.k8s.io/pause:target")
        self.assertEqual(calls[0][-1], "v1.36.6")
        with self.assertRaises(containerd.ConfigurationError):
            containerd.desired("1.36.6;id", run)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
