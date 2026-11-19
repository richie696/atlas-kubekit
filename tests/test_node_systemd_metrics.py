# SPDX-License-Identifier: MIT
import base64
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

from scripts.operations.adapters.host import bundle


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts/stages/node-systemd-metrics.py"
SPEC = importlib.util.spec_from_file_location("node_systemd_metrics", MODULE_PATH)
node_metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(node_metrics)


class NodeSystemdMetricsTest(unittest.TestCase):
    def test_systemd_units_render_fixed_prometheus_labels_and_states(self):
        result = Mock(returncode=0, stdout="loaded\nactive\n")
        runner = Mock(return_value=result)
        states = {unit: node_metrics._unit_state(unit, runner) for unit in node_metrics.UNITS}
        output = node_metrics.render_metrics(states)
        self.assertIn('atlas_systemd_unit_active{unit="kubelet"} 1', output)
        self.assertIn('atlas_systemd_unit_state{unit="containerd",state="active"} 1', output)
        self.assertEqual(runner.call_count, 2)

    def test_inactive_or_unreadable_unit_is_exported_without_shell_or_label_injection(self):
        replies = iter((Mock(returncode=0, stdout="loaded\nfailed\n"),
                        Mock(returncode=1, stdout="")))
        runner = Mock(side_effect=lambda *args, **kwargs: next(replies))
        states = {unit: node_metrics._unit_state(unit, runner) for unit in node_metrics.UNITS}
        output = node_metrics.render_metrics(states)
        self.assertIn('atlas_systemd_unit_active{unit="kubelet"} 0', output)
        self.assertIn('atlas_systemd_unit_state{unit="kubelet",state="failed"} 1', output)
        self.assertIn('atlas_systemd_unit_probe_success{unit="containerd"} 0', output)
        with self.assertRaises(ValueError):
            node_metrics.render_metrics({"kubelet": {"probe_success": 1, "load_state": 'loaded"} 9',
                                        "active_state": "active"},
                                       "containerd": states["containerd"]})

    def test_collection_atomically_writes_owned_metrics_and_preserves_foreign_file(self):
        runner = Mock(return_value=Mock(returncode=0, stdout="loaded\nactive\n"))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            destination = node_metrics.collect(directory, runner)
            self.assertEqual(destination.name, "atlas_systemd_units.prom")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
            self.assertTrue(destination.read_text().endswith("\n"))
            destination.write_text("foreign data\n")
            with self.assertRaisesRegex(ValueError, "foreign metrics file"):
                node_metrics.collect(directory, runner)

    def test_systemd_unit_renderer_is_idempotent_and_refuses_foreign_or_symlink_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "atlas.service"
            content = node_metrics.MARKER + "\n[Unit]\nDescription=owned\n"
            node_metrics._write_managed(path, content)
            modified = path.stat().st_mtime_ns
            self.assertFalse(node_metrics._write_managed(path, content))
            self.assertEqual(path.stat().st_mtime_ns, modified)
            path.write_text("foreign\n")
            with self.assertRaisesRegex(ValueError, "foreign systemd unit"):
                node_metrics._write_managed(path, content)
            path.unlink()
            foreign = Path(temporary) / "foreign"
            foreign.write_text(content)
            path.symlink_to(foreign)
            with self.assertRaisesRegex(ValueError, "symlinked systemd unit"):
                node_metrics._write_managed(path, content)

    def test_installation_is_wired_after_kubelet_packages_and_into_native_asset_bundle(self):
        setup = (ROOT / "scripts/stages/setup-k8s-node.sh").read_text()
        self.assertLess(setup.index("python3 \"${STORAGE_HELPER%/*}/containerd.py\""),
                        setup.index('python3 "$NODE_SYSTEMD_METRICS_HELPER" --install'))
        self.assertIn("node-systemd-metrics.py", (ROOT / "02-deploy-cluster.sh").read_text())
        assets = (ROOT / "scripts/operations/host_actions/stage_assets.py").read_text()
        self.assertIn('"stages/node-systemd-metrics.py"', assets)
        code = '''import sys
sys.path.insert(0, sys.argv[1])
from scripts.operations.host_actions.stage_assets import installed
with installed() as root:
    assert (root / "stages/node-systemd-metrics.py").is_file()
'''
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime.zip"
            runtime.write_bytes(base64.b64decode(bundle()))
            process = subprocess.run([sys.executable, "-c", code, str(runtime)], capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr.decode())


if __name__ == "__main__":
    unittest.main()
