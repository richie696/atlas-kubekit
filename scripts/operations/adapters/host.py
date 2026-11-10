# SPDX-License-Identifier: MIT
"""Ship a fixed code bundle over verified SSH; requests contain bounded data."""
import base64
import io
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import tempfile
import time
import zipfile

from ..models import OperationError
from .ssh import arguments
from ..host_actions.stage_assets import ASSETS

BOOTSTRAP = """import base64,json,os,sys,tempfile
from pathlib import Path
raw=sys.stdin.buffer.readline(4*1024*1024+1)
if len(raw)>4*1024*1024:raise SystemExit(1)
value=json.loads(raw)
with tempfile.TemporaryDirectory(prefix='.atlas-host-') as directory:
 path=Path(directory)/'runtime.zip';path.write_bytes(base64.b64decode(value['bundle'],validate=True));path.chmod(0o600)
 sys.path.insert(0,str(path))
 from scripts.operations.host_actions.entry import main
 raise SystemExit(main(value['request']))
"""


def bundle():
    root = Path(__file__).resolve().parents[3]
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("scripts/__init__.py", "")
        archive.writestr("scripts/lib/__init__.py", "")
        paths = [
            "scripts/operations/__init__.py",
            "scripts/operations/models.py",
            "scripts/operations/reporting.py",
            "scripts/operations/locking.py",
            "scripts/operations/collector.py",
            "scripts/operations/configuration.py",
            "scripts/operations/registry_ca.py",
            "scripts/operations/components/__init__.py",
            "scripts/operations/components/cilium_chart.py",
            "scripts/operations/components/cilium_policy.py",
            "scripts/lib/host_lock.py",
            "scripts/lib/component_versions.py",
            "scripts/lib/component-versions.json",
            "scripts/lib/archive.py",
        ]
        paths += [
            "scripts/operations/host_actions/" + p.name
            for p in sorted((root / "scripts/operations/host_actions").glob("*.py"))
        ]
        for name in paths:
            archive.writestr(name, (root / name).read_bytes())
        archive.writestr("scripts/operations/assets/__init__.py", "")
        for name in ASSETS:
            source = root / name if name.startswith("locales/") else root / "scripts" / name
            if not source.is_file() and name.startswith("stages/"):
                source = root / name.split("/", 1)[1]
            archive.writestr("scripts/operations/assets/" + name, source.read_bytes())
    return base64.b64encode(memory.getvalue()).decode()


class Host:
    def __init__(self, target, identity=None, ssh_config=None):
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.-]{0,252}", target):
            raise OperationError("ops.error.target")
        self.target = target
        self.identity = identity
        self.ssh_config = ssh_config

    def command(self):
        args = arguments(self.target, self.identity, self.ssh_config)
        return args + [self.target, shlex.join(["sudo", "-k", "-n", "python3", "-c", BOOTSTRAP])]

    def call(self, action, parameters=None, run_id="0" * 64, timeout=180):
        request = dict(schemaVersion=1, action=action, parameters=parameters or {}, runId=run_id)
        payload = json.dumps(dict(bundle=bundle(), request=request)).encode() + b"\n"
        if len(payload) > 4 * 1024 * 1024:
            raise OperationError("ops.error.limit")
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.run(
                    self.command(),
                    input=payload,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise OperationError("ops.host.uncertain") from None
            if output.tell() > 1024 * 1024:
                raise OperationError("ops.error.limit")
            output.seek(0)
            try:
                value = json.load(output)
            except ValueError:
                raise OperationError("ops.host.uncertain") from None
        if not isinstance(value, dict):
            raise OperationError("ops.host.uncertain")
        if not value.get("ok"):
            raise OperationError(value.get("error", "ops.host.failed"), *value.get("values", []))
        if process.returncode != 0:
            raise OperationError("ops.host.uncertain")
        return value["result"]

    def confirmed(self, action, parameters, run_id, probe):
        """Keep the original privileged connection while a fresh channel is tested."""
        request = dict(schemaVersion=1, action=action, parameters=parameters, runId=run_id)
        payload = json.dumps(dict(bundle=bundle(), request=request)).encode() + b"\n"
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(
                self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors
            )
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)

            def receive(timeout):
                if not selector.select(timeout):
                    raise OperationError("ops.host.uncertain")
                line = process.stdout.readline(1024 * 1024 + 1)
                if len(line) > 1024 * 1024:
                    raise OperationError("ops.error.limit")
                try:
                    value = json.loads(line)
                except ValueError:
                    raise OperationError("ops.host.uncertain") from None
                if not value.get("ok"):
                    raise OperationError(
                        value.get("error", "ops.host.failed"), *value.get("values", [])
                    )
                return value

            try:
                process.stdin.write(payload)
                process.stdin.flush()
                prepared = receive(120)
                if prepared.get("phase") != "prepared":
                    raise OperationError("ops.host.uncertain")
                try:
                    accepted = probe() is True
                except Exception:
                    accepted = False
                process.stdin.write(
                    json.dumps(dict(decision="commit" if accepted else "rollback")).encode() + b"\n"
                )
                process.stdin.flush()
                result = receive(90)
                if process.wait(timeout=15) != 0:
                    raise OperationError("ops.host.uncertain")
                return result["result"]
            finally:
                selector.close()
                process.stdin.close()
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
