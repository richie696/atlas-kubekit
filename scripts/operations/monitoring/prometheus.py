# SPDX-License-Identifier: MIT
"""Bounded read-only Prometheus query; missing samples never imply recovery."""
import ipaddress
import json
import math
import re
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .risk_catalog import KNOWN_ALERT_RISKS
from .rule_evidence import label_fingerprint, runtime_source

NODE_READY_QUERY = 'kube_node_status_condition{condition="Ready",status="true"}'
NODE_TIMESTAMP_QUERY = "timestamp(" + NODE_READY_QUERY + ")"
MAX_RESPONSE_BYTES = 1024 * 1024
SCRAPE_SOURCE_LABELS = ("metrics_path", "metrics_endpoint", "probe_target")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint(base, query=NODE_READY_QUERY, when=None):
    if query not in (NODE_READY_QUERY, NODE_TIMESTAMP_QUERY):
        raise ValueError("invalid query")
    return _query_endpoint(base, query, when)


def _query_endpoint(base, query, when):
    if not isinstance(base, str) or len(base) > 2048:
        raise ValueError("invalid endpoint")
    parts = urlsplit(base)
    if (parts.scheme not in ("https", "http") or not parts.hostname or
            parts.username is not None or parts.password is not None or parts.query or parts.fragment or
            any(ord(c) < 32 for c in base) or ".." in parts.path.split("/")):
        raise ValueError("invalid endpoint")
    try:
        port = parts.port
        if parts.scheme == "http" and not ipaddress.ip_address(parts.hostname).is_loopback:
            raise ValueError("HTTP requires a loopback address")
    except ValueError:
        if parts.scheme == "http":
            raise ValueError("HTTP requires a loopback IP") from None
        raise ValueError("invalid endpoint port") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid endpoint port")
    path = parts.path.rstrip("/") + "/api/v1/query"
    params = {"query": query}
    if when is not None:
        if type(when) not in (int, float) or not math.isfinite(when):
            raise ValueError("invalid evaluation time")
        params["time"] = str(when)
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(params), ""))


def _metric_selector(metric, labels):
    if (not isinstance(metric, str) or not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]{0,199}", metric) or
            not isinstance(labels, dict) or not {"job", "instance"} <= labels.keys() or len(labels) > 10):
        raise ValueError("invalid metric selector")
    for key, value in labels.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,79}", key) or
                key.startswith("__") or not isinstance(value, str) or not value or len(value) > 256 or
                any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
            raise ValueError("invalid label matcher")
    matchers = (f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in sorted(labels.items()))
    return metric + "{" + ",".join(matchers) + "}"


def validate_metric_selector(metric, labels):
    _metric_selector(metric, labels)


def parse_metric_timestamp(payload, labels, max_age, now):
    if (payload.get("status") != "success" or payload["data"].get("resultType") != "vector" or
            not isinstance(payload["data"].get("result"), list)):
        raise ValueError("invalid timestamp response")
    entries = payload["data"]["result"]
    timestamps = []
    for item in entries:
        observed = item["metric"]
        sample = item["value"]
        if (not isinstance(observed, dict) or any(observed.get(key) != val for key, val in labels.items()) or
                not isinstance(sample, list) or len(sample) != 2):
            raise ValueError("invalid timestamp sample")
        evaluated_at, source_at = (float(value) for value in sample)
        if not math.isfinite(evaluated_at) or not math.isfinite(source_at) or abs(evaluated_at - now) > 1:
            raise ValueError("invalid timestamp")
        timestamps.append(source_at)
    if not timestamps:
        return {"state": "missing"}
    if len(timestamps) != 1:
        return {"state": "ambiguous"}
    source_at = timestamps[0]
    state = "stale" if source_at > now + 10 or now - source_at > max_age else "fresh"
    return {"state": state, "sampledAt": source_at}


def collect_metric_timestamp(base, metric, labels, max_age, now=None):
    """Query the original timestamp of an exact selector with a finite sample."""
    try:
        now = time.time() if now is None else now
        if type(max_age) is not int or not 1 <= max_age <= 86400:
            raise ValueError("invalid sample age")
        selector = _metric_selector(metric, labels)
        # Take the timestamp from the raw selector before evaluating the value filters.
        query = f"timestamp({selector}) and ({selector} < +Inf) and ({selector} > -Inf)"
        url = _query_endpoint(base, query, now)
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(Request(url, headers={"Accept": "application/json"}), timeout=5) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        return parse_metric_timestamp(json.loads(raw), labels, max_age, now)
    except HTTPError as error:
        error.close()
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError, OSError, URLError):
        pass
    return {"state": "unverified"}


