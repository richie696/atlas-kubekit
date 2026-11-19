# SPDX-License-Identifier: MIT
"""Display a local independent observer snapshot without accessing credentials."""
from datetime import datetime, timedelta, timezone
from ..models import OperationError
from ..monitoring.observer import status


def perform(args, tr):
    if args.target or not args.observer_state_directory:
        raise OperationError("ops.change.invalid_parameters")
    data = status.snapshot(args.observer_state_directory)
    print(tr("ops.monitoring.observer_owner." + data["owner"]))
    for name, state, observation in data["observations"]:
        print(tr("ops.monitoring.observer_entry", name,
                 tr("ops.monitoring.observer_state." + state),
                 tr("ops.monitoring.observer_state." + observation)))
    print(tr("ops.monitoring.observer_pending", data["pending"]))
    if data["oldest"]:
        print(tr("ops.monitoring.observer_oldest", data["oldest"][0],
                 tr("ops.monitoring.observer_state." + data["oldest"][1])))
    for name, reason, when in data["probes"]:
        timestamp = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=when)).isoformat()
        print(tr("ops.monitoring.observer_probe", name, tr("ops.monitoring.observer_reason." + reason), timestamp))
    if not data["probes"]:
        print(tr("ops.monitoring.observer_no_probe"))
    if data["delivery"]:
        name, event, outcome, when = data["delivery"]
        timestamp = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=when)).isoformat()
        print(tr("ops.monitoring.observer_delivery", name, tr("ops.monitoring.observer_state." + event),
                 tr("ops.monitoring.observer_outcome." + outcome), timestamp))
    else:
        print(tr("ops.monitoring.observer_no_delivery"))
    print(tr("ops.monitoring.observer_limit"))
    return 2
