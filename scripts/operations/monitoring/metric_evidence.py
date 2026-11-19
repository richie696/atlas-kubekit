# SPDX-License-Identifier: MIT
"""Evaluate explicitly registered risk metrics against observed scrape targets."""
from concurrent.futures import ThreadPoolExecutor
import time

from . import prometheus
from .risk_catalog import RISK_IDS

TARGET_FIELDS = ("scrapePool", "job", "instance")
MAX_CHECKS = 128


def _identity(row):
    return tuple(row[field] for field in TARGET_FIELDS)


def _query_key(row):
    return row["metric"], tuple(sorted({"job": row["job"], "instance": row["instance"],
                                         **row["labels"]}.items()))


def parse_profile(items, risk_targets):
    if not isinstance(items, list) or len(items) > MAX_CHECKS:
        raise ValueError("invalid risk metric checklist")
    result = {}
    seen = set()
    fields = {*TARGET_FIELDS, "riskId", "metric"}
    for item in items:
        if (not isinstance(item, dict) or not fields <= item.keys() or item.keys() - (fields | {"labels"}) or
                not all(isinstance(item[field], str) for field in fields) or item["riskId"] not in RISK_IDS or
                not isinstance(item.get("labels", {}), dict) or
                {"job", "instance"} & item.get("labels", {}).keys()):
            raise ValueError("invalid risk metric")
        risk_id = item["riskId"]
        row = {key: item[key] for key in (*TARGET_FIELDS, "metric")}
        row["labels"] = dict(item.get("labels", {}))
        target = _identity(row)
        if not any(target == _identity(entry) for entry in risk_targets.get(risk_id, ())):
            raise ValueError("risk metric target must be declared for the same risk")
        metric, labels = _query_key(row)
        prometheus.validate_metric_selector(metric, dict(labels))
        key = risk_id, target, metric, labels
        if key in seen:
            raise ValueError("duplicate risk metric")
        seen.add(key)
        result.setdefault(risk_id, []).append(row)
    return result


def collect(base, requested, inventory, max_age):
    """Accept only uniquely identified, healthy targets and bounded exact queries."""
    if not requested:
        return {}
    targets = inventory.get("expected", []) + inventory.get("unexpected", [])
    observed = {_identity(target): target for target in targets if target["state"] != "missing"}
    peers = {}
    for target in observed.values():
        _, job, instance = _identity(target)
        peers.setdefault((job, instance), []).append(target)

    def attribution(row):
        identity = _identity(row)
        source = observed.get(identity)
        if not base or inventory.get("state") != "observed" or not source or source["state"] != "healthy":
            return "target-unavailable"
        labels = source.get("sourceLabels", {})
        selected_labels = {name: value for name, value in row["labels"].items()
                           if name in prometheus.SCRAPE_SOURCE_LABELS and name in labels}
        if any(labels[name] != value for name, value in selected_labels.items()):
            return "target-label-mismatch"
        competing = [target for target in peers[identity[1:]] if _identity(target) != identity]
        if not competing:
            return "unique-job-instance"
        for target in competing:
            other_labels = target.get("sourceLabels", {})
            if not any(name in other_labels and value != other_labels[name]
                       for name, value in selected_labels.items()):
                return "ambiguous-job-instance"
        return "distinguishing-target-labels"

    checks = {risk_id: [(row, attribution(row)) for row in rows] for risk_id, rows in requested.items()}
    proven = ("unique-job-instance", "distinguishing-target-labels")
    keys = dict.fromkeys(_query_key(row) for rows in checks.values() for row, basis in rows if basis in proven)
    results = {}
    if keys:
        now = time.time()

        def fetch(key):
            metric, labels = key
            return prometheus.collect_metric_timestamp(base, metric, dict(labels), max_age, now=now)

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = dict(zip(keys, executor.map(fetch, keys)))
    return {
        risk_id: [
            {**{key: row[key] for key in (*TARGET_FIELDS, "metric", "labels")},
             "attribution": basis,
             **(results[_query_key(row)] if basis in proven else {"state": "target-unverified"})}
            for row, basis in rows
        ]
        for risk_id, rows in checks.items()
    }


def assess(samples, targets):
    if any(row["state"] in ("missing", "stale", "ambiguous") for row in samples):
        return "failed", "risk-metric-sample-unhealthy"
    if any(row["state"] != "fresh" for row in samples):
        return "unknown", "risk-metric-query-unverified"
    if {_identity(row) for row in samples} != {_identity(target) for target in targets}:
        return "unknown", "risk-metric-targets-not-fully-mapped"
    return "verified", None