def parse_vector(payload, timestamps, expected_nodes, max_age, now):
    if type(max_age) is not int or not 1 <= max_age <= 86400:
        raise ValueError("invalid sample age")
    if not isinstance(expected_nodes, (list, tuple)) or not expected_nodes:
        return {"state": "unverified", "nodes": [], "reason": "expected-nodes-unavailable"}
    if any(not isinstance(name, str) or not name for name in expected_nodes) or len(set(expected_nodes)) != len(expected_nodes):
        raise ValueError("invalid expected nodes")
    def entries(document):
        if not isinstance(document, dict) or not isinstance(document.get("data"), dict):
            raise ValueError("invalid response")
        data = document["data"]
        if document.get("status") != "success" or data.get("resultType") != "vector" or not isinstance(data.get("result"), list):
            raise ValueError("invalid response")
        result = {}
        for item in data["result"]:
            try:
                labels = item["metric"]
                sample = item["value"]
                if (not isinstance(labels, dict) or not isinstance(labels.get("node"), str) or
                        not isinstance(sample, list) or len(sample) != 2 or
                        any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items())):
                    raise ValueError()
                key = tuple(sorted((k, v) for k, v in labels.items() if k != "__name__"))
                value = float(sample[1])
                if key in result or not math.isfinite(float(sample[0])) or not math.isfinite(value):
                    raise ValueError()
                result[key] = value
            except (KeyError, TypeError, ValueError, OverflowError):
                raise ValueError("invalid sample") from None
        return result

    values = entries(payload)
    times = entries(timestamps)
    if values.keys() != times.keys():
        raise ValueError("inconsistent query results")
    by_node = {}
    for key, value in values.items():
        if value not in (0, 1):
            raise ValueError("invalid sample")
        node = dict(key)["node"]
        timestamp = times[key]
        by_node.setdefault(node, []).append((timestamp, value))
    results = []
    for node in sorted(expected_nodes):
        samples = by_node.get(node, [])
        if len(samples) != 1:
            state = "missing" if not samples else "ambiguous"
        elif samples[0][0] > now + 10 or now - samples[0][0] > max_age:
            state = "stale"
        else:
            state = "ready" if samples[0][1] == 1 else "not-ready"
        results.append({"node": node, "state": state})
    state = "observed" if all(row["state"] in ("ready", "not-ready") for row in results) else "unverified"
    return {"state": state, "nodes": results}


def collect(base, expected_nodes, max_age, now=None):
    try:
        now = time.time() if now is None else now
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        def query(expression):
            url = endpoint(base, expression, now)
            with opener.open(Request(url, headers={"Accept": "application/json"}), timeout=5) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("response too large")
            return json.loads(raw)
        return parse_vector(query(NODE_READY_QUERY), query(NODE_TIMESTAMP_QUERY), expected_nodes, max_age, now)
    except HTTPError as error:
        error.close()
        return {"state": "unverified", "nodes": [], "reason": "query-failed"}
    except (ValueError, TypeError, AttributeError, OSError, URLError, json.JSONDecodeError):
        return {"state": "unverified", "nodes": [], "reason": "query-failed"}


def rules_endpoint(base):
    """Return the bounded read-only rules API URL after validating its origin."""
    if not isinstance(base, str) or len(base) > 2048:
        raise ValueError("invalid endpoint")
    parts = urlsplit(base)
    if (parts.scheme not in ("https", "http") or not parts.hostname or
            parts.username is not None or parts.password is not None or parts.query or parts.fragment or
            any(ord(c) < 32 for c in base) or ".." in parts.path.split("/")):
        raise ValueError("invalid endpoint")
    try:
        port = parts.port
        if parts.scheme == "http" and not ipaddress.ip_address(parts.hostname).is_loopback:
            raise ValueError("HTTP requires a loopback address")
    except ValueError:
        if parts.scheme == "http":
            raise ValueError("HTTP requires a loopback IP") from None
        raise ValueError("invalid endpoint port") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid endpoint port")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/api/v1/rules", "type=alert", ""))


