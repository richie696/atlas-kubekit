# SPDX-License-Identifier: MIT
"""Strict, bounded Alertmanager silence inputs and API response validation."""
from datetime import datetime, timezone
import re

from ..models import OperationError

_DURATION = re.compile(r"([1-9][0-9]{0,4})(s|m|h)\Z")
_MATCHER = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)=([^\x00-\x20\x7f]{1,256})\Z")
_ALLOWED = frozenset(("alertname", "cluster", "namespace", "node", "pod", "persistentvolumeclaim",
                      "risk_id", "service", "severity", "team", "workload"))


def matchers(values):
    if not isinstance(values, (list, tuple)) or not 2 <= len(values) <= 10:
        raise ValueError("silence requires 2 to 10 matchers")
    normalized, seen = [], set()
    for value in values:
        if (isinstance(value, dict) and set(value) == {"name", "value", "isEqual", "isRegex"} and
                isinstance(value["name"], str) and isinstance(value["value"], str) and
                type(value["isEqual"]) is bool and type(value["isRegex"]) is bool and
                value["isEqual"] is True and value["isRegex"] is False):
            value = value["name"] + "=" + value["value"]
        found = _MATCHER.fullmatch(value) if isinstance(value, str) else None
        if not found:
            raise ValueError("invalid silence matcher")
        name, target = found.groups()
        if name not in _ALLOWED or name in seen:
            raise ValueError("unsupported or duplicate silence label")
        seen.add(name)
        normalized.append({"name": name, "value": target, "isEqual": True, "isRegex": False})
    if not ({"alertname", "risk_id"} & seen) or not any(
            item["name"] == "severity" and item["value"] in ("warning", "info") for item in normalized):
        raise ValueError("silence must identify a rule and explicitly exclude critical alerts")
    return sorted(normalized, key=lambda item: item["name"])


def duration(value):
    found = _DURATION.fullmatch(value) if isinstance(value, str) else None
    if not found:
        raise ValueError("invalid silence duration")
    amount, unit = int(found.group(1)), found.group(2)
    seconds = amount * {"s": 1, "m": 60, "h": 3600}[unit]
    if not 60 <= seconds <= 8 * 3600:
        raise ValueError("silence duration must be between 1 minute and 8 hours")
    return value, seconds


def api_silences(value):
    if not isinstance(value, list) or len(value) > 10000:
        raise OperationError("ops.monitoring.silence_api")
    return value


def timestamp(value):
    if not isinstance(value, str):
        raise OperationError("ops.monitoring.silence_api")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise OperationError("ops.monitoring.silence_api") from None
    if parsed.tzinfo is None:
        raise OperationError("ops.monitoring.silence_api")
    return parsed.astimezone(timezone.utc)
