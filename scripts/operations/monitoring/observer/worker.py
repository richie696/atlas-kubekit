# SPDX-License-Identifier: MIT
"""Single-worker orchestration for independent probe and durable outbox."""
from . import probe, store


def sample(connection, name, url, fail_after, recover_after, *, timeout=5, expected_status=200, opener=None,
           probe_fn=None):
    result = probe_fn() if probe_fn else probe.probe(url, timeout=timeout, expected_status=expected_status, opener=opener)
    state, event = store.observe(connection, name, result["state"], fail_after, recover_after, reason=result["reason"])
    return state, event, result["reason"]


def deliver_one(connection, deliver):
    """Attempt only the oldest event; adapter errors remain pending as unknown."""
    event = store.pending(connection)
    if event is None:
        return "empty"
    event_id, name, state, reason = event
    try:
        outcome = deliver(name, state, reason)
        if outcome not in ("accepted", "unknown", "rejected", "retryable"):
            outcome = "unknown"
    except Exception:
        # An adapter exception may contain credentials; never retain it in a report.
        outcome = "unknown"
    store.acknowledge(connection, event_id, outcome)
    return outcome
