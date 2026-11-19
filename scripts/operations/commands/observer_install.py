# SPDX-License-Identifier: MIT
"""Manage the local user's independent-observer systemd service."""
from ..models import OperationError
from ..monitoring.observer import systemd


def perform(args, tr):
    if args.target:
        raise OperationError("ops.change.invalid_parameters")
    name = args.observer_name
    if args.action == "monitoring-observer-remove":
        if not name or args.confirm != name:
            raise OperationError("ops.error.confirm")
        result = systemd.remove(name)
        print(tr("ops.monitoring.observer_removed", result["name"]))
        print(tr("ops.monitoring.observer_state_preserved"))
        return 0
    if not name or not args.observer_wecom_endpoint_file:
        raise OperationError("ops.change.invalid_parameters")
    config = {
        "name": name,
        "state_directory": args.observer_state_directory,
        "url": args.observer_url,
        "ca_file": args.observer_ca_file,
        "dns_server": args.observer_dns_server,
        "dns_port": args.observer_dns_port,
        "dns_name": args.observer_dns_name,
        "dns_expected": args.observer_dns_expected,
        "wecom_endpoint_file": args.observer_wecom_endpoint_file,
        "lang": args.lang,
        "interval": args.observer_interval,
        "fail_after": args.observer_fail_after,
        "recover_after": args.observer_recover_after,
    }
    try:
        result = systemd.install(config)
    except ValueError as error:
        message = str(error)
        if message == "systemd user manager is unavailable":
            raise OperationError("ops.monitoring.observer_systemd_unavailable") from None
        if message.startswith("enable user lingering with: "):
            raise OperationError("ops.monitoring.observer_linger_required", message.removeprefix("enable user lingering with: ")) from None
        raise OperationError("ops.monitoring.observer_install_failed") from None
    print(tr("ops.monitoring.observer_installed", result["name"], result["unit"]))
    print(tr("ops.monitoring.observer_service_status", result["name"]))
    return 0
