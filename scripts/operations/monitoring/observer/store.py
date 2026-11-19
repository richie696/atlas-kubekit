# SPDX-License-Identifier: MIT
"""Transactional observer state and ordered notification outbox for one worker.

The owner supplies a private database connection and must ensure one delivery
worker per database. Delivery is at least once: a crash after external acceptance
but before acknowledgement can cause a duplicate notification.
"""
import re
import time
from .state import State, advance

_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")
REASONS = frozenset(("expected-http-status", "unexpected-http-status", "authentication-or-redirect",
                     "tls-error", "unreachable-from-observer", "dns-unreachable", "dns-invalid-response",
                     "dns-server-error", "dns-nxdomain", "dns-answer-mismatch", "dns-answer-expected"))
_REASONS = REASONS  # Kept for the read-only status adapter.


def initialize(connection):
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS observer_state (
      name TEXT PRIMARY KEY, status TEXT NOT NULL, last_observation TEXT NOT NULL,
      failures INTEGER NOT NULL, recoveries INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS observer_outbox (
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
      event TEXT NOT NULL CHECK(event IN ('firing', 'resolved')), reason TEXT
    );
    CREATE TABLE IF NOT EXISTS observer_probe_diagnostics (
      name TEXT PRIMARY KEY, reason TEXT NOT NULL, observed_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS observer_delivery_diagnostics (
      singleton INTEGER PRIMARY KEY CHECK(singleton = 1), name TEXT NOT NULL, event TEXT NOT NULL,
      outcome TEXT NOT NULL, attempted_at INTEGER NOT NULL
    );
    """)
    outbox_columns = {row[1] for row in connection.execute("PRAGMA table_info(observer_outbox)")}
    if "reason" not in outbox_columns:
        connection.execute("ALTER TABLE observer_outbox ADD COLUMN reason TEXT")


def observe(connection, name, observation, fail_after, recover_after, *, reason=None):
    """Atomically record an observation and enqueue its transition, if any."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("invalid observer name")
    if reason is not None and reason not in REASONS:
        raise ValueError("invalid observation reason")
    if connection.in_transaction:
        raise ValueError("observer connection has an active transaction")
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status, last_observation, failures, recoveries FROM observer_state WHERE name = ?", (name,)
        ).fetchone()
        current = State(*row) if row else State()
        updated, event = advance(current, observation, fail_after, recover_after)
        connection.execute(
            "INSERT INTO observer_state VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET status=excluded.status, last_observation=excluded.last_observation, "
            "failures=excluded.failures, recoveries=excluded.recoveries",
            (name, updated.status, updated.last_observation, updated.failures, updated.recoveries),
        )
        if event:
            connection.execute("INSERT INTO observer_outbox(name, event, reason) VALUES (?, ?, ?)",
                               (name, event, reason))
        if reason is not None:
            connection.execute(
                "INSERT INTO observer_probe_diagnostics VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET reason=excluded.reason, observed_at=excluded.observed_at",
                (name, reason, int(time.time())),
            )
        connection.commit()
        return updated, event
    except BaseException:
        connection.rollback()
        raise


def pending(connection):
    """Return the oldest unsent event; later events must not bypass it."""
    return connection.execute("SELECT id, name, event, reason FROM observer_outbox ORDER BY id LIMIT 1").fetchone()


def acknowledge(connection, event_id, outcome):
    """Delete only the oldest event and only after the provider accepts it."""
    if type(event_id) is not int or event_id < 1 or outcome not in ("accepted", "unknown", "rejected", "retryable"):
        raise ValueError("invalid delivery acknowledgement")
    if connection.in_transaction:
        raise ValueError("observer connection has an active transaction")
    try:
        connection.execute("BEGIN IMMEDIATE")
        first = pending(connection)
        if not first or first[0] != event_id:
            raise ValueError("outbox event is stale or out of order")
        connection.execute(
            "INSERT INTO observer_delivery_diagnostics VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET name=excluded.name, event=excluded.event, "
            "outcome=excluded.outcome, attempted_at=excluded.attempted_at",
            (first[1], first[2], outcome, int(time.time())),
        )
        if outcome == "accepted":
            connection.execute("DELETE FROM observer_outbox WHERE id = ?", (event_id,))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
