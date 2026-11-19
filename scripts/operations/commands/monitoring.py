# SPDX-License-Identifier: MIT
"""Read-only monitoring inventory and report."""
from datetime import datetime, timezone
from ..adapters.kubernetes import Kubernetes
from ..models import Finding, OperationError
from ..monitoring.inventory import collect
from ..monitoring import prometheus
from ..monitoring import coverage_report
from ..monitoring import metric_evidence
from ..monitoring import observer_evidence
from ..reporting import write_report


def perform(args, tr, context):
    if args.target:
        raise OperationError("ops.config.no_target")
    client = Kubernetes(args.kubeconfig, args.control_target, args.ssh_identity, args.ssh_config)
    details = collect(client)
    if args.prometheus_url:
        expected = [item["name"] for item in details["nodes"]["items"]]
        details["nodeMetrics"] = prometheus.collect(args.prometheus_url, expected, args.monitoring_max_age)
    findings = [Finding("monitoring-inventory", "UNVERIFIED", "ops.monitoring.inventory",
                        (len(details["nodes"]["items"]), tr("ops.monitoring.state." + details["nodes"]["state"]),
                         tr("ops.monitoring.state." + details["monitoringNamespace"])))]
    path, _ = write_report(args.output_dir, run_id=context[0], created_at=context[1],
                           version=context[2], target=details["clusterIdentity"],
                           findings=findings, translate=tr, operation=args.action,
                           execution={"state": "observed", "result": details})
    print("[UNVERIFIED] " + tr("ops.monitoring.inventory", *findings[0].values))
    email = details["emailTemplate"]
    print(tr("ops.monitoring.email_template",
             tr("ops.monitoring.email_template_state." + email["state"]),
             tr("ops.monitoring.email_template_html." + email["html"]),
             tr("ops.monitoring.email_template_text." + email["text"]),
             email["routes"]))
    if details["monitoringNamespace"] == "present":
        counts = details["workloadCounts"]
        values = tuple(str(counts[kind]) if counts[kind] is not None else tr("ops.monitoring.state.unverified")
                       for kind in ("deployment", "statefulset", "daemonset"))
        print(tr("ops.monitoring.workloads", *values))
    for node in details["nodes"]["items"]:
        print(tr("ops.monitoring.node", node["name"], tr("ops.monitoring.state." + node["ready"].lower())))
    if args.prometheus_url:
        metric = details["nodeMetrics"]
        print(tr("ops.monitoring.metrics", tr("ops.monitoring.state." + metric["state"])))
        for node in metric["nodes"]:
            print(tr("ops.monitoring.metric_node", node["node"], tr("ops.monitoring.state." + node["state"])))
    print(tr("ops.monitoring.limit"))
    print(tr("ops.report.saved", str(path)))
    return 2


