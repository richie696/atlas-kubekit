# SPDX-License-Identifier: MIT
"""Read-only collection and assessment for the registered monitoring risk matrix."""
import json
from pathlib import Path

from ..models import OperationError
from . import metric_evidence, rule_evidence
from .coverage import RiskObservation, assess, parse_scope
from .observer_evidence import parse_risk_observers
from .risk_catalog import ATLAS_DEFAULT_ALERT_RISKS, CHART_DEFAULT_ALERT_RISKS, RISK_CATALOG, RISK_IDS

MAX_SCOPE_BYTES = 1024 * 1024
MAX_RISK_TARGETS = 1000
MAX_EXPECTED_TARGETS = 1000


def _bounded_text(value, maximum):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and len(value) <= maximum
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def parse_expected_targets(values):
    """Parse target identities and collapse exact repeats without guessing inventory."""
    if not isinstance(values, (list, tuple)) or len(values) > MAX_EXPECTED_TARGETS:
        raise OperationError("ops.change.invalid_parameters")
    result = []
    seen = set()
    for value in values:
        if not isinstance(value, str) or value.count("|") != 2:
            raise OperationError("ops.change.invalid_parameters")
        scrape_pool, job, instance = value.split("|", 2)
        if not (_bounded_text(scrape_pool, 512) and _bounded_text(job, 256)
                and _bounded_text(instance, 256)):
            raise OperationError("ops.change.invalid_parameters")
        identity = (scrape_pool, job, instance)
        if identity not in seen:
            seen.add(identity)
            result.append({"scrapePool": scrape_pool, "job": job, "instance": instance})
    return tuple(result)


def parse_risk_targets(values):
    """Parse explicit risk-to-scrape-target mappings without inferring scope."""
    if not isinstance(values, (list, tuple)) or len(values) > MAX_RISK_TARGETS:
        raise OperationError("ops.change.invalid_parameters")
    result = {}
    identities = set()
    for value in values:
        if not isinstance(value, str) or value.count("|") != 3:
            raise OperationError("ops.change.invalid_parameters")
        risk_id, scrape_pool, job, instance = value.split("|", 3)
        parts = (risk_id, scrape_pool, job, instance)
        if (risk_id not in RISK_IDS or any(not part or part != part.strip() for part in parts) or
                len(scrape_pool) > 512 or len(job) > 256 or len(instance) > 256 or
                any(ord(char) < 32 or ord(char) == 127
                    for part in parts for char in part)):
            raise OperationError("ops.change.invalid_parameters")
        identity = (risk_id, scrape_pool, job, instance)
        if identity in identities:
            raise OperationError("ops.change.invalid_parameters")
        identities.add(identity)
        result.setdefault(risk_id, []).append({"scrapePool": scrape_pool, "job": job,
                                               "instance": instance})
    return {risk_id: tuple(targets) for risk_id, targets in result.items()}


def merge_risk_targets(*mappings):
    merged = {}
    total = 0
    for mapping in mappings:
        for risk_id, targets in mapping.items():
            destination = merged.setdefault(risk_id, [])
            seen = {(item["scrapePool"], item["job"], item["instance"])
                    for item in destination}
            for item in targets:
                total += 1
                if total > MAX_RISK_TARGETS:
                    raise OperationError("ops.change.invalid_parameters")
                identity = (item["scrapePool"], item["job"], item["instance"])
                if identity not in seen:
                    seen.add(identity)
                    destination.append(item)
    return {risk_id: tuple(targets) for risk_id, targets in merged.items()}


def merge_observer_mappings(*mappings):
    merged = {}
    total = 0
    for mapping in mappings:
        for risk_id, names in mapping.items():
            destination = merged.setdefault(risk_id, [])
            for name in names:
                total += 1
                if total > MAX_RISK_TARGETS:
                    raise OperationError("ops.change.invalid_parameters")
                if name not in destination:
                    destination.append(name)
    return {risk_id: tuple(names) for risk_id, names in merged.items()}


