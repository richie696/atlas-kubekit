"""Exercise the containerd registry stage against a disposable filesystem."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/stages/configure-registry-mirror.sh"


class _RegistryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/v2/":
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_: object) -> None:
        pass


class RegistryStageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="registry-stage-")
        self.root = Path(self.temp_dir.name)
        self.containerd = self.root / "etc/containerd"
        self.containerd.mkdir(parents=True)
        (self.containerd / "config.toml").write_text(
            "[plugins.'io.containerd.cri.v1.images'.registry]\n"
            "  sandbox_image = 'registry.k8s.io/pause:3.10'\n",
            encoding="utf-8",
        )
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.systemctl_log = self.root / "systemctl.log"
        self._write_stub(
            "id", "#!/bin/sh\nprintf '0\\n'\n"
        )
        self._write_stub(
            "containerd",
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = config ] && [ \"${2:-}\" = dump ]; then\n"
            "  printf '%s\\n' \"$REGISTRY_TEST_CERTS_DIR\"\n"
            "fi\n",
        )
        self._write_stub(
            "systemctl",
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$REGISTRY_TEST_SYSTEMCTL_LOG\"\n",
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RegistryHandler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.registry = f"127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.temp_dir.cleanup()

    def _write_stub(self, name: str, source: str) -> None:
        path = self.bin_dir / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)

    def _run(self, registry: str) -> subprocess.CompletedProcess[str]:
        script = self.root / "configure-registry-mirror.sh"
        source = SCRIPT.read_text(encoding="utf-8").replace(
            "/etc/containerd", str(self.containerd)
        )
        script.write_text(source, encoding="utf-8")
        environment = {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "REGISTRY_TEST_CERTS_DIR": str(self.containerd / "certs.d"),
            "REGISTRY_TEST_SYSTEMCTL_LOG": str(self.systemctl_log),
        }
        return subprocess.run(
            ["bash", str(script), "https://mirror.example.test", registry],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )

    def test_writes_exact_registry_trust_and_restarts_only_for_config_path(self) -> None:
        first = self._run(self.registry)
        self.assertEqual(first.returncode, 0, first.stderr)

        config = (self.containerd / "config.toml").read_text(encoding="utf-8")
        self.assertIn(
            f"config_path = '{self.containerd / 'certs.d'}'",
            config,
        )
        mirror = (
            self.containerd / "certs.d/registry.k8s.io/hosts.toml"
        ).read_text(encoding="utf-8")
        self.assertIn('server = "https://registry.k8s.io"', mirror)
        self.assertIn('capabilities = ["pull", "resolve"]', mirror)
        local = (self.containerd / "certs.d" / self.registry / "hosts.toml").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'server = "http://{self.registry}"', local)
        self.assertIn('capabilities = ["pull", "resolve"]', local)
        self.assertEqual(
            self.systemctl_log.read_text(encoding="utf-8").splitlines(),
            ["restart containerd", "is-active --quiet containerd"],
        )

        second = self._run(self.registry)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            self.systemctl_log.read_text(encoding="utf-8").splitlines().count(
                "restart containerd"
            ),
            1,
        )

    def test_rejects_unowned_conflicting_authority_without_mutating_config(self) -> None:
        local_host = self.containerd / "certs.d" / self.registry
        local_host.mkdir(parents=True)
        local_file = local_host / "hosts.toml"
        local_file.write_text("server = 'somebody-elses-config'\n", encoding="utf-8")
        config_before = (self.containerd / "config.toml").read_bytes()

        result = self._run(self.registry)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicts", result.stderr)
        self.assertEqual(
            (self.containerd / "config.toml").read_bytes(), config_before
        )
        self.assertEqual(
            local_file.read_text(encoding="utf-8"),
            "server = 'somebody-elses-config'\n",
        )
        self.assertFalse(self.systemctl_log.exists())

    def test_rejects_invalid_registry_before_writing_files(self) -> None:
        result = self._run("registry.invalid:65536")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("port", result.stderr)
        self.assertNotIn("config_path", (self.containerd / "config.toml").read_text())
        self.assertFalse((self.containerd / "certs.d").exists())

    def test_resume_override_uses_declared_variable(self) -> None:
        source = (REPO / "02-deploy-cluster.sh").read_text(encoding="utf-8")
        self.assertIn(
            'if [ -n "$LOCAL_REGISTRY_HOST_OVERRIDE" ] && [ -f "$CONF" ]; then',
            source,
        )
        self.assertNotIn("$REGISTRY_HOST_OVERRIDE", source)


if __name__ == "__main__":
    unittest.main()
