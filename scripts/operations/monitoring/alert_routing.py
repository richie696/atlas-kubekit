# SPDX-License-Identifier: MIT
"""Pure validation and rendering for bounded Alertmanager route policies."""
import re


LABELS = frozenset((
    "alertname", "cluster", "instance", "job", "namespace", "node", "persistentvolumeclaim",
    "pod", "risk_id", "service", "team", "workload",
))
DEFAULT_GROUP_BY = ("cluster", "alertname", "namespace", "severity")
DEFAULT_GROUP_WAIT = "30s"
DEFAULT_GROUP_INTERVAL = "5m"
DEFAULT_REPEAT_INTERVAL = "12h"
_DURATION = re.compile(r"([1-9][0-9]{0,4})(ms|s|m|h)\Z")
_MATCHER = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(!=|=)\s*(.+)\Z")


def _seconds(value):
    match = _DURATION.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError("invalid Alertmanager duration")
    number, unit = int(match.group(1)), match.group(2)
    return number * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


def _duration(value, minimum, maximum):
    seconds = _seconds(value)
    if not minimum <= seconds <= maximum:
        raise ValueError("Alertmanager duration outside supported range")
    return value


def _matchers(values):
    if values is None:
        values = ()
    if not isinstance(values, (tuple, list)) or len(values) > 10:
        raise ValueError("invalid Alertmanager route matchers")
    rendered, seen = [], set()
    for value in values:
        if (isinstance(value, dict) and set(value) == {"name", "value", "matchType"} and
                all(isinstance(value[field], str) for field in ("name", "value", "matchType"))):
            value = value["name"] + value["matchType"] + value["value"]
        match = _MATCHER.fullmatch(value) if isinstance(value, str) else None
        if not match:
            raise ValueError("invalid Alertmanager route matcher")
        name, operator, target = match.groups()
        target = target.strip()
        if (name not in LABELS or name == "severity" or name in seen or not target or len(target) > 256 or
                any(ord(char) < 32 for char in target)):
            raise ValueError("invalid Alertmanager route matcher")
        seen.add(name)
        rendered.append({"name": name, "value": target, "matchType": operator})
    rendered.sort(key=lambda item: item["name"])
    return rendered


def route_policy(matchers=None, group_by=None, group_wait=None, group_interval=None, repeat_interval=None):
    """Return normalized data only; caller combines it with its severity matcher."""
    labels = DEFAULT_GROUP_BY if group_by is None else group_by
    if (not isinstance(labels, (tuple, list)) or not 1 <= len(labels) <= 8 or
            any(not isinstance(label, str) or label not in LABELS | {"severity"} for label in labels) or
            len(set(labels)) != len(labels) or "alertname" not in labels):
        raise ValueError("invalid Alertmanager group labels")
    wait = _duration(DEFAULT_GROUP_WAIT if group_wait is None else group_wait, 5, 300)
    interval = _duration(DEFAULT_GROUP_INTERVAL if group_interval is None else group_interval, 30, 3600)
    repeat = _duration(DEFAULT_REPEAT_INTERVAL if repeat_interval is None else repeat_interval, 60, 86400)
    if _seconds(interval) < _seconds(wait) or _seconds(repeat) < _seconds(interval):
        raise ValueError("Alertmanager group timing order is invalid")
    return {"matchers": _matchers(matchers), "groupBy": list(labels), "groupWait": wait,
            "groupInterval": interval, "repeatInterval": repeat}


def render_route(severity, policy):
    """Render CRD route fields, keeping severity controlled by its own option."""
    if severity not in ("critical", "warning", "info"):
        raise ValueError("invalid severity")
    normalized = route_policy(policy.get("matchers"), policy.get("groupBy"), policy.get("groupWait"),
                              policy.get("groupInterval"), policy.get("repeatInterval"))
    return {"matchers": [{"name": "severity", "value": severity, "matchType": "="},
                         *normalized["matchers"]],
            "groupBy": normalized["groupBy"], "groupWait": normalized["groupWait"],
            "groupInterval": normalized["groupInterval"], "repeatInterval": normalized["repeatInterval"]}


def from_arguments(args):
    return route_policy(getattr(args, "monitoring_route_matcher", None),
                        getattr(args, "monitoring_group_by", None),
                        getattr(args, "monitoring_group_wait", DEFAULT_GROUP_WAIT),
                        getattr(args, "monitoring_group_interval", DEFAULT_GROUP_INTERVAL),
                        getattr(args, "monitoring_repeat_interval", DEFAULT_REPEAT_INTERVAL))