def parse_profile(document):
    """Validate the reusable scope and target profile while retaining schema-v1 files."""
    if (not isinstance(document, dict)
            or set(document) - {"schemaVersion", "risks", "expectedTargets", "riskTargets",
                                "observerMappings", "riskMetrics"}
            or type(document.get("schemaVersion")) is not int
            or document["schemaVersion"] != 1):
        raise OperationError("ops.monitoring.coverage_scope_invalid")
    try:
        scope = parse_scope({"schemaVersion": document["schemaVersion"],
                             "risks": document["risks"]})
    except (KeyError, ValueError, TypeError):
        raise OperationError("ops.monitoring.coverage_scope_invalid") from None

    expected_items = document.get("expectedTargets", [])
    if not isinstance(expected_items, list) or len(expected_items) > MAX_EXPECTED_TARGETS:
        raise OperationError("ops.monitoring.coverage_scope_invalid")
    expected_values = []
    for item in expected_items:
        if (not isinstance(item, dict) or set(item) != {"scrapePool", "job", "instance"}
                or not all(_bounded_text(item.get(field), limit) for field, limit in
                           (("scrapePool", 512), ("job", 256), ("instance", 256)))):
            raise OperationError("ops.monitoring.coverage_scope_invalid")
        expected_values.append("|".join((item["scrapePool"], item["job"], item["instance"])))

    risk_items = document.get("riskTargets", [])
    if not isinstance(risk_items, list) or len(risk_items) > MAX_RISK_TARGETS:
        raise OperationError("ops.monitoring.coverage_scope_invalid")
    risk_values = []
    for item in risk_items:
        if (not isinstance(item, dict)
                or set(item) != {"riskId", "scrapePool", "job", "instance"}
                or not isinstance(item.get("riskId"), str)
                or not all(_bounded_text(item.get(field), limit) for field, limit in
                           (("scrapePool", 512), ("job", 256), ("instance", 256)))):
            raise OperationError("ops.monitoring.coverage_scope_invalid")
        risk_values.append("|".join((item["riskId"], item["scrapePool"], item["job"], item["instance"])))

    observer_items = document.get("observerMappings", [])
    if not isinstance(observer_items, list) or len(observer_items) > MAX_RISK_TARGETS:
        raise OperationError("ops.monitoring.coverage_scope_invalid")
    observer_values = []
    for item in observer_items:
        if (not isinstance(item, dict) or set(item) != {"riskId", "observerName"}
                or not isinstance(item.get("riskId"), str)
                or not isinstance(item.get("observerName"), str)):
            raise OperationError("ops.monitoring.coverage_scope_invalid")
        observer_values.append(item["riskId"] + "|" + item["observerName"])

    try:
        risk_targets = parse_risk_targets(risk_values)
        return {"scope": scope,
                "expectedTargets": parse_expected_targets(expected_values),
                "riskTargets": risk_targets,
                "riskMetrics": metric_evidence.parse_profile(document.get("riskMetrics", []), risk_targets),
                "observerMappings": parse_risk_observers(observer_values)}
    except (ValueError, TypeError):
        raise OperationError("ops.monitoring.coverage_scope_invalid") from None


def read_scope(path):
    """Read only the applicability scope from either supported profile shape."""
    return read_profile(path)["scope"]


