# SPDX-License-Identifier: MIT
"""Adapt the independent observer's private snapshot to risk evidence."""
import math
import re
import time

from ..models import OperationError
from .observer import status
from .risk_catalog import RISK_IDS

MAX_RISK_OBSERVERS = 1000
HEALTHY_REASONS = frozenset(("expected-http-status", "dns-answer-expected"))
UNKNOWN_REASONS = frozenset(("authentication-or-redirect",))


def parse_risk_observers(values):
    """Parse explicit RISK_ID|OBSERVER_NAME links without guessing risk scope."""
    if not isinstance(values, (list, tuple)) or len(values) > MAX_RISK_OBSERVERS:
        raise OperationError("ops.change.invalid_parameters")
    result = {}
    seen = set()
    for value in values:
        if not isinstance(value, str) or value.count("|") != 1:
            raise OperationError("ops.change.invalid_parameters")
        risk_id, name = value.split("|", 1)
        if (risk_id not in RISK_IDS or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name)
                or (risk_id, name) in seen):
            raise OperationError("ops.change.invalid_parameters")
        seen.add((risk_id, name))
        result.setdefault(risk_id, []).append(name)
    return {risk_id: tuple(names) for risk_id, names in result.items()}


def _unverified(reason):
    return {"source": "independent-observer", "state": "unverified", "reason": reason,
            "observedAt": None, "observations": []}


def _service(snapshot, now, max_age):
    latest = max((item[2] for item in snapshot["probes"]), default=None)
    if snapshot["owner"] != "lock-held":
        return {"source": "independent-observer", "state": "failed", "reason": "observer-not-running",
                "observedAt": latest}
    fresh = [item for item in snapshot["probes"]
             if 0 <= now - item[2] <= max_age]
    if not fresh:
        return {"source": "independent-observer", "state": "unverified", "reason": "observer-probe-stale",
                "observedAt": latest}
    return {"source": "independent-observer", "state": "healthy", "reason": None,
            "observedAt": max(item[2] for item in fresh)}


def _observation(snapshot, name, now, max_age):
    probe = next((item for item in snapshot["probes"] if item[0] == name), None)
    if probe is None:
        return {"name": name, "state": "unverified", "reason": "observer-probe-not-found",
                "observedAt": None}
    _, reason, observed_at = probe
    age = now - observed_at
    if age < -30 or age > max_age:
        return {"name": name, "state": "unverified", "reason": "observer-probe-stale",
                "observedAt": observed_at}
    if reason in HEALTHY_REASONS:
        state = "healthy"
    elif reason in UNKNOWN_REASONS:
        state = "unverified"
    else:
        state = "failed"
    return {"name": name, "state": state, "reason": reason, "observedAt": observed_at}


def collect(state_directory, risk_observers, max_age, *, now=None):
    """Read only the observer snapshot; never change risk assessment or worker state."""
    if type(max_age) is not int or not 1 <= max_age <= 86400:
        raise OperationError("ops.change.invalid_parameters")
    if now is not None and (isinstance(now, bool) or not isinstance(now, (int, float))
                            or not math.isfinite(now)):
        raise OperationError("ops.change.invalid_parameters")
    names_by_risk = risk_observers or {}
    if state_directory is None:
        unverified = _unverified("observer-state-not-provided")
        return {"service": unverified,
                "risks": {**{risk: unverified for risk in names_by_risk}, "R-OBS-04": unverified}}
    try:
        snapshot = status.snapshot(state_directory)
    except (OSError, ValueError):
        return {"service": _unverified("observer-state-unavailable"),
                "risks": {**{risk: _unverified("observer-state-unavailable") for risk in names_by_risk},
                          "R-OBS-04": _unverified("observer-state-unavailable")}}
    observed_now = time.time() if now is None else now
    service = _service(snapshot, observed_now, max_age)
    results = {}
    for risk_id, names in names_by_risk.items():
        if service["state"] != "healthy":
            results[risk_id] = {"source": "independent-observer", "state": service["state"],
                                "reason": service["reason"], "observedAt": service["observedAt"],
                                "observations": []}
            continue
        observations = [_observation(snapshot, name, observed_now, max_age) for name in names]
        states = {item["state"] for item in observations}
        state = "failed" if "failed" in states else "unverified" if "unverified" in states else "healthy"
        observed = [item["observedAt"] for item in observations if item["observedAt"] is not None]
        results[risk_id] = {"source": "independent-observer", "state": state,
                            "reason": None if state == "healthy" else "observer-observation-incomplete",
                            "observedAt": max(observed, default=None),
                            "observations": observations}
    results.setdefault("R-OBS-04", service)
    return {"service": service, "risks": results}
