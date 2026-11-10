# SPDX-License-Identifier: MIT
"""Run a capability under shared ownership, fresh facts and durable checkpoints."""
from ..models import OperationError, fingerprint
from .plans import load


def execute(value, confirmation, coordinator, observe, apply, validate_parameters, progress=None):
    plan = load(value)
    if confirmation != plan.id:
        raise OperationError("ops.error.confirm")
    parameters = validate_parameters(plan.parameters)
    if coordinator.client.cluster_identity() != plan.cluster_identity:
        raise OperationError("ops.change.wrong_cluster")
    with coordinator.hold(plan.id):
        old = coordinator.journal(plan.id)
        if old:
            if old.get("state") == "succeeded":
                return dict(state="already-succeeded", planId=plan.id)
            raise OperationError("ops.change.incomplete")
        observation = observe(parameters)
        if fingerprint(observation) != plan.observation_fingerprint:
            raise OperationError("ops.change.stale")
        coordinator.record(plan.id, "running", capability=plan.capability)

        def checkpoint(step):
            coordinator.assert_owner()
            coordinator.record(plan.id, "running", step)
            if progress:
                progress(step)

        try:
            result = apply(parameters, checkpoint)
            coordinator.assert_owner()
            coordinator.record(plan.id, "succeeded", "complete")
            return dict(state="succeeded", planId=plan.id, result=result)
        except BaseException as error:
            # A timed-out SSH child may still be running. Require explicit recovery.
            coordinator.retain = True
            key = error.key if isinstance(error, OperationError) else "ops.change.failed"
            state = "cancelled" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
            try:
                coordinator.record(plan.id, state, error=key)
            except OperationError:
                pass  # Retain the primary failure; owner data remains available.
            raise
