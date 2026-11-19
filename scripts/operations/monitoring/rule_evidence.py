# SPDX-License-Identifier: MIT
"""Reconcile a Prometheus endpoint's loaded rules with Kubernetes declarations."""
from collections import Counter, defaultdict
import hashlib
import json
import re


def _text(value, maximum):
    return (isinstance(value, str) and 0 < len(value) <= maximum and not any(
        ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value))


def label_fingerprint(labels):
    if not isinstance(labels, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                           for key, value in labels.items()):
        raise ValueError("invalid static rule labels")
    encoded = json.dumps(labels, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def declaration(metadata, group, name, labels):
    fields = ("namespace", "name", "uid")
    source = {}
    if isinstance(metadata, dict) and all(
            isinstance(metadata.get(field), str) and
            re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", metadata[field]) for field in fields):
        source = {"namespace": metadata["namespace"], "resource": metadata["name"], "uid": metadata["uid"],
                  "file": "-".join(metadata[field] for field in fields) + ".yaml"}
    if _text(group, 512):
        source["group"] = group
    return {**source, "name": name, "labelFingerprint": label_fingerprint(labels)}


def runtime_source(group):
    result = {}
    if _text(group.get("name"), 512):
        result["group"] = group["name"]
    path = group.get("file")
    if _text(path, 4096):
        basename = path.rsplit("/", 1)[-1]
        if _text(basename, 768):
            result["file"] = basename
    return result


def _identity(row):
    return tuple(row.get(field) for field in ("file", "group", "name", "labelFingerprint"))


def assess(declared, loaded, declared_observed, loaded_observed):
    """Require an exact, healthy match for each declaration at the selected endpoint."""
    if not declared_observed or not loaded_observed or not declared:
        return {
            "state": "unknown",
            "reason": ("rule-inventory-unverified" if not declared_observed else
                       "prometheus-rules-unverified" if not loaded_observed else "no-declared-rules"),
            "checks": [],
        }
    expected_counts = Counter(_identity(row) for row in declared)
    known = defaultdict(list)
    unknown_names = set()
    for row in loaded:
        key = _identity(row)
        if all(key):
            known[key].append(row["health"])
        else:
            unknown_names.add(row["name"])
    changed_locations = {key[:3] for key in known if key not in expected_counts}
    checks = []
    seen = set()
    for rule in declared:
        key = _identity(rule)
        if all(key):
            if key in seen:
                continue
            seen.add(key)
        matches = known.get(key, [])
        expected_count = expected_counts[key] if all(key) else 1
        if not all(key):
            state = "source-unverified"
        elif len(matches) < expected_count and rule["name"] in unknown_names:
            state = "source-unverified"
        elif len(matches) > expected_count:
            state = "multiple-matches"
        elif len(matches) < expected_count:
            state = "labels-differ" if not matches and key[:3] in changed_locations else "missing"
        elif any(health == "err" for health in matches):
            state = "unhealthy"
        elif any(health != "ok" for health in matches):
            state = "health-unverified"
        else:
            state = "healthy"
        checks.append({**rule, "state": state, "expectedCount": expected_count, "loadedCount": len(matches)})
    if any(row["state"] in ("missing", "multiple-matches", "labels-differ", "unhealthy") for row in checks):
        return {"state": "failed", "reason": "rule-load-or-health-failed", "checks": checks}
    if any(row["state"] != "healthy" for row in checks):
        return {"state": "unknown", "reason": "rule-source-or-health-unverified", "checks": checks}
    return {"state": "verified", "reason": None, "checks": checks}
