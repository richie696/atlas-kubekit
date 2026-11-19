# SPDX-License-Identifier: MIT
import copy
import unittest

from scripts.operations.models import OperationError
from scripts.operations.monitoring import lb_collection
from scripts.operations.monitoring import publication


class Client:
    def __init__(self):
        self.prometheus = {
            "metadata": {"uid": "prom-1", "resourceVersion": "1"},
            "spec": {"serviceMonitorSelector": {"matchLabels": {"release": "stack"}}},
        }
        self.objects = {}
        self.version = 1
        self.fail_kind = None

    def get(self, kind, name=None, namespace=None, optional=False):
        if kind == publication.PROMETHEUS:
            return copy.deepcopy(self.prometheus)
        return copy.deepcopy(self.objects.get((kind, namespace, name)))

    def create(self, value):
        kind = lb_collection.RESOURCE_KIND[value["kind"]]
        key = (kind, value["metadata"]["namespace"], value["metadata"]["name"])
        if kind == self.fail_kind or key in self.objects:
            return None
        self.version += 1
        result = copy.deepcopy(value)
        result["metadata"].update(uid=f"uid-{self.version}", resourceVersion=str(self.version))
        self.objects[key] = result
        return copy.deepcopy(result)

    def replace(self, value):
        kind = lb_collection.RESOURCE_KIND[value["kind"]]
        key = (kind, value["metadata"]["namespace"], value["metadata"]["name"])
        current = self.objects.get(key)
        if (kind == self.fail_kind or current is None
                or current["metadata"]["resourceVersion"] != value["metadata"]["resourceVersion"]):
            return None
        self.version += 1
        result = copy.deepcopy(value)
        result["metadata"]["resourceVersion"] = str(self.version)
        self.objects[key] = result
        return copy.deepcopy(result)


class LoadBalancerCollectionTest(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.service, self.endpoints, self.monitor = lb_collection.manifests(
            "monitoring", "stack", [
                {"name": "lb1", "address": "10.20.1.12"},
                {"name": "lb2", "address": "10.20.1.19"},
            ]
        )

    def capture(self):
        return lb_collection.capture(self.client, self.service, self.endpoints, self.monitor, "prometheus")

    def test_manifests_scrape_registered_addresses_and_label_nodes(self):
        self.assertEqual(self.service["spec"]["clusterIP"], "None")
        self.assertEqual(self.service["spec"]["clusterIPs"], ["None"])
        self.assertEqual(self.service["spec"]["ipFamilies"], ["IPv4"])
        self.assertEqual(self.service["spec"]["ipFamilyPolicy"], "SingleStack")
        self.assertEqual(
            [row["ip"] for row in self.endpoints["subsets"][0]["addresses"]],
            ["10.20.1.12", "10.20.1.19"],
        )
        scrape = self.monitor["spec"]["endpoints"][0]
        self.assertEqual((scrape["port"], scrape["path"], scrape["interval"]), ("metrics", "/metrics", "30s"))
        self.assertEqual([row["replacement"] for row in scrape["relabelings"]], ["lb1", "lb2", "haproxy"])
        self.assertEqual(scrape["relabelings"][-1]["targetLabel"], "metrics_endpoint")
        self.assertEqual(scrape["relabelings"][-1]["replacement"], "haproxy")
        vip_scrape = self.monitor["spec"]["endpoints"][1]
        self.assertEqual((vip_scrape["path"], vip_scrape["interval"]), ("/vip-metrics", "15s"))
        self.assertEqual(vip_scrape["relabelings"][-1]["replacement"], "vip")

    def test_invalid_duplicate_and_non_lb_targets_are_rejected(self):
        for nodes in (
            [{"name": "worker1", "address": "10.0.0.1"}, {"name": "lb2", "address": "10.0.0.2"}],
            [{"name": "lb1", "address": "10.0.0.1"}, {"name": "lb2", "address": "10.0.0.1"}],
            [{"name": "lb1", "address": "127.0.0.1"}, {"name": "lb2", "address": "10.0.0.2"}],
        ):
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                lb_collection.manifests("monitoring", "stack", nodes)

    def test_prometheus_selector_and_foreign_resources_block_writes(self):
        self.client.prometheus["spec"]["serviceMonitorSelector"] = None
        with self.assertRaises(OperationError):
            self.capture()
        self.client = Client()
        key = (lb_collection.SERVICE, "monitoring", "atlas-lb-metrics")
        self.client.objects[key] = {"metadata": {"labels": {"third-party": "owned"}}, "spec": {}}
        with self.assertRaises(OperationError):
            self.capture()

    def test_publish_is_ordered_idempotent_and_reports_scraping_unverified(self):
        expected = self.capture()
        checkpoints = []
        result = lb_collection.publish(self.client, self.service, self.endpoints, self.monitor,
                                       "prometheus", expected, checkpoints.append)
        self.assertEqual(result["scraping"], "unverified")
        self.assertEqual(checkpoints, ["monitoring-lb-endpoints", "monitoring-lb-service-monitor"])
        self.assertEqual(len(self.client.objects), 3)
        versions = {key: value["metadata"]["resourceVersion"] for key, value in self.client.objects.items()}
        expected = self.capture()
        lb_collection.publish(self.client, self.service, self.endpoints, self.monitor,
                              "prometheus", expected, lambda _step: None)
        self.assertEqual(len(self.client.objects), 3)
        self.assertEqual(versions, {key: value["metadata"]["resourceVersion"]
                                    for key, value in self.client.objects.items()})

    def test_selector_drift_and_partial_failure_do_not_claim_success(self):
        expected = self.capture()
        self.client.prometheus["spec"]["serviceMonitorSelector"]["matchLabels"]["release"] = "other"
        with self.assertRaises(OperationError):
            lb_collection.publish(self.client, self.service, self.endpoints, self.monitor,
                                  "prometheus", expected, lambda _step: None)
        self.assertEqual(self.client.objects, {})

        self.client = Client()
        expected = self.capture()
        self.client.fail_kind = lb_collection.MONITOR
        with self.assertRaises(OperationError):
            lb_collection.publish(self.client, self.service, self.endpoints, self.monitor,
                                  "prometheus", expected, lambda _step: None)
        self.assertIn((lb_collection.SERVICE, "monitoring", "atlas-lb-metrics"), self.client.objects)
        self.assertIn((lb_collection.ENDPOINTS, "monitoring", "atlas-lb-metrics"), self.client.objects)
        self.assertNotIn((lb_collection.MONITOR, "monitoring", "atlas-lb-metrics"), self.client.objects)


if __name__ == "__main__":
    unittest.main()
