# SPDX-License-Identifier: MIT
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from unittest.mock import patch

from scripts.operations import cli
from scripts.operations.adapters.kubernetes import Kubernetes
from scripts.operations.models import OperationError
from scripts.operations.monitoring import metric_evidence
from scripts.operations.monitoring.coverage import parse_scope
from scripts.operations.monitoring.coverage_report import (
    collect, parse_profile, parse_risk_targets, read_profile, read_scope,
)
from scripts.operations.monitoring.prometheus import parse_alert_rules, rules_endpoint
from scripts.operations.monitoring.risk_catalog import RISK_CATALOG


class Client:
    def __init__(self, denied=False, rules=None):
        self.denied = denied
        self.rules = rules

    def get(self, kind, namespace=None):
        if self.denied:
            raise OperationError("ops.change.kubernetes")
        return {"items": [{"spec": {"groups": [{"rules": self.rules or [
            {"alert": "NodeReady", "labels": {"risk_id": "R-NODE-01"}},
            {"alert": "NoRiskLabel", "labels": {"severity": "warning"}},
        ]}]}}]}


class MonitoringCoverageReportTest(unittest.TestCase):
    def test_catalog_matches_user_risk_matrix(self):
        matrix = Path(__file__).resolve().parents[1] / "docs/07-监控与预警/02-生产监控指标与告警覆盖矩阵.md"
        import re
        rows = re.findall(r"^\|\s*(R-[A-Z]+-\d{2})\s*/\s*(通用|在用|扩展)\s*\|", matrix.read_text(), re.M)
        convert = {"通用": "required", "在用": "in-use", "扩展": "extension"}
        self.assertEqual(RISK_CATALOG, tuple((risk, convert[category]) for risk, category in rows))
        self.assertEqual(len(RISK_CATALOG), 63)

    def test_scope_file_cannot_hide_base_risks_or_use_unreasoned_exclusion(self):
        scope = parse_scope({"schemaVersion": 1, "risks": [
            {"riskId": "R-LB-01", "applicability": "required"},
            {"riskId": "R-BIZ-01", "applicability": "not-applicable", "reason": "no business workloads"},
        ]})
        self.assertEqual(scope["R-LB-01"], ("required", None))
        self.assertEqual(scope["R-BIZ-01"], ("not-applicable", "no business workloads"))
        for risks in (
            [{"riskId": "R-NODE-01", "applicability": "not-applicable", "reason": "reason"}],
            [{"riskId": "R-LB-01", "applicability": "not-applicable"}],
            [{"riskId": "R-UNKNOWN-01", "applicability": "required"}],
            [{"riskId": ["R-LB-01"], "applicability": "required"}],
            [{"riskId": "R-LB-01", "applicability": "not-applicable", "reason": "bad\nreason"}],
            [{"riskId": "R-LB-01", "applicability": "required"}, {"riskId": "R-LB-01", "applicability": "required"}],
        ):
            with self.subTest(risks=risks), self.assertRaises(ValueError):
                parse_scope({"schemaVersion": 1, "risks": risks})

    def test_report_requires_configured_and_loaded_rules_and_never_claims_delivery(self):
        live = {"state": "observed", "rules": {"R-NODE-01": [{"name": "NodeReady", "health": "ok", "state": "inactive"}]}}
        result = collect(Client(), {"R-LB-01": ("required", None)}, live)
        by_id = {item["riskId"]: item for item in result["risks"]}
        self.assertEqual(by_id["R-NODE-01"]["status"], "unverified")
        self.assertIn("data-unverified", by_id["R-NODE-01"]["reasons"])
        self.assertIn("delivery-unverified", by_id["R-NODE-01"]["reasons"])
        self.assertEqual(by_id["R-LB-01"]["status"], "uncovered")
        self.assertEqual(by_id["R-BIZ-01"]["status"], "unverified")
        self.assertEqual(by_id["R-BIZ-01"]["reasons"], ["scope-unreviewed"])
        self.assertEqual(result["counts"]["covered"], 0)

    def test_risk_target_health_does_not_claim_metric_samples_are_verified(self):
        risk_targets = parse_risk_targets([
            "R-NODE-01|serviceMonitor/monitoring/node-exporter/0|node-exporter|cp1:9100",
        ])
        live = {"state": "observed", "rules": {"R-NODE-01": [
            {"name": "NodeReady", "health": "ok", "state": "inactive"}]}}
        inventory = {"state": "observed", "healthy": True, "expected": [{
            "scrapePool": "serviceMonitor/monitoring/node-exporter/0",
            "job": "node-exporter", "instance": "cp1:9100", "state": "healthy"}],
            "unexpected": []}
        result = collect(Client(), {}, live, inventory, risk_targets)
        node = next(item for item in result["risks"] if item["riskId"] == "R-NODE-01")
        self.assertEqual(node["dataEvidence"]["state"], "unknown")
        self.assertEqual(node["dataEvidence"]["reason"], "risk-metric-samples-not-queried")
        self.assertEqual(node["targetHealthEvidence"]["state"], "healthy")
        self.assertEqual(node["targetHealthEvidence"]["basis"],
                         "prometheus-active-target-health-and-last-scrape-age")
        self.assertEqual(node["dataEvidence"]["targets"][0]["state"], "healthy")
        self.assertEqual(node["status"], "unverified")
        self.assertEqual(node["reasons"], ["data-unverified", "rule-unverified", "delivery-unverified"])

    def test_rule_evidence_requires_every_declared_variant_from_the_same_resource(self):
        risk_id = "R-NODE-01"
        meta = {"namespace": "monitoring", "name": "node-rules", "uid": "original-uid"}
        group = "node.ready"
        labels = {"risk_id": risk_id}
        rules = [{"alert": "NodeReady", "labels": {"severity": severity}}
                 for severity in ("warning", "critical")]

        class DeclaredClient:
            def get(self, kind):
                return {"items": [{"metadata": meta,
                    "spec": {"groups": [{"name": group, "labels": labels, "rules": [
                        *rules, {"record": "node:ready", "labels": labels},
                    ]}]}}]}

        warning, critical = [
            {"name": item["alert"], "type": "alerting", "state": "inactive", "health": "ok",
             "labels": {**labels, **item["labels"]}} for item in rules
        ]
        source = {"file": "/etc/prometheus/rules/monitoring-node-rules-original-uid.yaml", "name": group}

        def risk(runtime_groups):
            loaded = parse_alert_rules({"status": "success", "data": {"groups": runtime_groups}})
            report = collect(DeclaredClient(), {}, {"state": "observed", "rules": loaded})
            return next(item for item in report["risks"] if item["riskId"] == risk_id)

        complete = risk([{**source, "rules": [warning, critical]}])
        self.assertEqual(complete["ruleEvidence"]["state"], "verified")
        self.assertEqual(complete["configuredRuleNames"], ["NodeReady", "NodeReady"])
        self.assertEqual(complete["status"], "unverified")
        self.assertIn("delivery-unverified", complete["reasons"])
        self.assertNotIn("/etc/prometheus", str(complete))
        for observed, expected in (
                ([{**source, "rules": [warning]}], ["healthy", "missing"]),
                ([{**source, "rules": [warning, {**critical, "labels": {**labels, "severity": "info"}}]}],
                 ["healthy", "labels-differ"]),
                ([{**source, "file": source["file"].replace("original-uid", "old-uid"),
                   "rules": [warning, critical]}], ["missing", "missing"]),
                ([{**source, "name": "another.group", "rules": [warning, critical]}], ["missing", "missing"]),
                ([{**source, "rules": [warning, warning, critical]}], ["multiple-matches", "healthy"]),
                ([{**source, "rules": [warning, {**critical, "health": "err"}]}], ["healthy", "unhealthy"]),
        ):
            with self.subTest(expected=expected):
                item = risk(observed)
                self.assertEqual([row["state"] for row in item["ruleEvidence"]["checks"]], expected)
                self.assertEqual(item["status"], "uncovered")
                self.assertIn("rule-failed", item["reasons"])
        for observed in ({"rules": [warning, critical]},
                         {**source, "rules": [warning, {**critical, "health": "unknown"}]}):
            item = risk([observed])
            self.assertEqual(item["ruleEvidence"]["state"], "unknown")
            self.assertIn("rule-unverified", item["reasons"])

    def test_metric_registration_requires_an_exact_selector_and_a_declared_risk_target(self):
        target = {"scrapePool": "pool", "job": "node", "instance": "cp1:9100"}
        entry = {"riskId": "R-NODE-01", **target, "metric": "node_uname_info",
                 "labels": {"nodename": "cp1"}}
        profile = {"schemaVersion": 1, "risks": [], "riskTargets": [{"riskId": "R-NODE-01", **target}]}
        valid = parse_profile({**profile, "riskMetrics": [entry]})
        self.assertEqual(valid["riskMetrics"]["R-NODE-01"][0]["labels"], {"nodename": "cp1"})
        for invalid in ({"instance": "other:9100"}, {"metric": "vector(1)"},
                        {"labels": {"nodename": ""}}, {"labels": {"job": "other"}},
                        {"labels": {"__name__": "node_uname_info"}}, {"labels": {"a": "bad\nvalue"}}):
            with self.subTest(invalid=invalid), self.assertRaises(OperationError):
                parse_profile({**profile, "riskMetrics": [{**entry, **invalid}]})
        with self.assertRaises(OperationError):
            parse_profile({**profile, "riskMetrics": [entry, entry]})

    def test_metric_evidence_deduplicates_queries_and_rejects_conflicting_scrape_pools(self):
        target = {"scrapePool": "pool", "job": "node", "instance": "cp1:9100"}
        request = {risk: [{**target, "metric": "node_uname_info", "labels": {"nodename": "cp1"}}]
                   for risk in ("R-NODE-01", "R-NODE-02")}
        inventory = {"state": "observed", "expected": [{**target, "state": "healthy"}], "unexpected": []}
        with patch.object(metric_evidence.prometheus, "collect_metric_timestamp",
                          return_value={"state": "fresh", "sampledAt": 90}) as query:
            result = metric_evidence.collect("http://127.0.0.1:9090", request, inventory, 120)
            query.assert_called_once()
            self.assertEqual(result["R-NODE-01"], result["R-NODE-02"])
            self.assertEqual(result["R-NODE-01"][0]["labels"], {"nodename": "cp1"})
            query.reset_mock()
            inventory["unexpected"] = [{**target, "scrapePool": "another-pool", "state": "healthy"}]
            result = metric_evidence.collect("http://127.0.0.1:9090", request, inventory, 120)
            query.assert_not_called()
            self.assertEqual(result["R-NODE-01"][0]["state"], "target-unverified")
            self.assertEqual(result["R-NODE-01"][0]["attribution"], "ambiguous-job-instance")

    def test_shared_job_and_instance_require_labels_that_distinguish_every_pool(self):
        target = {"scrapePool": "kubelet/1", "job": "kubelet", "instance": "cp1:10250"}
        selected = {"metrics_path": "/metrics/cadvisor"}
        row = {**target, "metric": "container_memory_working_set_bytes", "labels": selected}
        source = {**target, "state": "healthy", "sourceLabels": selected}
        peer = {**target, "scrapePool": "kubelet/0", "state": "down"}
        extra = {**target, "scrapePool": "kubelet/2", "state": "stale"}
        for peer_labels, labels, expected in (
            ({"metrics_path": "/metrics"}, selected, "distinguishing-target-labels"),
            ({}, selected, "ambiguous-job-instance"),
            (selected, selected, "ambiguous-job-instance"),
            ({"metrics_path": "/metrics"}, {"metrics_path": "/metrics"}, "target-label-mismatch"),
        ):
            with self.subTest(attribution=expected, peer_labels=peer_labels), patch.object(
                metric_evidence.prometheus, "collect_metric_timestamp", return_value={"state": "fresh"}
            ) as query:
                inventory = {"state": "observed", "expected": [source],
                             "unexpected": [{**peer, "sourceLabels": peer_labels},
                                            {**extra, "sourceLabels": {"metrics_path": "/metrics/probes"}}]}
                checks = metric_evidence.collect("http://127.0.0.1:9090",
                    {"R-NODE-01": [{**row, "labels": labels}]}, inventory, 120)
                result = checks["R-NODE-01"][0]
                self.assertEqual(result["attribution"], expected)
                if expected == "distinguishing-target-labels":
                    self.assertEqual(result["state"], "fresh")
                    self.assertEqual(query.call_args.args[2], {"job": target["job"],
                                                              "instance": target["instance"], **selected})
                else:
                    self.assertEqual(result["state"], "target-unverified")
                    query.assert_not_called()

    def test_metric_evidence_requires_samples_from_all_risk_targets(self):
        target = {"scrapePool": "pool", "job": "node", "instance": "cp1:9100"}
        samples = [{**target, "metric": "node_uname_info", "state": "fresh"}]
        self.assertEqual(metric_evidence.assess(samples, [target]), ("verified", None))
        self.assertEqual(metric_evidence.assess(samples, [target, {**target, "instance": "w1:9100"}]),
                         ("unknown", "risk-metric-targets-not-fully-mapped"))
        for state in ("missing", "stale", "ambiguous"):
            self.assertEqual(metric_evidence.assess([{**samples[0], "state": state}], [target])[0], "failed")

    def test_risk_specific_target_failure_marks_data_uncovered_and_notifies_scope(self):
        targets = parse_risk_targets([
            "R-NODE-01|serviceMonitor/monitoring/node-exporter/0|node-exporter|cp1:9100",
            "R-NODE-01|serviceMonitor/monitoring/node-exporter/0|node-exporter|w1:9100",
        ])
        inventory = {"state": "observed", "healthy": False, "expected": [
            {"scrapePool": "serviceMonitor/monitoring/node-exporter/0", "job": "node-exporter",
             "instance": "cp1:9100", "state": "healthy"},
            {"scrapePool": "serviceMonitor/monitoring/node-exporter/0", "job": "node-exporter",
             "instance": "w1:9100", "state": "stale"}], "unexpected": []}
        result = collect(Client(), {}, {"state": "observed", "rules": {}}, inventory, targets)
        node = next(item for item in result["risks"] if item["riskId"] == "R-NODE-01")
        self.assertEqual(node["dataEvidence"]["state"], "failed")
        self.assertEqual(node["targetHealthEvidence"]["state"], "failed")
        self.assertEqual(node["status"], "uncovered")
        self.assertIn("data-failed", node["reasons"])
        self.assertIn("risk-target-unhealthy", node["dataEvidence"]["reason"])

    def test_risk_target_parse_rejects_unknown_duplicate_and_oversized_mappings(self):
        for values in (
            ["R-NOT-99|pool|job|target:1"],
            ["R-NODE-01|pool|job|target:1", "R-NODE-01|pool|job|target:1"],
            ["R-NODE-01|pool|job"],
            ["R-NODE-01|pool\ninvalid|job|target:1"],
            ["R-NODE-01|pool| |target:1"],
            ["R-NODE-01|pool|job|target:1"] * 1001,
        ):
            with self.subTest(values=values[:2]), self.assertRaises(OperationError):
                parse_risk_targets(values)

    def test_denied_rule_inventory_is_unknown_instead_of_false_gap(self):
        result = collect(Client(denied=True), {})
        required = next(item for item in result["risks"] if item["riskId"] == "R-NODE-01")
        self.assertEqual(required["status"], "unverified")
        self.assertIn("configuration-unverified", required["reasons"])

    def test_known_chart_rules_are_mapped_to_partial_risk_evidence(self):
        chart_rules = [
            {"alert": "KubeAPIDown", "labels": {"severity": "warning"}},
            {"alert": "etcdMembersDown", "labels": {"severity": "critical"}},
        ]
        loaded = {"state": "observed", "rules": {
            "R-CP-01": [{"name": "KubeAPIDown", "health": "ok", "state": "inactive"}],
            "R-ETCD-01": [{"name": "etcdMembersDown", "health": "ok", "state": "inactive"}],
        }}
        report = collect(Client(rules=chart_rules), {}, loaded)
        by_id = {item["riskId"]: item for item in report["risks"]}
        for risk_id, rule_name in (("R-CP-01", "KubeAPIDown"),
                                   ("R-ETCD-01", "etcdMembersDown")):
            with self.subTest(risk_id=risk_id):
                risk = by_id[risk_id]
                self.assertEqual(risk["configuredRuleNames"], [rule_name])
                self.assertEqual(risk["configuredRuleBasis"], ["known-chart-default-name"])
                self.assertEqual(risk["loadedRules"][0]["health"], "ok")
                self.assertEqual(risk["status"], "unverified")
                self.assertIn("data-unverified", risk["reasons"])
                self.assertIn("delivery-unverified", risk["reasons"])
        self.assertIn("known-chart-default-rules-prove-rule-presence-only", report["limitations"])
        self.assertIn("target-health-does-not-prove-risk-metric-samples", report["limitations"])

    def test_atlas_node_systemd_rules_are_identified_as_default_managed_rules(self):
        chart_rules = [{"alert": "AtlasKubeletServiceUnavailable", "labels": {"severity": "critical"}}]
        loaded = {"state": "observed", "rules": {"R-NODE-06": [
            {"name": "AtlasKubeletServiceUnavailable", "health": "ok", "state": "inactive"}]}}
        report = collect(Client(rules=chart_rules), {}, loaded)
        risk = next(item for item in report["risks"] if item["riskId"] == "R-NODE-06")
        self.assertEqual(risk["configuredRuleBasis"], ["known-atlas-default-name"])
        self.assertEqual(risk["loadedRules"][0]["name"], "AtlasKubeletServiceUnavailable")

    def test_explicit_target_inventory_only_verifies_data_stage_for_observer_risk(self):
        class TargetRuleClient(Client):
            def get(self, kind, namespace=None):
                return {"items": [{"metadata": {"namespace": "monitoring", "name": "scrapes", "uid": "uid"},
                                   "spec": {"groups": [{"name": "scrapes", "rules": [
                    {"alert": "AtlasScrapeTargetOrTelemetryMissing",
                     "labels": {"risk_id": "R-OBS-01"}},
                ]}]}}]}

        loaded = {"state": "observed", "rules": parse_alert_rules({"status": "success", "data": {"groups": [
            {"file": "/rules/monitoring-scrapes-uid.yaml", "name": "scrapes", "rules": [
                {"name": "AtlasScrapeTargetOrTelemetryMissing", "health": "ok", "state": "inactive",
                 "labels": {"risk_id": "R-OBS-01"}}]},
        ]}})}
        healthy_targets = {"state": "observed", "healthy": True, "expected": [
            {"job": "node", "instance": "cp1:9100", "state": "healthy"}], "unexpected": []}
        result = collect(TargetRuleClient(), {}, loaded, healthy_targets)
        observer = next(item for item in result["risks"] if item["riskId"] == "R-OBS-01")
        self.assertEqual(observer["status"], "unverified")
        self.assertEqual(observer["reasons"], ["delivery-unverified"])
        self.assertTrue(result["targetInventory"]["healthy"])

        missing_targets = {"state": "observed", "healthy": False, "expected": [
            {"job": "node", "instance": "cp1:9100", "state": "missing"}], "unexpected": []}
        result = collect(TargetRuleClient(), {}, loaded, missing_targets)
        observer = next(item for item in result["risks"] if item["riskId"] == "R-OBS-01")
        self.assertEqual(observer["status"], "uncovered")
        self.assertIn("data-failed", observer["reasons"])

    def test_scope_file_is_absolute_json_and_malformed_file_fails(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as directory:
            path = Path(directory) / "scope.json"
            path.write_text(json.dumps({"schemaVersion": 1, "risks": [
                {"riskId": "R-LB-01", "applicability": "required"}]}))
            self.assertEqual(read_scope(str(path))["R-LB-01"], ("required", None))
            path.write_text("{}")
            with self.assertRaises(OperationError):
                read_scope(str(path))

    def test_reusable_profile_validates_and_retains_legacy_scope_shape(self):
        example_path = Path(__file__).resolve().parents[1] / "examples/monitoring-scope.example.json"
        example = parse_profile(json.loads(example_path.read_text()))
        self.assertEqual(len(example["expectedTargets"]), 2)
        self.assertEqual(example["observerMappings"]["R-EDGE-01"], ("api-vip",))

        legacy = parse_profile({"schemaVersion": 1, "risks": [
            {"riskId": "R-BIZ-01", "applicability": "not-applicable", "reason": "No business workloads"}
        ]})
        self.assertEqual(legacy["scope"]["R-BIZ-01"], ("not-applicable", "No business workloads"))
        self.assertEqual(legacy["expectedTargets"], ())
        self.assertEqual(legacy["riskTargets"], {})
        self.assertEqual(legacy["observerMappings"], {})

        profile = parse_profile({"schemaVersion": 1, "risks": [
            {"riskId": "R-BIZ-01", "applicability": "required"}],
            "expectedTargets": [
                {"scrapePool": "serviceMonitor/monitoring/node/0", "job": "node", "instance": "cp1:9100"},
                {"scrapePool": "serviceMonitor/monitoring/node/0", "job": "node", "instance": "cp1:9100"}],
            "riskTargets": [
                {"riskId": "R-NODE-01", "scrapePool": "serviceMonitor/monitoring/node/0",
                 "job": "node", "instance": "cp1:9100"}],
            "observerMappings": [{"riskId": "R-EDGE-01", "observerName": "api-vip"}]})
        self.assertEqual(len(profile["expectedTargets"]), 1)
        self.assertEqual(profile["riskTargets"]["R-NODE-01"][0]["instance"], "cp1:9100")
        self.assertEqual(profile["observerMappings"], {"R-EDGE-01": ("api-vip",)})

    def test_reusable_profile_rejects_unknown_fields_versions_risks_and_mappings(self):
        base = {"schemaVersion": 1, "risks": []}
        invalid = (
            {**base, "schemaVersion": 2},
            {**base, "schemaVersion": True},
            {**base, "extra": True},
            {**base, "riskTargets": [{"riskId": "R-NOPE-01", "scrapePool": "pool",
                                      "job": "node", "instance": "cp1:9100"}]},
            {**base, "riskTargets": [
                {"riskId": "R-NODE-01", "scrapePool": "pool", "job": "node", "instance": "cp1:9100"},
                {"riskId": "R-NODE-01", "scrapePool": "pool", "job": "node", "instance": "cp1:9100"}]},
            {**base, "expectedTargets": [{"scrapePool": "pool", "job": "node", "instance": "cp1:9100",
                                           "url": "https://secret.invalid"}]},
            {**base, "observerMappings": [{"riskId": "R-EDGE-01", "observerName": "bad name"}]},
            {**base, "observerMappings": [{"riskId": "R-UNKNOWN-01", "observerName": "api-vip"}]},
        )
        for document in invalid:
            with self.subTest(document=document), self.assertRaises(OperationError):
                parse_profile(document)

    def test_read_profile_preserves_scope_only_compatibility(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as directory:
            path = Path(directory) / "scope.json"
            path.write_text(json.dumps({"schemaVersion": 1, "risks": [], "expectedTargets": [
                {"scrapePool": "pool", "job": "node", "instance": "cp1:9100"}]}))
            self.assertEqual(len(read_profile(str(path))["expectedTargets"]), 1)
            self.assertEqual(read_scope(str(path)), {})

    def test_cli_profile_supplies_all_coverage_inputs_and_cli_duplicates_merge(self):
        from tests.test_monitoring_inventory import Client as InventoryClient
        target = {"scrapePool": "serviceMonitor/default/node/0", "job": "node", "instance": "cp1:9100"}
        audit = {"state": "observed", "healthy": True, "expectedCount": 1,
                 "discoveredCount": 1, "issueCount": 0, "unexpectedCount": 0,
                 "missingCount": 0, "expected": [{**target, "state": "healthy"}], "unexpected": []}
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as directory:
            profile_path = Path(directory) / "profile.json"
            report_dir = Path(directory) / "reports"
            profile_path.write_text(json.dumps({"schemaVersion": 1, "risks": [
                {"riskId": "R-BIZ-01", "applicability": "not-applicable", "reason": "No business workloads"}],
                "expectedTargets": [target],
                "riskTargets": [{"riskId": "R-NODE-01", **target}],
                "riskMetrics": [{"riskId": "R-NODE-01", **target, "metric": "node_uname_info"}],
                "observerMappings": [{"riskId": "R-EDGE-01", "observerName": "api-vip"}]}))
            with patch("scripts.operations.commands.monitoring.Kubernetes", return_value=InventoryClient()), \
                 patch("scripts.operations.commands.monitoring.prometheus.collect_alert_rules",
                       return_value={"state": "observed", "rules": {}}), \
                 patch("scripts.operations.commands.monitoring.prometheus.collect_targets",
                       return_value=audit) as collect_targets, \
                 patch.object(metric_evidence.prometheus, "collect_metric_timestamp",
                              return_value={"state": "fresh", "sampledAt": 90}) as collect_metric, \
                 patch("scripts.operations.commands.monitoring.observer_evidence.collect",
                       return_value={"service": {"state": "unverified"}, "risks": {}}) as collect_observer, \
                 redirect_stdout(io.StringIO()):
                result = cli.main(["--lang", "zh_CN", "--monitoring-coverage", "--output-dir", str(report_dir),
                                   "--prometheus-url", "http://127.0.0.1:19090",
                                   "--monitoring-scope-file", str(profile_path),
                                   "--monitoring-expected-target", "serviceMonitor/default/node/0|node|cp1:9100",
                                   "--monitoring-risk-target",
                                   "R-NODE-01|serviceMonitor/default/node/0|node|cp1:9100",
                                   "--monitoring-observer-risk", "R-EDGE-01|api-vip"])
                self.assertEqual(result, 2)
            collect_targets.assert_called_once_with("http://127.0.0.1:19090", [target], 120)
            collect_metric.assert_called_once()
            collect_observer.assert_called_once_with(None, {"R-EDGE-01": ("api-vip",)}, 120)
            report = json.loads(next(report_dir.rglob("*.json")).read_text())
            details = report["execution"]["result"]
            self.assertEqual(details["riskTargets"]["R-NODE-01"], [target])
            node = next(item for item in details["risks"] if item["riskId"] == "R-NODE-01")
            self.assertEqual(node["dataEvidence"]["state"], "verified")
            self.assertNotEqual(node["status"], "covered")
            biz = next(item for item in details["risks"] if item["riskId"] == "R-BIZ-01")
            self.assertEqual(biz["status"], "not-applicable")

    def test_rule_inventory_reads_all_namespaces(self):
        client = Kubernetes("/tmp/kubeconfig")
        with patch.object(client, "call", return_value={"items": []}) as call:
            client.get("prometheusrules.monitoring.coreos.com")
        self.assertIn("--all-namespaces", call.call_args.args[0])

    def test_prometheus_rule_api_parsing_and_url_validation(self):
        self.assertEqual(rules_endpoint("http://127.0.0.1:9090/prometheus"),
                         "http://127.0.0.1:9090/prometheus/api/v1/rules?type=alert")
        with self.assertRaises(ValueError):
            rules_endpoint("http://prometheus.example:9090")
        parsed = parse_alert_rules({"status": "success", "data": {"groups": [{"rules": [
            {"name": "NodeReady", "health": "ok", "state": "inactive", "labels": {"risk_id": "R-NODE-01"}},
            {"name": "KubeAPIDown", "health": "ok", "state": "inactive", "labels": {"severity": "warning"}},
            {"name": "etcdHighNumberOfFailedProposals", "health": "ok", "state": "inactive", "labels": {"severity": "warning"}}]}]}})
        self.assertEqual(parsed["R-NODE-01"][0]["health"], "ok")
        self.assertEqual(parsed["R-CP-01"][0]["name"], "KubeAPIDown")
        self.assertEqual(parsed["R-ETCD-04"][0]["name"], "etcdHighNumberOfFailedProposals")
        atlas_parsed = parse_alert_rules({"status": "success", "data": {"groups": [{"rules": [
            {"name": "AtlasKubeletServiceUnavailable", "health": "ok", "state": "pending",
             "labels": {"severity": "critical"}},
        ]}]}})
        self.assertEqual(atlas_parsed["R-NODE-06"][0]["name"], "AtlasKubeletServiceUnavailable")
        with self.assertRaises(ValueError):
            parse_alert_rules({"status": "success", "data": {"groups": [{"rules": [
                {"name": "Bad", "health": "ok", "state": "unknown", "labels": {"risk_id": "R-NODE-01"}}]}]}})

    def test_cli_writes_complete_private_risk_report_and_keeps_uncertainty(self):
        from tests.test_monitoring_inventory import Client as InventoryClient
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as directory, \
             patch("scripts.operations.commands.monitoring.Kubernetes", return_value=InventoryClient()), \
             redirect_stdout(io.StringIO()) as output:
            result = cli.main(["--lang", "zh_CN", "--monitoring-coverage", "--output-dir", directory])
            self.assertEqual(result, 2)
            report = json.loads(next(Path(directory).rglob("*.json")).read_text())
        details = report["execution"]["result"]
        self.assertEqual(len(details["risks"]), 63)
        self.assertEqual(details["ruleInventoryScope"], "all-namespaces")
        self.assertEqual(details["counts"]["covered"], 0)
        self.assertIn("通知送达", output.getvalue())

    def test_cli_target_audit_passes_exact_expected_scope_and_records_live_result(self):
        from tests.test_monitoring_inventory import Client as InventoryClient
        audit = {"state": "observed", "healthy": True, "expectedCount": 1,
                 "discoveredCount": 1, "issueCount": 0, "unexpectedCount": 0,
                 "missingCount": 0,
                 "expected": [{"scrapePool": "serviceMonitor/default/node/0", "job": "node",
                               "instance": "cp1:9100", "state": "healthy"}],
                 "unexpected": []}
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as directory, \
             patch("scripts.operations.commands.monitoring.Kubernetes", return_value=InventoryClient()), \
             patch("scripts.operations.commands.monitoring.prometheus.collect_alert_rules",
                   return_value={"state": "observed", "rules": {}}), \
             patch("scripts.operations.commands.monitoring.prometheus.collect_targets",
                   return_value=audit) as collect_targets, redirect_stdout(io.StringIO()) as output:
            result = cli.main(["--lang", "zh_CN", "--monitoring-coverage", "--output-dir", directory,
                               "--prometheus-url", "http://127.0.0.1:19090",
                               "--monitoring-expected-target",
                               "serviceMonitor/default/node/0|node|cp1:9100",
                               "--monitoring-risk-target",
                               "R-NODE-01|serviceMonitor/default/node/0|node|cp1:9100"])
            self.assertEqual(result, 2)
            report = json.loads(next(Path(directory).rglob("*.json")).read_text())
        collect_targets.assert_called_once_with("http://127.0.0.1:19090",
            [{"scrapePool": "serviceMonitor/default/node/0", "job": "node", "instance": "cp1:9100"}], 120)
        self.assertEqual(report["execution"]["result"]["targetInventory"], audit)
        node = next(item for item in report["execution"]["result"]["risks"]
                    if item["riskId"] == "R-NODE-01")
        self.assertEqual(node["dataEvidence"]["state"], "unknown")
        self.assertEqual(node["targetHealthEvidence"]["state"], "healthy")
        self.assertIn("预期目标：1", output.getvalue())

    def test_cli_risk_target_alone_is_audited_and_shared_identity_is_queried_once(self):
        from tests.test_monitoring_inventory import Client as InventoryClient
        audit = {"state": "observed", "healthy": True, "expectedCount": 1,
                 "discoveredCount": 1, "issueCount": 0, "unexpectedCount": 0,
                 "missingCount": 0,
                 "expected": [{"scrapePool": "serviceMonitor/default/node/0", "job": "node",
                               "instance": "cp1:9100", "state": "healthy"}],
                 "unexpected": []}
        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as directory, \
             patch("scripts.operations.commands.monitoring.Kubernetes", return_value=InventoryClient()), \
             patch("scripts.operations.commands.monitoring.prometheus.collect_alert_rules",
                   return_value={"state": "observed", "rules": {}}), \
             patch("scripts.operations.commands.monitoring.prometheus.collect_targets",
                   return_value=audit) as collect_targets, redirect_stdout(io.StringIO()) as output:
            result = cli.main(["--lang", "zh_CN", "--monitoring-coverage", "--output-dir", directory,
                               "--prometheus-url", "http://127.0.0.1:19090",
                               "--monitoring-risk-target",
                               "R-NODE-01|serviceMonitor/default/node/0|node|cp1:9100",
                               "--monitoring-risk-target",
                               "R-OBS-01|serviceMonitor/default/node/0|node|cp1:9100"])
            self.assertEqual(result, 2)
            report = json.loads(next(Path(directory).rglob("*.json")).read_text())
        collect_targets.assert_called_once_with("http://127.0.0.1:19090",
            [{"scrapePool": "serviceMonitor/default/node/0", "job": "node", "instance": "cp1:9100"}], 120)
        risks = {item["riskId"]: item for item in report["execution"]["result"]["risks"]}
        self.assertEqual(risks["R-NODE-01"]["dataEvidence"]["state"], "unknown")
        self.assertEqual(risks["R-NODE-01"]["targetHealthEvidence"]["state"], "healthy")
        self.assertEqual(risks["R-OBS-01"]["dataEvidence"]["state"], "verified")
        self.assertIn("指标语义、告警行为和通知送达", output.getvalue())

        with tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None) as directory, \
             patch("scripts.operations.commands.monitoring.Kubernetes", return_value=InventoryClient()), \
             patch("scripts.operations.commands.monitoring.prometheus.collect_alert_rules",
                   return_value=None), \
             patch("scripts.operations.commands.monitoring.prometheus.collect_targets") as collect_targets, \
             redirect_stdout(io.StringIO()) as output:
            result = cli.main(["--lang", "zh_CN", "--monitoring-coverage", "--output-dir", directory,
                               "--monitoring-risk-target",
                               "R-NODE-01|serviceMonitor/default/node/0|node|cp1:9100"])
            self.assertEqual(result, 2)
            report = json.loads(next(Path(directory).rglob("*.json")).read_text())
        collect_targets.assert_not_called()
        node = next(item for item in report["execution"]["result"]["risks"]
                    if item["riskId"] == "R-NODE-01")
        self.assertEqual(node["dataEvidence"]["state"], "unknown")
        self.assertIn("--monitoring-risk-target", output.getvalue())


if __name__ == "__main__":
    unittest.main()
