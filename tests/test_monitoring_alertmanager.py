# SPDX-License-Identifier: MIT
import json
import unittest
from scripts.operations.monitoring.channels import alertmanager


def payload():
    return {"version": "4", "receiver": "team-a", "status": "firing",
            "truncatedAlerts": 2, "alerts": [
                {"status": "firing", "fingerprint": "a0123", "startsAt": "2026-11-10T00:00:00Z",
                 "labels": {"alertname": "NodeDown", "node": "worker1", "pod": "api-0",
                            "severity": "critical", "risk_id": "R-NODE-01", "secret": "do-not-forward"},
                 "annotations": {"summary": "节点 worker1 未就绪", "description": "请检查节点心跳和 kubelet 状态。",
                                 "observed_value": "Ready=0", "extra": "do-not-forward"}},
                {"status": "resolved", "fingerprint": "b4567", "startsAt": "2026-11-10T00:00:00Z",
                 "endsAt": "2026-11-10T00:01:00Z", "labels": {"alertname": "Other"}, "annotations": {}}]}


class AlertmanagerContractTest(unittest.TestCase):
    def test_group_preserves_individual_states_and_truncation(self):
        result = alertmanager.parse(json.dumps(payload()).encode())
        self.assertEqual(result.receiver, "team-a")
        self.assertIn("[告警触发] 告警规则: NodeDown", result.text)
        self.assertIn("[告警恢复] 告警规则: Other", result.text)
        self.assertIn("摘要: 节点 worker1 未就绪", result.text)
        self.assertIn("处理建议: 请检查节点心跳和 kubelet 状态。", result.text)
        self.assertIn("Pod: api-0", result.text)
        self.assertIn("级别: 严重", result.text)
        self.assertIn("风险编号: R-NODE-01", result.text)
        self.assertIn("观测值: Ready=0", result.text)
        self.assertIn("接收路由: team-a", result.text)
        self.assertIn("节点: worker1", result.text)
        self.assertIn("开始时间: 2026-11-10T00:00:00Z", result.text)
        self.assertIn("摘要: 未提供摘要", result.text)
        self.assertIn("处理建议: 未提供处理说明", result.text)
        self.assertIn("恢复时间: 2026-11-10T00:01:00Z", result.text)
        self.assertIn("另有 2 条告警未包含", result.text)
        self.assertNotIn("do-not-forward", result.text)

    def test_explicit_english_output_remains_available(self):
        item = payload()
        item["alerts"][0]["labels"]["alertname"] = "AtlasNodeReadinessOrTelemetryMissing"
        item["alerts"][0]["labels"]["severity"] = "critical"
        result = alertmanager.parse(json.dumps(item).encode(), "en")
        self.assertIn("Status: Firing", result.text)
        self.assertIn("Severity: Critical", result.text)
        self.assertIn("Additional alerts omitted by Alertmanager: 2", result.text)
        self.assertIn("Alert rule: Node readiness or telemetry missing", result.text)

    def test_known_rule_names_are_localized_and_unknown_names_remain_visible(self):
        item = payload()
        item["alerts"] = item["alerts"][:1]
        item["alerts"][0]["labels"]["alertname"] = "AtlasNodeReadinessOrTelemetryMissing"
        result = alertmanager.parse(json.dumps(item).encode(), "zh_CN")
        self.assertIn("告警规则: 节点未就绪或监控数据缺失", result.text)
        self.assertIn("告警规则: NodeDown", alertmanager.parse(json.dumps(payload()).encode()).text)

    def test_etcd_majority_context_is_forwarded_and_localized(self):
        item = payload()
        item["alerts"] = item["alerts"][:1]
        item["alerts"][0]["labels"].update(
            alertname="AtlasEtcdQuorumOrTelemetryMissing", registered_members="3", quorum_required="2")
        result = alertmanager.parse(json.dumps(item).encode(), "zh_CN")
        self.assertIn("告警规则: etcd 多数派成员不可用", result.text)
        self.assertIn("登记的 etcd 成员数: 3", result.text)
        self.assertIn("多数派所需数量: 2", result.text)

    def test_keda_alert_details_are_localized_and_forwarded(self):
        item = payload()
        item["alerts"] = item["alerts"][:1]
        item["alerts"][0]["labels"].update(
            alertname="AtlasKEDAScalerErrors", namespace="keda", exported_namespace="production",
            scaledObject="api-worker", scaler="prometheusScaler")
        item["alerts"][0]["annotations"].update(summary="事件源查询错误", description="检查 KEDA 事件源和认证配置。")
        result = alertmanager.parse(json.dumps(item).encode(), "zh_CN")
        self.assertIn("告警规则: KEDA 事件源查询错误", result.text)
        self.assertIn("KEDA ScaledObject: api-worker", result.text)
        self.assertIn("命名空间: production", result.text)
        self.assertIn("KEDA scaler: prometheusScaler", result.text)
        self.assertIn("摘要: 事件源查询错误", result.text)
        self.assertIn("处理建议: 检查 KEDA 事件源和认证配置。", result.text)

    def test_hpa_and_keda_empty_upstream_alert_names_are_localized(self):
        expected = {
            "AtlasHPAMetricsUnavailable": "HPA 扩缩容指标不可用",
            "AtlasKEDAEmptyUpstreamResponses": "KEDA scaler 上游指标查询为空",
            "AtlasKEDAOperatorTargetMissing": "KEDA Operator 指标采集目标不可用或缺失",
        }
        for alertname, localized_name in expected.items():
            item = payload()
            item["alerts"] = item["alerts"][:1]
            item["alerts"][0]["labels"]["alertname"] = alertname
            with self.subTest(alertname=alertname):
                self.assertIn("告警规则: " + localized_name,
                              alertmanager.parse(json.dumps(item).encode(), "zh_CN").text)

    def test_full_pvc_identity_uses_annotation_when_label_is_short_reference(self):
        item = payload()
        item["alerts"] = item["alerts"][:1]
        claim = "prometheus-kube-prometheus-stack-prometheus-db-prometheus-kube-prometheus-stack-prometheus-0"
        item["alerts"][0]["labels"].update(persistentvolumeclaim_id="pvc-3ab22f5a87")
        item["alerts"][0]["annotations"]["persistentvolumeclaim"] = claim
        result = alertmanager.parse(json.dumps(item).encode(), "zh_CN")
        self.assertIn("PVC: " + claim, result.text)

    def test_invalid_group_is_never_forwarded(self):
        for change in (lambda p: p.update(version="3"), lambda p: p.update(status="resolved"),
                       lambda p: p.update(truncatedAlerts=True), lambda p: p.update(alerts=[]),
                       lambda p: p["alerts"][0].update(fingerprint="secret"),
                       lambda p: p["alerts"][0]["labels"].update(alertname="")):
            item = payload()
            change(item)
            with self.subTest(item=item), self.assertRaises(ValueError):
                alertmanager.parse(json.dumps(item).encode())
        with self.assertRaises(ValueError):
            alertmanager.parse(b"x" * (alertmanager.MAX_BODY + 1))
        item = payload()
        item["alerts"][0]["labels"]["alertname"] = "\ud800"
        with self.assertRaises(ValueError):
            alertmanager.parse(json.dumps(item).encode())

    def test_acknowledgement_does_not_call_unknown_success(self):
        self.assertEqual(alertmanager.response_code("accepted"), 200)
        self.assertEqual(alertmanager.response_code("unknown"), 503)
        self.assertEqual(alertmanager.response_code("retryable"), 503)
        self.assertEqual(alertmanager.response_code("rejected"), 503)

    def test_truncated_group_can_omit_firing_alert(self):
        item = payload()
        item["alerts"] = item["alerts"][1:]
        result = alertmanager.parse(json.dumps(item).encode())
        self.assertIn("[告警恢复] 告警规则: Other", result.text)
        self.assertIn("另有 2 条告警未包含", result.text)
        item["truncatedAlerts"] = 0
        with self.assertRaises(ValueError):
            alertmanager.parse(json.dumps(item).encode())


if __name__ == "__main__":
    unittest.main()