def read_profile(path):
    if path is None:
        return {"scope": {}, "expectedTargets": (), "riskTargets": {}, "riskMetrics": {}, "observerMappings": {}}
    source = Path(path).expanduser()
    if not source.is_absolute() or source.is_symlink():
        raise OperationError("ops.monitoring.coverage_scope_invalid")
    try:
        info = source.stat()
        if not source.is_file() or info.st_size > MAX_SCOPE_BYTES:
            raise ValueError("invalid scope file")
        return parse_profile(json.loads(source.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise OperationError("ops.monitoring.coverage_scope_invalid") from None


def _configured_rules(client):
    try:
        response = client.get("prometheusrules.monitoring.coreos.com")
        items = response["items"]
        if not isinstance(items, list):
            raise ValueError("invalid rule list")
        found = {}
        bases = {}
        declarations = {}
        for item in items:
            metadata = item.get("metadata")
            groups = item["spec"]["groups"]
            if not isinstance(groups, list):
                raise ValueError("invalid rule groups")
            for group in groups:
                rules = group["rules"]
                if not isinstance(rules, list):
                    raise ValueError("invalid rule definitions")
                for rule in rules:
                    alert_name = rule.get("alert")
                    if not isinstance(alert_name, str) or not alert_name:
                        continue
                    labels = {**group.get("labels", {}), **rule.get("labels", {})}
                    risk_id = labels.get("risk_id")
                    basis = "risk-id-label"
                    if risk_id is None:
                        risk_id = CHART_DEFAULT_ALERT_RISKS.get(alert_name)
                        basis = "known-chart-default-name"
                        if risk_id is None:
                            risk_id = ATLAS_DEFAULT_ALERT_RISKS.get(alert_name)
                            basis = "known-atlas-default-name"
                    if isinstance(risk_id, str):
                        found.setdefault(risk_id, []).append(alert_name)
                        bases.setdefault(risk_id, set()).add(basis)
                        declarations.setdefault(risk_id, []).append(
                            rule_evidence.declaration(metadata, group.get("name"), alert_name, labels))
        return {"state": "observed", "rules": found, "declarations": declarations,
                "ruleBases": {risk_id: sorted(values) for risk_id, values in bases.items()}}
    except (OperationError, AttributeError, KeyError, TypeError, ValueError):
        return {"state": "unverified", "rules": {}, "reason": "permission-or-resource-unavailable"}


def collect(client, scope, prometheus_rules=None, target_inventory=None, risk_targets=None, risk_samples=None):
    configured = _configured_rules(client)
    loaded = prometheus_rules or {"state": "unverified", "rules": {}, "reason": "prometheus-not-queried"}
    target_inventory = target_inventory or {"state": "unverified", "healthy": False,
                                            "expected": [], "unexpected": [],
                                            "reason": "expected-targets-not-provided"}
    risk_targets = risk_targets or {}
    risk_samples = risk_samples or {}
    target_states = {
        (item.get("scrapePool"), item.get("job"), item.get("instance")): item.get("state")
        for item in target_inventory.get("expected", []) if isinstance(item, dict)
    }
    results = []
    for risk_id, category in RISK_CATALOG:
        applicability, reason = scope.get(
            risk_id, ("required", None) if category == "required" else ("unreviewed", None)
        )
        names = configured["rules"].get(risk_id, [])
        rule_bases = configured.get("ruleBases", {}).get(risk_id, [])
        if configured["state"] != "observed":
            # Unknown inventory must never be mistaken for a missing rule.
            is_configured = True
        else:
            is_configured = bool(names)
        live = loaded.get("rules", {}).get(risk_id, [])
        rules = rule_evidence.assess(configured.get("declarations", {}).get(risk_id, []), live,
                                     configured["state"] == "observed", loaded.get("state") == "observed")
        mapped_targets = risk_targets.get(risk_id, ())
        samples = risk_samples.get(risk_id, [])
        evidence_targets = []
        data_state = "unknown"
        data_reason = "risk-targets-not-provided"
        target_health_state = "unknown"
        if mapped_targets:
            for target in mapped_targets:
                identity = (target["scrapePool"], target["job"], target["instance"])
                state = target_states.get(identity, "missing") if target_inventory.get("state") == "observed" else "unknown"
                evidence_targets.append({**target, "state": state})
            if target_inventory.get("state") == "observed":
                target_health_state = "healthy" if all(
                    item["state"] == "healthy" for item in evidence_targets
                ) else "failed"
                if target_health_state == "failed":
                    data_state = "failed"
                    data_reason = "risk-target-unhealthy"
                elif risk_id == "R-OBS-01":
                    # For this risk, active-target inventory is the signal being monitored.
                    data_state = "verified" if target_inventory.get("healthy") is True else "failed"
                    data_reason = None if data_state == "verified" else "risk-target-inventory-unhealthy"
                elif samples:
                    data_state, data_reason = metric_evidence.assess(samples, mapped_targets)
                else:
                    # UP/fresh lastScrape proves only endpoint reachability, not the
                    # presence or freshness of this risk's actual metric samples.
                    data_reason = "risk-metric-samples-not-queried"
            else:
                data_reason = "target-inventory-unverified"
        elif risk_id == "R-OBS-01" and target_inventory.get("state") == "observed":
            data_state = "verified" if target_inventory.get("healthy") is True else "failed"
            target_health_state = "healthy" if target_inventory.get("healthy") is True else "failed"
            data_reason = None if data_state == "verified" else "target-inventory-unhealthy"
        assessment = assess(RiskObservation(risk_id=risk_id, applicability=applicability,
                                            configured=is_configured, data=data_state,
                                            rule=rules["state"], delivery="unknown", exclusion_reason=reason))
        reasons = list(assessment.reasons)
        if configured["state"] != "observed":
            reasons.append("configuration-unverified")
        results.append({"riskId": risk_id, "category": category, "status": assessment.status,
                        "reasons": reasons, "configuredRuleNames": names,
                        "configuredRuleBasis": rule_bases,
                        "loadedRules": live, "ruleEvidence": rules,
                        "targetHealthEvidence": {
                            "state": target_health_state,
                            "basis": "prometheus-active-target-health-and-last-scrape-age",
                            "targets": evidence_targets,
                        },
                        "dataEvidence": {"state": data_state, "reason": data_reason,
                                         "targets": evidence_targets, "samples": samples}})
    counts = {status: sum(item["status"] == status for item in results)
              for status in ("covered", "uncovered", "unverified", "not-applicable")}
    return {"schemaVersion": 1, "ruleInventory": configured["state"],
            "ruleInventoryScope": "all-namespaces",
            "prometheusRules": loaded["state"], "targetInventory": target_inventory,
            "riskTargets": risk_targets, "counts": counts, "risks": results,
            "limitations": ["expected-target-list-completeness-not-proven",
                            "target-health-does-not-prove-signal-semantics",
                            "target-health-does-not-prove-risk-metric-samples",
                            "registered-samples-do-not-prove-risk-metric-selection-completeness",
                            "rule-source-match-does-not-prove-in-place-expression-revision",
                            "notification-delivery-not-proven",
                            "known-chart-default-rules-prove-rule-presence-only"]}
