import unittest
import base64

from tests.e2e.monitoring_longhorn_failure import (
    DATA,
    collapse_samples,
    expected_state,
    make_resources,
    safe_prometheus_url,
)


class LonghornFailureE2EToolTest(unittest.TestCase):
    def test_duplicate_manager_samples_are_accepted_only_when_equal(self):
        self.assertEqual(collapse_samples([{"value": ["1", "1"]}, {"value": ["2", "1"]}]), 1.0)
        self.assertIsNone(collapse_samples([{"value": ["1", "1"]}, {"value": ["2", "0"]}]))
        self.assertIsNone(collapse_samples([]))

    def test_seed_payload_is_exact_binary_configmap_and_pod_mounts_it_read_only(self):
        resources = make_resources("atlas-e2e-longhorn-x1", "atlas-e2e-longhorn-x1",
                                   "sentinel", "writer", "worker-1", "busybox:1.36")
        self.assertEqual(base64.b64decode(resources[0]["binaryData"]["sentinel.bin"]), DATA)
        pod = resources[3]
        self.assertEqual(pod["spec"]["volumes"][1]["configMap"]["name"],
                         "atlas-e2e-longhorn-x1-payload")
        mounts = pod["spec"]["containers"][0]["volumeMounts"]
        self.assertEqual(mounts[1], {"name": "seed", "mountPath": "/seed", "readOnly": True})

    def test_prometheus_url_requires_tls_or_loopback(self):
        self.assertEqual(safe_prometheus_url("http://127.0.0.1:9090/"), "http://127.0.0.1:9090")
        self.assertEqual(safe_prometheus_url("https://prom.example.com"), "https://prom.example.com")
        for value in (
            "http://prom.example.com",
            "http://user:password@127.0.0.1:9090",
            "https://prom.example.com?token=secret",
            "ftp://127.0.0.1",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                safe_prometheus_url(value)

    def test_volume_health_requires_exact_one_hot_state(self):
        healthy = {"healthy": 1.0, "degraded": 0.0, "faulted": 0.0, "unknown": 0.0}
        degraded = {"healthy": 0.0, "degraded": 1.0, "faulted": 0.0, "unknown": 0.0}
        self.assertTrue(expected_state(healthy, "healthy"))
        self.assertTrue(expected_state(degraded, "degraded"))
        self.assertFalse(expected_state(None, "healthy"))
        self.assertFalse(expected_state({**healthy, "unknown": 1.0}, "healthy"))

    def test_test_storage_and_workload_are_scoped_to_generated_names(self):
        resources = make_resources("atlas-e2e-longhorn-x1", "atlas-e2e-longhorn-x1",
                                   "sentinel", "writer", "worker-1", "busybox:1.36")
        self.assertEqual(resources[1]["metadata"]["name"], "atlas-e2e-longhorn-x1")
        self.assertEqual(resources[1]["parameters"]["numberOfReplicas"], "2")
        self.assertEqual(resources[2]["metadata"]["namespace"], "atlas-e2e-longhorn-x1")
        self.assertEqual(resources[3]["spec"]["nodeSelector"]["kubernetes.io/hostname"], "worker-1")
        # Kubernetes ConfigMaps are limited to 1 MiB; this deterministic fixture
        # stays comfortably below that API limit after base64 encoding.
        self.assertLess(len(resources[0]["binaryData"]["sentinel.bin"]), 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