def coverage(args, tr, context):
    if args.target:
        raise OperationError("ops.config.no_target")
    profile = coverage_report.read_profile(args.monitoring_scope_file)
    scope = profile["scope"]
    client = Kubernetes(args.kubeconfig, args.control_target, args.ssh_identity, args.ssh_config)
    live_rules = (prometheus.collect_alert_rules(args.prometheus_url)
                  if args.prometheus_url else None)
    expected_targets = coverage_report.parse_expected_targets(
        ["|".join((item["scrapePool"], item["job"], item["instance"]))
         for item in profile["expectedTargets"]] + (args.monitoring_expected_target or []))
    risk_targets = coverage_report.merge_risk_targets(
        profile["riskTargets"],
        coverage_report.parse_risk_targets(args.monitoring_risk_target or []))
    risk_observers = coverage_report.merge_observer_mappings(
        profile["observerMappings"],
        observer_evidence.parse_risk_observers(args.monitoring_observer_risk or []))
    target_by_identity = {(item["scrapePool"], item["job"], item["instance"]): item
                          for item in expected_targets}
    for targets in risk_targets.values():
        for item in targets:
            identity = (item["scrapePool"], item["job"], item["instance"])
            target_by_identity.setdefault(identity, item)
    target_request = list(target_by_identity.values())
    if target_request and args.prometheus_url:
        target_inventory = prometheus.collect_targets(args.prometheus_url, target_request,
                                                      args.monitoring_max_age)
    else:
        reason = "prometheus-url-required" if target_request else "expected-targets-not-provided"
        target_inventory = {"state": "unverified", "healthy": False, "expected": [],
                            "unexpected": [], "reason": reason}
    risk_samples = metric_evidence.collect(args.prometheus_url, profile["riskMetrics"], target_inventory,
                                           args.monitoring_max_age)
    details = coverage_report.collect(client, scope, live_rules, target_inventory, risk_targets, risk_samples)
    details["independentObserverEvidence"] = observer_evidence.collect(
        args.observer_state_directory, risk_observers, args.monitoring_max_age
    )
    counts = details["counts"]
    values = (counts["covered"], counts["uncovered"], counts["unverified"], counts["not-applicable"])
    findings = [Finding("monitoring-coverage", "UNVERIFIED", "ops.monitoring.coverage_summary", values)]
    path, _ = write_report(args.output_dir, run_id=context[0], created_at=context[1],
                           version=context[2], target=client.cluster_identity(), findings=findings,
                           translate=tr, operation=args.action,
                           execution={"state": "observed", "result": details})
    print("[UNVERIFIED] " + tr("ops.monitoring.coverage_summary", *values))
    reason_codes = {"not-configured", "data-unverified", "data-failed", "rule-unverified", "delivery-unverified",
                    "rule-failed", "scope-unreviewed", "configuration-unverified"}
    for risk in details["risks"]:
        status = tr("ops.monitoring.coverage.status." + risk["status"])
        reasons = ", ".join(tr("ops.monitoring.coverage.reason." + reason)
                             if reason in reason_codes else reason for reason in risk["reasons"])
        print(tr("ops.monitoring.coverage.row", risk["riskId"], status, reasons or "-"))
        for check in risk.get("ruleEvidence", {}).get("checks", ()):
            if check["state"] == "healthy":
                continue
            state = tr("ops.monitoring.risk_rule.state." + check["state"])
            print(tr("ops.monitoring.risk_rule.row", risk["riskId"], check.get("namespace", "-"),
                     check.get("resource", "-"), check.get("group", "-"), check["name"],
                     state, check["loadedCount"], check["expectedCount"]))
        for target in risk["dataEvidence"]["targets"]:
            state = tr("ops.monitoring.target_audit.state." + target["state"])
            print(tr("ops.monitoring.risk_target.row", risk["riskId"], target["scrapePool"],
                     target["job"], target["instance"], state))
        for sample in risk["dataEvidence"].get("samples", ()):
            state = tr("ops.monitoring.risk_sample.state." + sample["state"])
            print(tr("ops.monitoring.risk_sample.row", risk["riskId"], sample["metric"],
                     sample["job"], sample["instance"], state))
        external = details["independentObserverEvidence"]["risks"].get(risk["riskId"])
        if external:
            state = tr("ops.monitoring.observer_evidence.state." + external["state"])
            reason = external.get("reason") or "-"
            if reason != "-":
                reason = tr("ops.monitoring.observer_evidence.reason." + reason)
            probe_time = _observer_time(external.get("observedAt"))
            print(tr("ops.monitoring.observer_evidence.row", risk["riskId"], state, reason, probe_time))
            for observation in external.get("observations", []):
                state = tr("ops.monitoring.observer_evidence.state." + observation["state"])
                reason = observation.get("reason") or "-"
                if reason != "-":
                    reason = tr("ops.monitoring.observer_evidence.reason." + reason)
                print(tr("ops.monitoring.observer_evidence.probe", observation["name"], state,
                         reason, _observer_time(observation.get("observedAt"))))
    observer_service = details["independentObserverEvidence"]["service"]
    service_reason = observer_service.get("reason") or "-"
    if service_reason != "-":
        service_reason = tr("ops.monitoring.observer_evidence.reason." + service_reason)
    print(tr("ops.monitoring.observer_evidence.service",
             tr("ops.monitoring.observer_evidence.state." + observer_service["state"]),
             service_reason, _observer_time(observer_service.get("observedAt"))))
    if target_inventory["state"] == "observed":
        print(tr("ops.monitoring.target_audit_summary", target_inventory["expectedCount"],
                 target_inventory["discoveredCount"], target_inventory["issueCount"],
                 target_inventory["unexpectedCount"]))
        for target in target_inventory["expected"]:
            state = tr("ops.monitoring.target_audit.state." + target["state"])
            print(tr("ops.monitoring.target_audit.row", target["scrapePool"], target["job"],
                     target["instance"], state))
        for target in target_inventory["unexpected"]:
            state = tr("ops.monitoring.target_audit.state." + target["state"])
            print(tr("ops.monitoring.target_audit.unexpected", target["scrapePool"], target["job"],
                     target["instance"], state))
    else:
        print(tr("ops.monitoring.target_audit_unverified"))
    print(tr("ops.monitoring.coverage.limit"))
    print(tr("ops.report.saved", str(path)))
    return 2


def _observer_time(value):
    if type(value) is not int:
        return "-"
    return datetime.fromtimestamp(value, timezone.utc).isoformat()
