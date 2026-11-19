# SPDX-License-Identifier: MIT
"""Read a private observer snapshot without starting or changing the worker."""
from contextlib import closing
import fcntl
import os
from pathlib import Path
import re
import sqlite3
import stat
from .store import _REASONS


def _check(info, kind):
    if not kind(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("invalid observer state path")


def snapshot(directory):
    path = Path(directory)
    if not path.is_absolute() or path.is_symlink() or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("invalid observer state directory")
    _check(path.stat(), stat.S_ISDIR)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    lock_fd = os.open(str(path / "observer.lock"), flags)
    db_fd = None
    try:
        _check(os.fstat(lock_fd), stat.S_ISREG)
        db_fd = os.open(str(path / "observer.sqlite"), flags)
        _check(os.fstat(db_fd), stat.S_ISREG)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            owner = "not-running"
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except BlockingIOError:
            owner = "lock-held"
        with closing(sqlite3.connect((path / "observer.sqlite").as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.execute("BEGIN")
            rows = connection.execute("SELECT name, status, last_observation FROM observer_state ORDER BY name LIMIT 1001").fetchall()
            pending = connection.execute("SELECT COUNT(*) FROM observer_outbox").fetchone()[0]
            first = connection.execute("SELECT name, event FROM observer_outbox ORDER BY id LIMIT 1").fetchone()
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('observer_probe_diagnostics', 'observer_delivery_diagnostics')")}
            probes = (connection.execute("SELECT name, reason, observed_at FROM observer_probe_diagnostics ORDER BY name LIMIT 1001").fetchall()
                      if "observer_probe_diagnostics" in tables else [])
            delivery = (connection.execute("SELECT name, event, outcome, attempted_at FROM observer_delivery_diagnostics WHERE singleton=1").fetchone()
                        if "observer_delivery_diagnostics" in tables else None)
        valid_name = lambda name: isinstance(name, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name)
        valid_time = lambda value: type(value) is int and 0 <= value <= 253402300799
        if (len(rows) > 1000 or len(probes) > 1000 or type(pending) is not int or pending < 0 or
                any(not valid_name(name) or state not in ("unknown", "healthy", "firing") or
                    last not in ("unknown", "healthy", "failed") for name, state, last in rows) or
                (first is not None and (not valid_name(first[0]) or first[1] not in ("firing", "resolved"))) or
                any(not valid_name(name) or reason not in _REASONS or not valid_time(when) for name, reason, when in probes) or
                (delivery is not None and (not valid_name(delivery[0]) or delivery[1] not in ("firing", "resolved") or
                    delivery[2] not in ("accepted", "unknown", "rejected", "retryable") or not valid_time(delivery[3])))):
            raise ValueError("invalid observer state")
        return {"owner": owner, "observations": tuple(rows), "pending": pending, "oldest": first,
                "probes": tuple(probes), "delivery": delivery}
    except sqlite3.Error:
        raise ValueError("observer database unavailable") from None
    finally:
        if db_fd is not None:
            os.close(db_fd)
        os.close(lock_fd)
