# SPDX-License-Identifier: MIT
"""Bounded private stage logs and termination of the process group we created."""
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from ..models import OperationError

MAX_LOG_BYTES = 8 * 1024 * 1024
TRUNCATED = b"\n[output truncated]\n"


def stop(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def run(args, log, timeout):
    path = Path(log)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise OperationError("ops.error.path")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    process = None
    selector = selectors.DefaultSelector()
    with os.fdopen(descriptor, "wb") as output:
        written = 0
        truncated = False
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OperationError("ops.host.uncertain")
                if not selector.select(min(remaining, 1)):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                allowed = max(0, MAX_LOG_BYTES - len(TRUNCATED) - written)
                output.write(chunk[:allowed])
                written += min(len(chunk), allowed)
                truncated |= len(chunk) > allowed
            if process.wait(timeout=max(1, deadline - time.monotonic())):
                raise OperationError("ops.host.command", Path(args[0]).name)
        except (OSError, subprocess.TimeoutExpired):
            raise OperationError("ops.host.uncertain") from None
        finally:
            if process is not None:
                stop(process)
                if process.stdout:
                    process.stdout.close()
            selector.close()
            if truncated:
                output.write(TRUNCATED)
            output.flush()
            os.fsync(output.fileno())
