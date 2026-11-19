# SPDX-License-Identifier: MIT
"""Minimal Loki query_range adapter for bounded alert log correlation."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from . import log_diagnostics

TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class QueryResult:
    status: str
    lines: tuple = ()
    truncated: bool = False


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid alert timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("alert timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _range(alert, before_seconds, after_seconds):
    if (not isinstance(alert, dict) or type(before_seconds) is not int or
            type(after_seconds) is not int or not 0 <= before_seconds <= 3600 or
            not 0 <= after_seconds <= 3600):
        raise ValueError("invalid query range")
    pivot = alert.get("endsAt") if alert.get("status") == "resolved" else alert.get("startsAt")
    if not pivot or pivot.startswith("0001-"):
        pivot = alert.get("startsAt")
    center = _timestamp(pivot)
    start = int((center - timedelta(seconds=before_seconds)).timestamp() * 1_000_000_000)
    end = int((center + timedelta(seconds=after_seconds)).timestamp() * 1_000_000_000)
    if end <= start:
        end = start + 1_000_000_000
    return start, end


def query(endpoint, alert, before_seconds=300, after_seconds=60, limit=20, opener=None):
    """Query only the object labels selected by the pure diagnostics module.

    Provider failures are returned as status data so notification delivery can
    continue with an explicit diagnostic failure state.
    """
    if not endpoint:
        return QueryResult("source_missing")
    if not isinstance(alert, dict) or not isinstance(alert.get("labels"), dict):
        return QueryResult("insufficient_context")
    logql = log_diagnostics.selector(alert["labels"])
    if logql is None:
        return QueryResult("insufficient_context")
    try:
        start_ns, end_ns = _range(alert, before_seconds, after_seconds)
        url = log_diagnostics.query_url(endpoint, logql, start_ns, end_ns, limit)
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        transport = urlopen if opener is None else opener.open
        with transport(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.getcode()
            if status != 200:
                return QueryResult("query_failed")
            body = response.read(log_diagnostics.MAX_RESPONSE_BYTES + 1)
        lines, truncated = log_diagnostics.parse_loki_response(body, limit)
        return QueryResult("ok", tuple(lines), truncated)
    except (OSError, TimeoutError, ValueError, TypeError, KeyError):
        return QueryResult("query_failed")
