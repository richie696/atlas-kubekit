# SPDX-License-Identifier: MIT
"""Send a bounded, localized notification when systemd detects observer failure."""
import argparse
import re
import sys
import time

from scripts.lib.i18n import available_locales, translate
from ..channels import secret_file, wecom

_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z")


def message(lang, name):
    if lang not in available_locales() or not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("invalid observer failure notification")
    return translate(lang, "ops.monitoring.observer_message.process_failed", name)


def notify(endpoint, text, *, deliver=wecom.deliver, sleep=time.sleep):
    """Retry uncertain delivery up to three times; a platform rejection is final."""
    for attempt, delay in enumerate((0, 1, 2), start=1):
        if delay:
            sleep(delay)
        outcome = deliver(endpoint, text)
        if outcome == "accepted":
            return True
        if outcome == "rejected":
            return False
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--endpoint-file", required=True)
    parser.add_argument("--lang", choices=tuple(available_locales()), default="zh_CN")
    args = parser.parse_args()
    try:
        if not _NAME.fullmatch(args.name):
            raise ValueError()
        endpoint = wecom._endpoint(secret_file.read_private(args.endpoint_file))
        text = message(args.lang, args.name)
        if not notify(endpoint, text):
            raise ValueError()
    except (OSError, ValueError):
        parser.exit(1, "Independent observer failure notification was not accepted.\n")


if __name__ == "__main__":
    main()
