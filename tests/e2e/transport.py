"""Verified SSH and bounded interactive logs; no hypervisor credentials in CI."""

import json
import os
from pathlib import Path
import selectors
import subprocess
import time

from scripts.operations.adapters.ssh import arguments


def command(lab, node, text, tty=False):
    target = "ubuntu@" + node.address
    return (
        ["ssh"]
        + (["-tt"] if tty else [])
        + arguments(target, configuration=lab.ssh_config)[1:]
        + [target, text]
    )


def run(lab, node, text, timeout=90):
    result = subprocess.run(command(lab, node, text), capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Remote prerequisite failed: " + node.name)
    if len(result.stdout) > 1024 * 1024:
        raise RuntimeError("Remote response limit")
    return result.stdout.decode().strip()


def guard(lab, node):
    marker = json.loads(run(lab, node, "sudo -k -n cat /etc/atlas-kubekit-e2e-lab.json"))
    if marker != dict(schemaVersion=1, labId=lab.id, node=node.name, vmid=node.vmid):
        raise RuntimeError("Disposable ownership marker mismatch: " + node.name)
    routes = json.loads(run(lab, node, "ip -j -4 route show default"))
    if routes and (
        not getattr(lab, "offline_gateway", None)
        or any(row.get("gateway") != lab.offline_gateway for row in routes)
    ):
        raise RuntimeError("Unexpected default gateway in offline fixture: " + node.name)


def interactive(lab, node, text, prompts, logfile, timeout):
    descriptor = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        process = subprocess.Popen(
            command(lab, node, text, True),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except BaseException:
        os.close(descriptor)
        raise
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    tail = ""
    answered = set()
    deadline = time.monotonic() + timeout
    try:
        with os.fdopen(descriptor, "wb") as stream:
            while time.monotonic() < deadline:
                if not selector.select(1):
                    if process.poll() is not None:
                        break
                    continue
                part = os.read(process.stdout.fileno(), 65536)
                if not part:
                    break
                stream.write(part)
                stream.flush()
                if stream.tell() > 64 * 1024 * 1024:
                    raise RuntimeError("Private log limit")
                tail = (tail + part.decode(errors="replace"))[-20000:]
                for key, pattern, answer in prompts:
                    if key not in answered and pattern.search(tail):
                        process.stdin.write((answer + "\n").encode())
                        process.stdin.flush()
                        answered.add(key)
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    raise RuntimeError("Interactive deadline exceeded") from None
            return process.returncode, sorted(answered)
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
