# SPDX-License-Identifier: MIT
"""Bounded, privacy-aware log query and notification helpers."""
import json
import re
from urllib.parse import urlencode, urlsplit
import ipaddress

from scripts.lib.i18n import available_locales, translate
from .channels.alertmanager import localized_alert_name

MAX_QUERY_LINES = 100
MAX_LINE_CHARS = 2048
MAX_MESSAGE_BYTES = 16384
MAX_RESPONSE_BYTES = 1_048_576
MAX_ALERT_QUERIES = 5
_BEARER = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+")
_CREDENTIAL = re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)(\s*[=:]\s*)[^\s,;]+")
_URL_SECRET = re.compile(r"(?i)([?&](?:key|token|access_token|sign)=)[^&#\s]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _message(key, lang, *values):
    if lang not in available_locales():
        raise ValueError("unsupported diagnostic language")
    value = translate(lang, "ops.monitoring.log_" + key, *values)
    return value


def status_text(status, lang="zh_CN"):
    if status not in ("query_failed", "source_missing", "insufficient_context", "no_logs", "truncated"):
        raise ValueError("invalid diagnostic status")
    return _message(status, lang)


def selector(labels):
    """Build an exact Loki stream selector from the labels Alloy actually publishes."""
    if not isinstance(labels, dict):
        return None
    namespace = labels.get("namespace", labels.get("exported_namespace"))
    pod = labels.get("pod")
    container = labels.get("container")
    node = labels.get("node")
    cluster = labels.get("cluster")
    for value in (cluster, namespace, pod, container, node):
        if value is not None and (not isinstance(value, str) or not value or len(value) > 256 or
                                  any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return None
    # Alloy's Kubernetes log source publishes `instance` as
    # namespace/pod:container. The explicit relabel config also publishes
    # `node`; namespace/pod/container are only used to build the instance key.
    if namespace and pod:
        identity = namespace + "/" + pod
        if container:
            matcher = "instance=" + json.dumps(identity + ":" + container, ensure_ascii=False)
            return "{" + ("cluster=" + json.dumps(cluster, ensure_ascii=False) + "," if cluster else "") + matcher + "}"
        pattern = "^" + re.escape(identity) + ":.+$"
        matcher = "instance=~" + json.dumps(pattern, ensure_ascii=False)
        return "{" + ("cluster=" + json.dumps(cluster, ensure_ascii=False) + "," if cluster else "") + matcher + "}"
    if node:
        matcher = "node=" + json.dumps(node, ensure_ascii=False)
        return "{" + ("cluster=" + json.dumps(cluster, ensure_ascii=False) + "," if cluster else "") + matcher + "}"
    return None


def sanitize_line(value):
    if not isinstance(value, str):
        raise ValueError("invalid log line")
    value = _CONTROL.sub("�", value).replace("\r", " ").replace("\n", " ")
    value = _BEARER.sub(r"\1[已脱敏]", value)
    value = _CREDENTIAL.sub(r"\1\2[已脱敏]", value)
    value = _URL_SECRET.sub(r"\1[已脱敏]", value)
    return value[:MAX_LINE_CHARS]


def render_lines(lines, lang="zh_CN", max_lines=20, max_bytes=8192):
    """Return localized bounded text and a truncation flag for a sequence of raw log lines."""
    if (not isinstance(lines, (list, tuple)) or type(max_lines) is not int or not 1 <= max_lines <= MAX_QUERY_LINES or
            type(max_bytes) is not int or not 256 <= max_bytes <= MAX_MESSAGE_BYTES or
            lang not in available_locales()):
        raise ValueError("invalid log rendering limits")
    safe = [sanitize_line(item) for item in lines]
    truncated = len(safe) > max_lines
    safe = safe[-max_lines:]
    if not safe:
        return status_text("no_logs", lang), False
    marker = "\n" + status_text("truncated", lang)
    prefix = _message("section", lang) + "\n"
    output = prefix + "\n".join(safe)
    while len(output.encode("utf-8")) > max_bytes and safe:
        safe.pop(0)
        truncated = True
        output = prefix + "\n".join(safe)
    if truncated:
        if len((output + marker).encode("utf-8")) <= max_bytes:
            output += marker
        else:
            while safe and len((prefix + "\n".join(safe) + marker).encode("utf-8")) > max_bytes:
                safe.pop(0)
            output = prefix + "\n".join(safe) + marker
    return output, truncated


def query_url(endpoint, logql, start_ns, end_ns, limit=20):
    """Validate an in-cluster Loki service URL and construct a bounded query_range URL."""
    if (not isinstance(endpoint, str) or len(endpoint) > 512 or type(start_ns) is not int or
            type(end_ns) is not int or start_ns <= 0 or end_ns <= start_ns or
            type(limit) is not int or not 1 <= limit <= MAX_QUERY_LINES or
            not isinstance(logql, str) or not logql.startswith("{") or len(logql) > 4096):
        raise ValueError("invalid Loki query")
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or
            parsed.query or parsed.fragment or parsed.path not in ("", "/") or
            not (parsed.hostname.endswith(".svc") or parsed.hostname.endswith(".svc.cluster.local"))):
        raise ValueError("Loki endpoint must be an in-cluster service")
    query = urlencode({"query": logql, "start": str(start_ns), "end": str(end_ns),
                       "limit": str(limit), "direction": "backward"})
    return endpoint.rstrip("/") + "/loki/api/v1/query_range?" + query


def validate_settings(settings):
    required = {"loki_endpoint", "before_seconds", "after_seconds", "max_lines", "max_message_bytes"}
    link_fields = {"grafana_explore_url", "loki_datasource_uid", "grafana_org_id"}
    if (not isinstance(settings, dict) or set(settings) - (required | link_fields) or
            not required <= set(settings)):
        raise ValueError("invalid diagnostics configuration")
    endpoint = settings["loki_endpoint"]
    if endpoint is not None:
        query_url(endpoint, '{pod="validation"}', 1, 2, settings["max_lines"])
    if (type(settings["before_seconds"]) is not int or not 0 <= settings["before_seconds"] <= 3600 or
            type(settings["after_seconds"]) is not int or not 0 <= settings["after_seconds"] <= 3600 or
            type(settings["max_lines"]) is not int or not 1 <= settings["max_lines"] <= MAX_QUERY_LINES or
            type(settings["max_message_bytes"]) is not int or
            not 256 <= settings["max_message_bytes"] <= MAX_MESSAGE_BYTES):
        raise ValueError("invalid diagnostics limits")
    result = dict(settings)
    result.setdefault("grafana_explore_url", None)
    result.setdefault("loki_datasource_uid", None)
    result.setdefault("grafana_org_id", 1)
    configured = (result["grafana_explore_url"] is not None or result["loki_datasource_uid"] is not None)
    if configured:
        base, uid = result["grafana_explore_url"], result["loki_datasource_uid"]
        if (not isinstance(base, str) or len(base) > 512 or not isinstance(uid, str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", uid) or
                type(result["grafana_org_id"]) is not int or result["grafana_org_id"] < 1):
            raise ValueError("invalid Grafana link settings")
        parsed = urlsplit(base)
        host = parsed.hostname
        try:
            address = ipaddress.ip_address(host) if host else None
        except ValueError:
            address = None
        if (parsed.scheme != "https" or not host or address is not None or parsed.username or parsed.password or
                parsed.query or parsed.fragment or parsed.path not in ("", "/") or "." not in host or
                host.lower() in ("localhost",) or host.lower().endswith((".local", ".internal", ".svc", ".test"))):
            raise ValueError("Grafana link must use a public HTTPS hostname")
    elif result["grafana_org_id"] != 1:
        raise ValueError("Grafana organization requires a configured link")
    return result


def explore_url(settings, alert):
    """Build the supported Grafana Explore pane URL only for an explicit HTTPS entry."""
    settings = validate_settings(settings)
    base = settings["grafana_explore_url"]
    if base is None:
        return None
    labels = alert.get("labels") if isinstance(alert, dict) else None
    logql = selector(labels)
    if logql is None:
        return None
    pivot = alert.get("endsAt") if alert.get("status") == "resolved" else alert.get("startsAt")
    if not pivot or pivot.startswith("0001-"):
        pivot = alert.get("startsAt")
    try:
        center = __import__("datetime").datetime.fromisoformat(pivot.replace("Z", "+00:00"))
        if center.tzinfo is None:
            return None
        center = center.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    start_ms = int((center - settings["before_seconds"]) * 1000)
    end_ms = int((center + settings["after_seconds"]) * 1000)
    datasource_uid = settings["loki_datasource_uid"]
    panes = {"1": {"datasource": datasource_uid, "queries": [{"refId": "A",
              "datasource": {"type": "loki", "uid": datasource_uid}, "expr": logql,
              "queryType": "range", "editorMode": "code"}],
              "range": {"from": str(start_ms), "to": str(end_ms)}}}
    query = urlencode({"panes": json.dumps(panes, separators=(",", ":"), ensure_ascii=False),
                       "schemaVersion": "1", "orgId": str(settings["grafana_org_id"])})
    return settings["grafana_explore_url"].rstrip("/") + "/explore?" + query


def parse_loki_response(body, max_lines=20):
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES or type(max_lines) is not int or not 1 <= max_lines <= MAX_QUERY_LINES:
        raise ValueError("invalid Loki response")
    try:
        document = json.loads(body)
        data = document["data"]
        if document.get("status") != "success" or data.get("resultType") != "streams":
            raise ValueError()
        values = []
        for stream in data["result"]:
            for entry in stream["values"]:
                if not isinstance(entry, list) or len(entry) != 2 or not isinstance(entry[1], str):
                    raise ValueError()
                values.append((int(entry[0]), entry[1]))
        values.sort(key=lambda item: item[0], reverse=True)
        selected = [line for _, line in values[:max_lines]]
        return selected, len(values) > max_lines
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid Loki response") from None


def enrich_text(notification, settings, query_logs, lang="zh_CN"):
    """Append bounded diagnostics without changing the alert delivery outcome."""
    if (not isinstance(notification, object) or not isinstance(getattr(notification, "text", None), str) or
            not callable(query_logs) or
            lang not in available_locales()):
        raise ValueError("invalid diagnostics configuration")
    settings = validate_settings(settings)
    endpoint, max_lines, max_bytes = (settings["loki_endpoint"], settings["max_lines"],
                                      settings["max_message_bytes"])
    alerts = getattr(notification, "alerts", ())
    if not isinstance(alerts, tuple):
        raise ValueError("invalid alert records")
    sections = []
    attempted = 0
    seen = set()
    for alert in alerts:
        labels = alert.get("labels", {}) if isinstance(alert, dict) else {}
        selector_value = selector(labels)
        name = localized_alert_name(alert.get("name", ""), lang) if isinstance(alert, dict) else _message("no_summary", lang)
        lines = ["\n" + _message("name", lang) + ": " + name]
        link = explore_url(settings, alert) if settings["grafana_explore_url"] else None
        if link:
            lines.append(_message("source_link", lang) + ": " + link)
        elif endpoint is None:
            lines.append(status_text("source_missing", lang))
        elif selector_value is None:
            lines.append(status_text("insufficient_context", lang))
        elif attempted >= MAX_ALERT_QUERIES:
            lines.append(status_text("truncated", lang))
        else:
            seen.add(selector_value)
            attempted += 1
            try:
                result = query_logs(endpoint, alert, settings["before_seconds"], settings["after_seconds"], max_lines)
            except Exception:
                result = None
            if result is None or getattr(result, "status", None) not in ("ok", "source_missing", "insufficient_context"):
                lines.append(status_text("query_failed", lang))
            elif result.status != "ok":
                lines.append(status_text(result.status, lang))
            else:
                rendered, was_truncated = render_lines(list(result.lines), lang, max_lines, max_bytes)
                lines.append(rendered)
                if result.truncated and not was_truncated:
                    lines.append(status_text("truncated", lang))
        sections.append("\n".join(lines))
    if not sections:
        sections.append(status_text("source_missing" if endpoint is None else "insufficient_context", lang))
    diagnostics = "\n" + "\n".join(sections)
    encoded = diagnostics.encode("utf-8")
    if len(encoded) > max_bytes:
        marker = "\n" + status_text("truncated", lang)
        budget = max(0, max_bytes - len(marker.encode("utf-8")))
        diagnostics = encoded[:budget].decode("utf-8", errors="ignore") + marker
    # Bound the complete payload as well as the log section: grouped Alertmanager
    # notifications may contain many individually valid, but collectively large,
    # annotations. Keep the alert header first and make truncation explicit.
    total = notification.text + diagnostics
    total_bytes = total.encode("utf-8")
    if len(total_bytes) > max_bytes:
        marker = "\n" + status_text("truncated", lang)
        available = max(0, max_bytes - len(marker.encode("utf-8")))
        total = total_bytes[:available].decode("utf-8", errors="ignore") + marker
    return total
