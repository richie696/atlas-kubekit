# SPDX-License-Identifier: MIT
"""Pure Helm release-history checks used by the add-on installer."""
import json
import sys


def initial_failure_needs_cleanup(raw):
    """Return true only for a single failed first-install revision.

    A failed upgrade may still have a deployed predecessor and must be handled
    by Helm normally. Never infer that a multi-revision release is disposable.
    """
    try:
        history = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("invalid Helm history") from None
    if not isinstance(history, list) or len(history) != 1:
        return False
    revision = history[0]
    return (
        isinstance(revision, dict)
        and revision.get("revision") == 1
        and revision.get("status") in ("failed", "uninstalling")
    )


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args != ["initial-failure-needs-cleanup"]:
        return 2
    print("yes" if initial_failure_needs_cleanup(sys.stdin.read()) else "no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
