# SPDX-License-Identifier: MIT
"""Resolve the locale used for alert content independently from the CLI UI."""


def notification_language(args):
    """Return the selected notification locale, with compatibility for old callers."""
    return getattr(args, "monitoring_notification_language", args.lang)
