# SPDX-License-Identifier: MIT
"""Run an independent HTTP or DNS probe with direct WeCom notification."""
import argparse
import signal
import threading
from scripts.lib.i18n import available_locales, translate
from . import dns_probe, probe, runtime, store, systemd_notify
from ..channels import secret_file, wecom


def notification_text(lang, name, event, reason=None):
    if lang not in available_locales() or not isinstance(name, str) or not name.strip() or event not in ("firing", "resolved"):
        raise ValueError("invalid observer notification")
    if reason not in store.REASONS:
        reason = "unreachable-from-observer" if event == "firing" else "expected-http-status"
    detail = translate(lang, "ops.monitoring.observer_reason." + reason)
    return translate(lang, "ops.monitoring.observer_message." + event, name, detail)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--name", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--url")
    target.add_argument("--dns-name")
    parser.add_argument("--ca-file")
    parser.add_argument("--dns-server")
    parser.add_argument("--dns-port", type=int, default=53)
    parser.add_argument("--dns-expected", action="append")
    parser.add_argument("--wecom-endpoint-file", required=True)
    parser.add_argument("--lang", choices=tuple(available_locales()), default="zh_CN")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--fail-after", type=int, default=2)
    parser.add_argument("--recover-after", type=int, default=2)
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: stop.set())
    try:
        if args.url:
            if args.dns_server or args.dns_expected:
                raise ValueError()
            probe.validate_url(args.url)
            opener = probe.build_probe_opener(args.ca_file)
            probe_fn = lambda: probe.probe(args.url, opener=opener)
        else:
            if not args.dns_server or not args.dns_expected or args.ca_file:
                raise ValueError()
            if type(args.dns_port) is not int or not 1 <= args.dns_port <= 65535:
                raise ValueError()
            dns_probe.validate(args.dns_server, args.dns_name, args.dns_expected)
            probe_fn = lambda: dns_probe.probe(args.dns_server, args.dns_name, args.dns_expected,
                                               port=args.dns_port)
        endpoint = wecom._endpoint(secret_file.read_private(args.wecom_endpoint_file))
        # Validate before opening state or starting the process loop.
        if not 1 <= args.fail_after <= 1000 or not 1 <= args.recover_after <= 1000:
            raise ValueError()
        with runtime.database(args.state_directory) as connection:
            def deliver(name, event, reason):
                return wecom.deliver(endpoint, notification_text(args.lang, name, event, reason))
            runtime.run(connection, args.name, args.url, args.fail_after, args.recover_after,
                        args.interval, deliver, stop=stop.is_set, sleep=stop.wait, probe_fn=probe_fn,
                        on_ready=lambda: systemd_notify.notify("READY=1"),
                        on_tick=lambda: systemd_notify.notify("WATCHDOG=1"))
    except (OSError, ValueError):
        parser.exit(1, "Observer configuration, state, or lock unavailable.\n")


if __name__ == "__main__":
    main()
