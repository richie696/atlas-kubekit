# SPDX-License-Identifier: MIT
import unittest
from argparse import Namespace
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from scripts.lib.i18n import translate
from scripts.operations.models import OperationError
from scripts.operations.commands import monitoring
from scripts.operations.monitoring import observer_evidence
from scripts.operations.monitoring.observer import runtime, store


class MonitoringObserverEvidenceTest(unittest.TestCase):
    def test_risk_mapping_is_explicit_and_rejects_invalid_or_duplicate_entries(self):
        self.assertEqual(observer_evidence.parse_risk_observers([
            "R-EDGE-01|api-vip", "R-EDGE-01|cluster-dns", "R-OBS-04|monitoring-health",
        ]), {
            "R-EDGE-01": ("api-vip", "cluster-dns"),
            "R-OBS-04": ("monitoring-health",),
        })
        for values in (
            ["R-UNKNOWN-01|probe"], ["R-EDGE-01|bad probe"],
            ["R-EDGE-01|probe|extra"], ["R-EDGE-01|probe", "R-EDGE-01|probe"],
        ):
            with self.subTest(values=values), self.assertRaises(OperationError):
                observer_evidence.parse_risk_observers(values)

    def test_fresh_healthy_probe_is_reported_separately_from_assessment(self):
        snapshot = {"owner": "lock-held", "probes": [
            ("api-vip", "expected-http-status", 950),
            ("cluster-dns", "dns-answer-expected", 960),
        ]}
        with patch.object(observer_evidence.status, "snapshot", return_value=snapshot):
            result = observer_evidence.collect("/private/observer", {
                "R-EDGE-01": ("api-vip", "cluster-dns"),
            }, 120, now=1000)

        self.assertEqual(result["service"]["state"], "healthy")
        self.assertEqual(result["service"]["observedAt"], 960)
        self.assertEqual(result["risks"]["R-EDGE-01"]["state"], "healthy")
        self.assertEqual(result["risks"]["R-EDGE-01"]["observedAt"], 960)
        self.assertEqual([row["state"] for row in result["risks"]["R-EDGE-01"]["observations"]],
                         ["healthy", "healthy"])
        self.assertEqual(result["risks"]["R-OBS-04"]["state"], "healthy")

    def test_adapter_reads_fresh_evidence_from_real_observer_sqlite_snapshot(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            observed_at = 1000
            with runtime.database(directory) as connection:
                with patch.object(store, "time", Namespace(time=lambda: observed_at)):
                    store.observe(connection, "api-vip", "healthy", 1, 1,
                                  reason="expected-http-status")
                result = observer_evidence.collect(directory, {"R-EDGE-01": ("api-vip",)},
                                                   120, now=observed_at)

        self.assertEqual(result["service"]["state"], "healthy")
        self.assertEqual(result["risks"]["R-EDGE-01"]["state"], "healthy")
        self.assertEqual(result["risks"]["R-EDGE-01"]["observations"], [{
            "name": "api-vip", "state": "healthy", "reason": "expected-http-status",
            "observedAt": observed_at,
        }])
        self.assertEqual(result["risks"]["R-OBS-04"]["state"], "healthy")

    def test_evidence_uses_clock_after_reading_concurrently_updated_snapshot(self):
        current = 1000

        def read_snapshot(_directory):
            nonlocal current
            current = 1001
            return {"owner": "lock-held", "probes": [("api-vip", "expected-http-status", current)]}

        with patch.object(observer_evidence.status, "snapshot", side_effect=read_snapshot), \
             patch.object(observer_evidence, "time", Namespace(time=lambda: current)):
            result = observer_evidence.collect("/private/observer", {"R-EDGE-01": ("api-vip",)}, 120)
        self.assertEqual(result["service"]["state"], "healthy")
        self.assertEqual(result["risks"]["R-EDGE-01"]["observedAt"], 1001)

    def test_failed_unknown_and_stale_probe_states_are_preserved(self):
        snapshot = {"owner": "lock-held", "probes": [
            ("api-vip", "unreachable-from-observer", 990),
            ("auth-api", "authentication-or-redirect", 990),
            ("old-dns", "dns-answer-expected", 700),
        ]}
        with patch.object(observer_evidence.status, "snapshot", return_value=snapshot):
            result = observer_evidence.collect("/private/observer", {
                "R-EDGE-01": ("api-vip",), "R-NET-01": ("auth-api",),
                "R-NET-02": ("old-dns",),
            }, 120, now=1000)

        self.assertEqual(result["risks"]["R-EDGE-01"]["state"], "failed")
        self.assertEqual(result["risks"]["R-NET-01"]["state"], "unverified")
        self.assertEqual(result["risks"]["R-NET-02"]["state"], "unverified")
        self.assertEqual(result["risks"]["R-NET-02"]["observations"][0]["reason"],
                         "observer-probe-stale")

    def test_stopped_or_unreadable_observer_never_reports_fresh_risk(self):
        snapshot = {"owner": "not-running", "probes": [("api-vip", "expected-http-status", 990)]}
        with patch.object(observer_evidence.status, "snapshot", return_value=snapshot):
            stopped = observer_evidence.collect("/private/observer", {"R-EDGE-01": ("api-vip",)},
                                                120, now=1000)
        self.assertEqual(stopped["service"]["state"], "failed")
        self.assertEqual(stopped["risks"]["R-EDGE-01"]["state"], "failed")

        with patch.object(observer_evidence.status, "snapshot", side_effect=ValueError("private details")):
            unreadable = observer_evidence.collect("/private/observer", {"R-EDGE-01": ("api-vip",)},
                                                   120, now=1000)
        self.assertEqual(unreadable["service"]["reason"], "observer-state-unavailable")
        self.assertEqual(unreadable["risks"]["R-EDGE-01"]["state"], "unverified")
        self.assertEqual(unreadable["risks"]["R-OBS-04"]["state"], "unverified")
        self.assertEqual(unreadable["risks"]["R-OBS-04"]["reason"], "observer-state-unavailable")

    def test_absent_state_and_invalid_time_do_not_claim_health(self):
        absent = observer_evidence.collect(None, {"R-EDGE-01": ("api-vip",)}, 120, now=1000)
        self.assertEqual(absent["risks"]["R-OBS-04"]["state"], "unverified")
        with self.assertRaises(OperationError):
            observer_evidence.collect("/private/observer", {}, 120, now=float("nan"))

    def test_coverage_command_persists_external_evidence_without_changing_risk_counts(self):
        external = {
            "service": {"source": "independent-observer", "state": "healthy", "reason": None},
            "risks": {"R-EDGE-01": {"source": "independent-observer", "state": "healthy",
                                     "reason": None, "observations": []}},
        }
        args = Namespace(
            action="monitoring-coverage", target=None, kubeconfig="/etc/kubernetes/admin.conf",
            control_target="ubuntu@10.20.1.28",
            ssh_identity=None, ssh_config=None, monitoring_scope_file=None, prometheus_url=None,
            monitoring_expected_target=None, monitoring_risk_target=None,
            monitoring_observer_risk=["R-EDGE-01|api-vip"], monitoring_max_age=120,
            observer_state_directory="/private/observer", output_dir="",
        )
        details = {"counts": {"covered": 0, "uncovered": 1, "unverified": 62, "not-applicable": 0},
                   "risks": [{"riskId": "R-EDGE-01", "status": "uncovered", "reasons": ["not-configured"],
                              "dataEvidence": {"targets": []}}], "targetInventory": {"state": "unverified"}}
        client = type("Client", (), {"cluster_identity": lambda self: "atlas-e2e-lab"})()
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            args.output_dir = directory
            with patch.object(monitoring, "Kubernetes", return_value=client), \
                 patch.object(monitoring.coverage_report, "read_scope", return_value={}), \
                 patch.object(monitoring.coverage_report, "collect", return_value=details), \
                 patch.object(observer_evidence, "collect", return_value=external):
                result = monitoring.coverage(args, lambda key, *values: translate("zh_CN", key, *values),
                                             ("run-id", "now", "0.3.0"))
            self.assertEqual(result, 2)
            report = json.loads(next(Path(directory).rglob("report.json")).read_text())
            saved = report["execution"]["result"]
            self.assertEqual(saved["independentObserverEvidence"], external)
            self.assertEqual(saved["counts"]["covered"], 0)
            self.assertEqual(saved["risks"][0]["status"], "uncovered")


if __name__ == "__main__":
    unittest.main()
