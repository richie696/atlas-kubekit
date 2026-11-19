# SPDX-License-Identifier: MIT
"""Single-owner runtime for the independent observer's durable SQLite queue."""
import contextlib
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import time
from . import store, worker


@contextlib.contextmanager
def database(directory):
    """Open private state under an exclusive, non-blocking process lock."""
    path = Path(directory)
    if not path.is_absolute() or path.is_symlink() or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("invalid observer state directory")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("invalid observer state directory")
    lock_fd = os.open(str(path / "observer.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    conn = None
    try:
        lock_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid() or
                stat.S_IMODE(lock_info.st_mode) & 0o077):
            raise ValueError("invalid observer lock")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = path / "observer.sqlite"
        fd = os.open(str(db), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            db_info = os.fstat(fd)
            if (not stat.S_ISREG(db_info.st_mode) or db_info.st_uid != os.geteuid() or
                    stat.S_IMODE(db_info.st_mode) & 0o077):
                raise ValueError("invalid observer database")
        finally:
            os.close(fd)
        conn = sqlite3.connect(str(db), timeout=5)
        store.initialize(conn)
        yield conn
    finally:
        if conn is not None:
            conn.close()
        os.close(lock_fd)


def run(connection, name, url, fail_after, recover_after, interval, deliver, *, stop, sleep=time.sleep,
        probe_fn=None, on_ready=lambda: None, on_tick=lambda: None):
    """Probe periodically and retry the oldest queued event once per interval."""
    if type(interval) is not int or not 1 <= interval <= 3600 or not callable(stop) or not callable(deliver):
        raise ValueError("invalid observer runtime policy")
    if not callable(on_ready) or not callable(on_tick):
        raise ValueError("invalid observer heartbeat")
    ready = False
    while not stop():
        worker.sample(connection, name, url, fail_after, recover_after, probe_fn=probe_fn)
        worker.deliver_one(connection, deliver)
        if not ready:
            on_ready()
            ready = True
        on_tick()
        if not stop():
            sleep(interval)
