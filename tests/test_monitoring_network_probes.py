# SPDX-License-Identifier: MIT
import copy
import unittest

from scripts.operations.models import OperationError
from scripts.operations.monitoring import network_probes as probes


def pod(name, namespace, ip, labels, node="w1"):
    return {"metadata": {"name": name, "namespace": namespace, "uid": name + "-uid", "labels": labels},
            "spec": {"nodeName": node, "containers": [{"name": "app", "ports": [{"name": "http", "containerPort": 8080}]}]},
            "status": {"phase": "Running", "podIP": ip,
                       "conditions": [{"type": "Ready", "status": "True"}]}}


class Client:
    def __init__(self):
        self.prom = {"metadata": {"uid": "prom-1", "resourceVersion": "1"},
                     "spec": {"serviceMonitorSelector": {"matchLabels": {"release": "stack"}},
                              "ruleSelector": {"matchLabels": {"release": "stack"}}}}
        self.services = [
            {"metadata": {"name": "target", "namespace": "apps", "uid": "target-svc", "labels": {}},
             "spec": {"clusterIP": "10.96.0.10", "clusterIPs": ["10.96.0.10"],
                      "selector": {"app": "target"}, "ports": [{"name": "web", "port": 8080, "targetPort": "http"}]}},
            {"metadata": {"name": "kube-dns", "namespace": "kube-system", "uid": "dns-svc"},
             "spec": {"clusterIP": "10.96.0.53", "selector": {"k8s-app": "kube-dns"},
                      "ports": [{"port": 53, "protocol": "UDP"}]}},
            {"metadata": {"name": "blackbox-exporter", "namespace": "monitoring", "uid": "exporter-svc",
                           "labels": {"app.kubernetes.io/name": "prometheus-blackbox-exporter",
                                      "app.kubernetes.io/instance": "blackbox-exporter"}},
             "spec": {"selector": {"app.kubernetes.io/name": "prometheus-blackbox-exporter",
                                   "app.kubernetes.io/instance": "blackbox-exporter"},
                      "ports": [{"name": "http", "port": 9115}]}}
        ]
        exporter_labels = {"app.kubernetes.io/name": "prometheus-blackbox-exporter",
                           "app.kubernetes.io/instance": "blackbox-exporter"}
        self.daemons = [{"metadata": {"name": "blackbox-exporter", "namespace": "monitoring", "uid": "ds-1",
                                      "labels": exporter_labels},
                         "spec": {"selector": {"matchLabels": exporter_labels}},
                         "status": {"desiredNumberScheduled": 3, "numberReady": 3}}]
        self.pods = [pod("target-pod", "apps", "10.244.1.8", {"app": "target"})]
        self.objects = {}
        self.policies = []
        self.version = 1

    def get(self, kind, name=None, namespace=None, optional=False):
        if kind == probes.publication.PROMETHEUS:
            return copy.deepcopy(self.prom)
        if kind == "service":
            rows = [item for item in self.services if namespace is None or item["metadata"]["namespace"] == namespace]
        elif kind == "pod":
            rows = self.pods
        elif kind == "daemonset":
            rows = self.daemons
        elif kind == probes.POLICY:
            if name is not None:
                value = self.objects.get((kind, namespace, name))
                if optional:
                    return copy.deepcopy(value)
                if value is None:
                    raise OperationError("ops.change.kubernetes")
                return copy.deepcopy(value)
            rows = self.policies
        else:
            value = self.objects.get((kind, namespace, name))
            if optional:
                return copy.deepcopy(value)
            if value is None:
                raise OperationError("ops.change.kubernetes")
            return copy.deepcopy(value)
        if name is not None:
            match = next((item for item in rows if item["metadata"]["name"] == name), None)
            if optional:
                return copy.deepcopy(match)
            if match is None:
                raise OperationError("ops.change.kubernetes")
            return copy.deepcopy(match)
        return {"items": copy.deepcopy(rows)}

    def create(self, value):
        kind = {"NetworkPolicy": probes.POLICY, "ServiceMonitor": probes.MONITOR,
                "PrometheusRule": probes.RULES}[value["kind"]]
        meta = value["metadata"]
        key = (kind, meta["namespace"], meta["name"])
        if key in self.objects:
            return None
        self.version += 1
        result = copy.deepcopy(value)
        result["metadata"].update(uid="created-" + str(self.version), resourceVersion=str(self.version))
        self.objects[key] = result
        return copy.deepcopy(result)

    def replace(self, value):
        kind = {"NetworkPolicy": probes.POLICY, "ServiceMonitor": probes.MONITOR,
                "PrometheusRule": probes.RULES}[value["kind"]]
        meta = value["metadata"]
        key = (kind, meta["namespace"], meta["name"])
        current = self.objects.get(key)
        if current is None or current["metadata"]["resourceVersion"] != meta["resourceVersion"]:
            return None
        self.version += 1
        result = copy.deepcopy(value)
        result["metadata"]["resourceVersion"] = str(self.version)
        self.objects[key] = result
        return copy.deepcopy(result)


class NetworkProbeTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.notification = {"failedSummary": "probe failed", "failedDescription": "check path",
                             "scrapeSummary": "scrape failed"}
        self.targets = ["pod=http://10.244.1.8:8080/health", "service=http://10.96.0.10:8080/health",
                        "dns=http://target.apps.svc.cluster.local:8080/health"]

    def manifests(self):
        return probes.manifests("monitoring", "stack", "prometheus-stack", "blackbox-exporter",
                                self.targets, self.client.services, self.client.pods, self.notification)

    def test_targets_are_resolved_and_egress_is_restricted_to_dns_and_target_pods(self):
        monitor, policy, rule, bindings = self.manifests()
        self.assertEqual([item["name"] for item in bindings], ["pod", "service", "dns"])
        peers = policy["spec"]["egress"]
        self.assertEqual(len(peers), 2)
        target_peer = next(item for item in peers if item["ports"][0]["port"] == 8080)
        self.assertEqual(target_peer["to"][0]["namespaceSelector"]["matchLabels"][
            "kubernetes.io/metadata.name"], "apps")
        self.assertEqual(target_peer["to"][0]["podSelector"]["matchLabels"], {"app": "target"})
        self.assertEqual(len(monitor["spec"]["endpoints"]), 4)
        for endpoint in monitor["spec"]["endpoints"]:
            self.assertEqual(endpoint["relabelings"][0]["action"], "replace")
        self.assertEqual(monitor["spec"]["endpoints"][0]["relabelings"][1],
                         {"action": "replace", "targetLabel": "metrics_endpoint", "replacement": "blackbox-exporter"})
        for endpoint, target in zip(monitor["spec"]["endpoints"][1:], bindings):
            self.assertNotIn("metricRelabelings", endpoint)
            self.assertIn({"action": "replace", "targetLabel": "metrics_endpoint", "replacement": "blackbox-probe"},
                          endpoint["relabelings"])
            self.assertIn({"action": "replace", "targetLabel": "probe_target", "replacement": target["name"]},
                          endpoint["relabelings"])
        self.assertFalse(monitor["spec"]["endpoints"][1]["params"].get("hostname"))
        rules = rule["spec"]["groups"][0]["rules"]
        self.assertIn('(probe_success{job="blackbox-exporter",metrics_endpoint="blackbox-probe"} == 0)',
                      rules[0]["expr"])
        for alert in rules:
            self.assertIn("time() - timestamp(", alert["expr"])
            self.assertIn(" <= 90)", alert["expr"])
        self.assertEqual(rules[0]["labels"]["risk_id"], "R-NET-03")
        self.assertEqual(rules[1]["labels"]["risk_id"], "R-OBS-01")

    def test_rejects_external_hosts_credentials_redirects_bad_ports_and_duplicate_names(self):
        invalid = [
            ["public=http://example.com/"],
            ["auth=http://user:pass@target.apps.svc.cluster.local:8080/"],
            ["tls=https://target.apps.svc.cluster.local:8443/"],
            ["bad=http://target.apps.svc.cluster.local:70000/"],
            ["same=http://10.244.1.8:8080/", "same=http://10.96.0.10:8080/"],
        ]
        for targets in invalid:
            with self.subTest(targets=targets):
                with self.assertRaises((ValueError, OperationError)):
                    bound = probes.parse_targets(targets)
                    probes.manifests("monitoring", "stack", "prometheus-stack", "blackbox-exporter",
                                      [item["name"] + "=" + item["url"] for item in bound],
                                      self.client.services, self.client.pods, self.notification)

    def test_plan_capture_rejects_unready_exporter_and_existing_egress_policy(self):
        rendered = self.manifests()
        self.client.daemons[0]["status"]["numberReady"] = 2
        with self.assertRaises(OperationError):
            probes.capture(self.client, "monitoring", "stack", "prometheus-stack", "blackbox-exporter", rendered)
        self.client.daemons[0]["status"]["numberReady"] = 3
        self.client.policies.append({"metadata": {"name": "existing-egress"}, "spec": {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus-blackbox-exporter"}},
            "egress": [], "policyTypes": ["Egress"]}})
        with self.assertRaises(OperationError):
            probes.capture(self.client, "monitoring", "stack", "prometheus-stack", "blackbox-exporter", rendered)

    def test_capture_publish_and_idempotent_retry_preserve_resource_ownership(self):
        rendered = self.manifests()
        expected = probes.capture(self.client, "monitoring", "stack", "prometheus-stack", "blackbox-exporter", rendered)
        steps = []
        result = probes.publish(self.client, rendered, expected, steps.append)
        self.assertEqual(result["evaluation"], "unverified")
        self.assertEqual(steps, ["monitoring-network-egress", "monitoring-network-servicemonitor",
                                 "monitoring-network-rules"])
        probes.publish(self.client, rendered, expected, lambda _step: None)
        self.assertEqual(len(self.client.objects), 3)
        self.client.prom["spec"]["serviceMonitorSelector"] = {"matchLabels": {"release": "other"}}
        with self.assertRaises(OperationError):
            probes.capture(self.client, "monitoring", "stack", "prometheus-stack", "blackbox-exporter", rendered)


if __name__ == "__main__":
    unittest.main()