def parse_alert_rules(document):
    """Collect valid rule status by risk ID; malformed API payloads fail closed."""
    if (not isinstance(document, dict) or document.get("status") != "success" or
            not isinstance(document.get("data"), dict) or not isinstance(document["data"].get("groups"), list)):
        raise ValueError("invalid rules response")
    result = {}
    for group in document["data"]["groups"]:
        if not isinstance(group, dict) or not isinstance(group.get("rules"), list):
            raise ValueError("invalid rule group")
        source = runtime_source(group)
        for rule in group["rules"]:
            if not isinstance(rule, dict):
                raise ValueError("invalid rule")
            if rule.get("type") == "recording":
                continue
            labels = rule.get("labels", {})
            if not isinstance(labels, dict):
                raise ValueError("invalid rule labels")
            risk_id = labels.get("risk_id")
            if risk_id is None:
                risk_id = KNOWN_ALERT_RISKS.get(rule.get("name"))
            if risk_id is None:
                continue
            if (not isinstance(risk_id, str) or not re.fullmatch(r"R-[A-Z]+-[0-9]{2}", risk_id) or
                    not isinstance(rule.get("name"), str) or
                    rule.get("health") not in ("ok", "err", "unknown") or
                    rule.get("state") not in ("firing", "inactive", "pending")):
                raise ValueError("invalid labeled alert rule")
            result.setdefault(risk_id, []).append({
                **source, "name": rule["name"], "health": rule["health"], "state": rule["state"],
                "labelFingerprint": label_fingerprint(labels),
            })
    return result


def collect_alert_rules(base):
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(Request(rules_endpoint(base), headers={"Accept": "application/json"}), timeout=5) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        return {"state": "observed", "rules": parse_alert_rules(json.loads(raw))}
    except HTTPError as error:
        error.close()
        return {"state": "unverified", "rules": {}, "reason": "query-failed"}
    except (ValueError, TypeError, AttributeError, OSError, URLError, json.JSONDecodeError):
        return {"state": "unverified", "rules": {}, "reason": "query-failed"}


def targets_endpoint(base):
    """Return the read-only active-target API URL after validating its origin."""
    if not isinstance(base, str) or len(base) > 2048:
        raise ValueError("invalid endpoint")
    parts = urlsplit(base)
    if (parts.scheme not in ("https", "http") or not parts.hostname or
            parts.username is not None or parts.password is not None or parts.query or parts.fragment or
            any(ord(c) < 32 for c in base) or ".." in parts.path.split("/")):
        raise ValueError("invalid endpoint")
    try:
        port = parts.port
        if parts.scheme == "http" and not ipaddress.ip_address(parts.hostname).is_loopback:
            raise ValueError("HTTP requires a loopback address")
    except ValueError:
        if parts.scheme == "http":
            raise ValueError("HTTP requires a loopback IP") from None
        raise ValueError("invalid endpoint port") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid endpoint port")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/api/v1/targets",
                       "state=active", ""))


def _scrape_source_labels(labels):
    """Keep only bounded, URL-free labels used to distinguish known scrape endpoints."""
    result = {}
    for key in SCRAPE_SOURCE_LABELS:
        value = labels.get(key)
        if key == "metrics_path":
            valid = isinstance(value, str) and value in (
                "/metrics", "/metrics/cadvisor", "/metrics/probes", "/metrics/resource", "/probe",
            )
        else:
            valid = isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}", value)
        if valid:
            result[key] = value
    return result


