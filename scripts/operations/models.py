# SPDX-License-Identifier: MIT
"""Operation-owned values and deterministic security assessment rules."""
from dataclasses import asdict, dataclass
import hashlib
import json


class OperationError(ValueError):
    def __init__(self, key, *values):
        self.key, self.values = key, values
        super().__init__(key)


@dataclass(frozen=True)
class Finding:
    check: str
    status: str
    message_key: str
    values: tuple = ()


@dataclass(frozen=True)
class OperationPlan:
    operation: str
    target_fingerprint: str
    actions: tuple[str, ...]
    scope: str = ""
    schema_version: int = 1
    resources: tuple[str, ...] = ()
    parameters: tuple[tuple[str, str], ...] = ()

    def record(self):
        return asdict(self)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def security_findings(facts):
    """Assess only supplied facts; command failures remain unverified."""
    result = []
    bootstrap = facts.get("bootstrap", {})
    if bootstrap.get("state") != "ok":
        result.append(Finding("bootstrap-sudo", "UNVERIFIED", "ops.finding.unverified"))
    elif bootstrap.get("broad"):
        result.append(Finding("bootstrap-sudo", "WARN", "ops.finding.bootstrap_retained"))
    else:
        result.append(Finding("bootstrap-sudo", "PASS", "ops.finding.bootstrap_absent"))
    pairing = facts.get("pairing", {})
    if pairing.get("state") != "ok":
        result.append(Finding("pairing", "UNVERIFIED", "ops.finding.unverified"))
    elif pairing.get("active"):
        result.append(Finding("pairing", "WARN", "ops.finding.pairing_active"))
    else:
        result.append(Finding("pairing", "PASS", "ops.finding.pairing_inactive"))
    settings = facts.get("ssh", {})
    if settings.get("state") != "ok":
        result.append(Finding("ssh-policy", "UNVERIFIED", "ops.finding.unverified"))
    else:
        wanted = dict(
            passwordauthentication="no", kbdinteractiveauthentication="no", permitrootlogin="no"
        )
        ok = all(settings.get("settings", {}).get(k) == v for k, v in wanted.items())
        result.append(
            Finding(
                "ssh-policy",
                "PASS" if ok else "WARN",
                "ops.finding.ssh_ok" if ok else "ops.finding.ssh_review",
            )
        )
    stats = facts.get("stats", {})
    if stats.get("state") != "ok":
        result.append(Finding("lb-stats", "UNVERIFIED", "ops.finding.unverified"))
    else:
        exposed = [
            v
            for v in stats.get("listeners", [])
            if v not in ("127.0.0.1:7000", "[::1]:7000", "::1:7000")
        ]
        result.append(
            Finding(
                "lb-stats",
                "WARN" if exposed else "PASS",
                "ops.finding.stats_review" if exposed else "ops.finding.stats_local",
            )
        )
    # Removing this file alone would break the existing 02/05 automation contract.
    result.append(Finding("cleanup-apply", "SKIP", "ops.finding.cleanup_plan_only"))
    return tuple(result)


def cleanup_plan(facts):
    return OperationPlan(
        "security-review",
        fingerprint(facts),
        (
            "review-retained-access",
            "review-bootstrap-sudo",
            "review-pairing",
            "review-management-ingress",
        ),
    )


def service_findings(facts):
    services = {
        "cp": ("containerd", "kubelet"),
        "worker": ("containerd", "kubelet"),
        "lb": ("haproxy", "keepalived"),
    }.get(facts.get("role"), ())
    if not services:
        return (Finding("node-role", "UNVERIFIED", "ops.finding.unverified"),)
    rows = []
    for service in services:
        state = facts.get("services", {}).get(service)
        rows.append(
            Finding(
                "service-" + service,
                "UNVERIFIED" if state is None else "PASS" if state == "active" else "FAIL",
                "ops.finding.unverified" if state is None else "ops.finding.service",
                () if state is None else (service, state),
            )
        )
    return tuple(rows)


def exit_status(findings):
    if any(f.status == "FAIL" for f in findings):
        return 1
    return 2 if any(f.status in ("WARN", "UNVERIFIED") for f in findings) else 0
