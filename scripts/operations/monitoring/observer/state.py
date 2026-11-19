# SPDX-License-Identifier: MIT
"""Pure observer transitions; unknown observations never resolve an incident."""
from dataclasses import dataclass


@dataclass(frozen=True)
class State:
    status: str = "unknown"
    last_observation: str = "unknown"
    failures: int = 0
    recoveries: int = 0


def advance(state, observation, fail_after, recover_after):
    """Return (new state, event); observation means health, failure or unknown."""
    if (not isinstance(state, State) or state.status not in ("unknown", "healthy", "firing") or
            state.last_observation not in ("healthy", "failed", "unknown") or
            type(state.failures) is not int or type(state.recoveries) is not int or
            state.failures < 0 or state.recoveries < 0 or
            observation not in ("healthy", "failed", "unknown") or
            type(fail_after) is not int or type(recover_after) is not int or
            not 1 <= fail_after <= 1000 or not 1 <= recover_after <= 1000):
        raise ValueError("invalid observer state or policy")
    if observation == "unknown":
        return State(state.status, "unknown", 0, 0), None
    if observation == "failed":
        failures = min(fail_after, state.failures + 1) if state.last_observation == "failed" else 1
        if failures >= fail_after and state.status != "firing":
            return State("firing", "failed", failures, 0), "firing"
        return State(state.status, "failed", failures, 0), None
    recoveries = min(recover_after, state.recoveries + 1) if state.last_observation == "healthy" else 1
    if recoveries >= recover_after:
        event = "resolved" if state.status == "firing" else None
        return State("healthy", "healthy", 0, recoveries), event
    return State(state.status, "healthy", 0, recoveries), None