def parse_targets(document, expected_targets, max_age, now):
    """Compare declared scrape-pool/job/instance identities with active targets."""
    if (type(max_age) is not int or not 1 <= max_age <= 86400 or
            type(now) not in (int, float) or not math.isfinite(now) or
            not isinstance(expected_targets, (list, tuple)) or not 1 <= len(expected_targets) <= 1000):
        raise ValueError("invalid target inventory request")
    expected = []
    for item in expected_targets:
        if (not isinstance(item, dict) or set(item) != {"scrapePool", "job", "instance"} or
                not isinstance(item["scrapePool"], str) or not item["scrapePool"] or
                len(item["scrapePool"]) > 512 or
                any(not isinstance(item[key], str) or not item[key] or len(item[key]) > 256 or
                    any(ord(char) < 32 or ord(char) == 127 for char in item[key])
                    for key in ("job", "instance"))):
            raise ValueError("invalid expected target")
        identity = (item["scrapePool"], item["job"], item["instance"])
        if identity in expected:
            raise ValueError("duplicate expected target")
        expected.append(identity)
    if (not isinstance(document, dict) or document.get("status") != "success" or
            not isinstance(document.get("data"), dict) or
            not isinstance(document["data"].get("activeTargets"), list)):
        raise ValueError("invalid targets response")

    discovered = {}
    for target in document["data"]["activeTargets"]:
        if not isinstance(target, dict) or not isinstance(target.get("labels"), dict):
            raise ValueError("invalid active target")
        labels = target["labels"]
        scrape_pool, job, instance, health, last_scrape = (target.get("scrapePool"), labels.get("job"),
                                                            labels.get("instance"), target.get("health"),
                                                            target.get("lastScrape"))
        if (not isinstance(scrape_pool, str) or not scrape_pool or len(scrape_pool) > 512 or
                not isinstance(job, str) or not job or len(job) > 256 or
                not isinstance(instance, str) or not instance or len(instance) > 256 or
                health not in ("up", "down", "unknown") or
                not isinstance(last_scrape, str) or len(last_scrape) > 64):
            raise ValueError("invalid active target identity")
        identity = (scrape_pool, job, instance)
        try:
            normalized_time = last_scrape.replace("Z", "+00:00")
            match = re.fullmatch(r"(.+?)(?:\.(\d+))?([+-]\d{2}:\d{2})", normalized_time)
            if match is None:
                raise ValueError("invalid RFC3339 timestamp")
            whole, fraction, zone = match.groups()
            if fraction is not None:
                normalized_time = whole + "." + (fraction + "000000")[:6] + zone
            parsed_time = datetime.fromisoformat(normalized_time)
            if parsed_time.tzinfo is None:
                raise ValueError("timezone required")
            timestamp = parsed_time.timestamp()
        except (ValueError, OverflowError):
            timestamp = None
        if timestamp is not None and not math.isfinite(timestamp):
            timestamp = None
        if health == "down":
            state = "down"
        elif health != "up" or timestamp is None:
            state = "unknown"
        elif timestamp > now + 10 or now - timestamp > max_age:
            state = "stale"
        else:
            state = "healthy"
        discovered.setdefault(identity, []).append((state, _scrape_source_labels(labels)))

    def audit_row(identity):
        scrape_pool, job, instance = identity
        observations = discovered.get(identity, [])
        state = "missing" if not observations else "duplicate" if len(observations) > 1 else observations[0][0]
        row = {"scrapePool": scrape_pool, "job": job, "instance": instance, "state": state}
        if len(observations) == 1 and observations[0][1]:
            row["sourceLabels"] = observations[0][1]
        return row

    expected_set = set(expected)
    rows = [audit_row(identity) for identity in sorted(expected_set)]
    unexpected = [audit_row(identity) for identity in sorted(discovered.keys() - expected_set)]
    healthy = all(row["state"] == "healthy" for row in rows) and not unexpected
    return {"state": "observed", "healthy": healthy, "expectedCount": len(rows),
            "discoveredCount": sum(len(states) for states in discovered.values()),
            "missingCount": sum(row["state"] == "missing" for row in rows),
            "issueCount": sum(row["state"] != "healthy" for row in rows),
            "unexpectedCount": len(unexpected), "expected": rows, "unexpected": unexpected}


def collect_targets(base, expected_targets, max_age, now=None):
    """Read Prometheus active targets without following redirects or using proxies."""
    try:
        now = time.time() if now is None else now
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(Request(targets_endpoint(base), headers={"Accept": "application/json"}), timeout=5) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        return parse_targets(json.loads(raw), expected_targets, max_age, now)
    except HTTPError as error:
        error.close()
        return {"state": "unverified", "healthy": False, "expected": [], "unexpected": [],
                "reason": "query-failed"}
    except (ValueError, TypeError, AttributeError, OSError, URLError, json.JSONDecodeError):
        return {"state": "unverified", "healthy": False, "expected": [], "unexpected": [],
                "reason": "query-failed"}
