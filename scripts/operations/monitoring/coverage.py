# SPDX-License-Identifier: MIT
"""Pure risk coverage assessment; observations are supplied by collectors."""
from dataclasses import dataclass
import re
from typing import Optional

from .risk_catalog import RISK_CATALOG, RISK_IDS


@dataclass(frozen=True)
class RiskObservation:
    risk_id: str
    applicability: str
    configured: bool
    data: str
    rule: str
    delivery: str
    exclusion_reason: Optional[str] = None


@dataclass(frozen=True)
class RiskCoverage:
    risk_id: str
    status: str
    reasons: tuple[str, ...]


def assess(observation):
    """A covered result requires independent positive evidence for each stage."""
    if (not isinstance(observation, RiskObservation)
            or not isinstance(observation.risk_id, str)
            or not re.fullmatch(r"R-[A-Z]+-[0-9]{2}", observation.risk_id)):
        raise ValueError("invalid risk observation")
    if observation.applicability not in ("required", "not-applicable", "unreviewed"):
        raise ValueError("invalid applicability")
    if type(observation.configured) is not bool:
        raise ValueError("invalid configuration state")
    if any(value not in ("verified", "failed", "unknown") for value in
           (observation.data, observation.rule, observation.delivery)):
        raise ValueError("invalid evidence state")
    if observation.applicability == "not-applicable":
        if not isinstance(observation.exclusion_reason, str) or not observation.exclusion_reason.strip():
            raise ValueError("exclusion reason required")
        return RiskCoverage(observation.risk_id, "not-applicable", (observation.exclusion_reason,))
    if observation.applicability == "unreviewed":
        if observation.exclusion_reason is not None:
            raise ValueError("unexpected exclusion reason")
        return RiskCoverage(observation.risk_id, "unverified", ("scope-unreviewed",))
    if observation.exclusion_reason is not None:
        raise ValueError("unexpected exclusion reason")
    if not observation.configured:
        return RiskCoverage(observation.risk_id, "uncovered", ("not-configured",))
    stages = (("data", observation.data), ("rule", observation.rule),
              ("delivery", observation.delivery))
    failures = tuple(name + "-failed" for name, state in stages if state == "failed")
    if failures:
        return RiskCoverage(observation.risk_id, "uncovered", failures)
    unknown = tuple(name + "-unverified" for name, state in stages if state == "unknown")
    if unknown:
        return RiskCoverage(observation.risk_id, "unverified", unknown)
    return RiskCoverage(observation.risk_id, "covered", ())


def assess_all(observations):
    """Reject duplicate IDs so a later observation cannot hide a gap."""
    results = tuple(assess(item) for item in observations)
    if len({item.risk_id for item in results}) != len(results):
        raise ValueError("duplicate risk id")
    return results


def parse_scope(document):
    """Validate explicit in-use and extension risk scope without guessing exclusions."""
    if not isinstance(document, dict) or set(document) != {"schemaVersion", "risks"}:
        raise ValueError("invalid scope document")
    if document["schemaVersion"] != 1 or not isinstance(document["risks"], list):
        raise ValueError("invalid scope document")
    result = {}
    for item in document["risks"]:
        if not isinstance(item, dict) or set(item) - {"riskId", "applicability", "reason"}:
            raise ValueError("invalid scope entry")
        risk_id = item.get("riskId")
        applicability = item.get("applicability")
        if not isinstance(risk_id, str) or risk_id not in RISK_IDS or risk_id in result:
            raise ValueError("unknown or duplicate risk")
        category = dict(RISK_CATALOG)[risk_id]
        if category == "required":
            raise ValueError("base risks cannot be excluded or overridden")
        if applicability == "required" and set(item) == {"riskId", "applicability"}:
            result[risk_id] = ("required", None)
        elif (applicability == "not-applicable" and set(item) == {"riskId", "applicability", "reason"}
              and isinstance(item["reason"], str) and item["reason"].strip()
              and len(item["reason"]) <= 512 and not any(ord(char) < 32 for char in item["reason"])):
            result[risk_id] = ("not-applicable", item["reason"].strip())
        else:
            raise ValueError("invalid scope applicability")
    return result
