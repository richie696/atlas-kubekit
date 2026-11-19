# SPDX-License-Identifier: MIT
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.operations import configuration
from scripts.operations.adapters.kubernetes import Kubernetes
from scripts.operations.capabilities import monitoring_silence
from scripts.operations.models import OperationError, fingerprint
from scripts.operations.monitoring import silences


class FakeClient:
    def __init__(self):
        self.manager = {"metadata": {"uid": "manager-uid", "resourceVersion": "7"},
                        "spec": {"version": "v0.35.0"}}
        self.service = {"metadata": {"uid": "service-uid", "resourceVersion": "3"},
                        "spec": {"selector": {"alertmanager": "stack-alertmanager",
                                               "app.kubernetes.io/name": "alertmanager"},
                                 "ports": [{"name": "http-web", "port": 9093}]}}
        self.items = []
        self.calls = []

    def get(self, kind, name, namespace, optional=False):
        return copy.deepcopy(self.manager if kind == monitoring_silence.ALERTMANAGER else self.service)

    def cluster_identity(self):
        return "12345678-1234-1234-1234-123456789abc"

    def alertmanager_request(self, namespace, service, port, method, endpoint, payload=None):
        self.calls.append((namespace, service, port, method, endpoint, copy.deepcopy(payload)))
        if method == "GET":
            return copy.deepcopy(self.items)
        if method == "POST":
            result = {"silenceID": "abcd1234-1234-1234-1234-123456789abc"}
            self.items.append({"id": result["silenceID"], "createdBy": payload["createdBy"],
                               "comment": payload["comment"], "status": {"state": "active"},
                               "endsAt": payload["endsAt"]})
            return result
        self.items = [item for item in self.items if item["id"] not in endpoint]
        return ""


class Environment:
    def __init__(self):
        self.client = FakeClient()

    def verify(self, value):
        return configuration.normalize(value), {}


def create_values(client):
    expected = {"cluster": client.cluster_identity(),
                "manager": {"uid": "manager-uid", "resourceVersion": "7",
                            "specHash": fingerprint(client.manager["spec"])},
                "service": {"uid": "service-uid", "resourceVersion": "3"}, "silence": None}
    return {"configuration": configuration.template(), "action": "create", "namespace": "monitoring",
            "managerName": "stack-alertmanager", "serviceName": "stack-alertmanager",
            "servicePort": "http-web", "matchers": ["alertname=NodeNotReady", "severity=warning"],
            "duration": "30m", "reason": "例行维护窗口", "silenceId": None,
            "operator": "operator1", "expected": expected}


class MonitoringSilenceTest(unittest.TestCase):
    def test_matchers_duration_are_narrow_and_exclude_critical(self):
        self.assertEqual(len(silences.matchers(["alertname=NodeNotReady", "severity=warning"])), 2)
        for matchers in (["alertname=NodeNotReady"], ["alertname=NodeNotReady", "severity=critical"],
                         ["severity=warning", "namespace=prod"],
                         ["alertname=NodeNotReady", "severity=warning", "severity=info"]):
            with self.subTest(matchers=matchers), self.assertRaises(ValueError):
                silences.matchers(matchers)
        for duration in ("30s", "9h", "2d", "0m"):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                silences.duration(duration)

    def test_create_has_plan_idempotence_and_uses_service_proxy(self):
        environment = Environment()
        values = monitoring_silence.parameters(create_values(environment.client))
        observation = monitoring_silence.observe(values, environment)
        self.assertIn("desired", observation)
        result = monitoring_silence.apply(values, environment, lambda _step: None, "a" * 64)
        self.assertEqual(result["state"], "created")
        self.assertFalse(result["idempotent"])
        self.assertEqual(environment.client.calls[-1][3], "POST")
        self.assertEqual(environment.client.calls[-1][4], "/api/v2/silences")
        self.assertEqual(environment.client.items[0]["createdBy"],
                         "Atlas KubeKit cluster/" + environment.client.cluster_identity() + "/operator1")
        duplicate = monitoring_silence.apply(values, environment, lambda _step: None, "a" * 64)
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(len(environment.client.items), 1)

    def test_expire_refuses_foreign_silence_and_only_deletes_owned_id(self):
        environment = Environment()
        create = monitoring_silence.parameters(create_values(environment.client))
        monitoring_silence.apply(create, environment, lambda _step: None, "a" * 64)
        current = environment.client.items[0]
        expire = {**create, "action": "expire", "matchers": [], "duration": None,
                  "reason": "", "silenceId": current["id"],
                  "expected": {**create["expected"], "silence": {
                      "id": current["id"], "createdBy": current["createdBy"],
                      "comment": current["comment"], "state": "active", "endsAt": current["endsAt"]}}}
        expire = monitoring_silence.parameters(expire)
        monitoring_silence.observe(expire, environment)
        result = monitoring_silence.apply(expire, environment, lambda _step: None, "b" * 64)
        self.assertEqual(result["state"], "expired")
        self.assertEqual(environment.client.calls[-1][3], "DELETE")

        foreign = copy.deepcopy(current)
        foreign["id"] = "efef1234-1234-1234-1234-123456789abc"
        foreign["createdBy"] = "someone else"
        environment.client.items = [foreign]
        expire["silenceId"] = foreign["id"]
        expire["expected"]["silence"]["id"] = foreign["id"]
        expire["expected"]["silence"]["createdBy"] = foreign["createdBy"]
        with self.assertRaises(OperationError):
            monitoring_silence.observe(expire, environment)

    def test_kubernetes_adapter_limits_proxy_methods_and_paths(self):
        client = Kubernetes("/tmp/kubeconfig")
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, _size): return b'{"silenceID":"abcd1234"}'
        process = SimpleNamespace(stdout=None, terminate=lambda: None, wait=lambda **_kwargs: 0)
        with patch.object(client, "_free_port", return_value=45123), \
             patch.object(client, "_wait_proxy") as wait_proxy, \
             patch("scripts.operations.adapters.kubernetes.subprocess.Popen", return_value=process) as popen, \
             patch("scripts.operations.adapters.kubernetes.urlopen", return_value=Response()):
            result = client.alertmanager_request("monitoring", "alertmanager", "http-web", "POST",
                "/api/v2/silences", {"matchers": []})
            self.assertEqual(result, {"silenceID": "abcd1234"})
            self.assertIn("--port=45123", popen.call_args.args[0])
            wait_proxy.assert_called_once()
        for args in (("monitoring", "alertmanager", "http-web", "GET", "/api/v2/status", None),
                     ("monitoring", "alertmanager", "http-web", "DELETE", "/api/v2/silences", None),
                     ("monitoring", "alertmanager", "http-web", "POST", "/api/v2/silences", None)):
            with self.subTest(args=args), self.assertRaises(OperationError):
                client.alertmanager_request(*args)

    def test_remote_proxy_cleanup_kills_only_the_recorded_pid(self):
        client = Kubernetes("/etc/kubernetes/admin.conf", control_target="ubuntu@10.20.1.28")
        with patch("scripts.operations.adapters.kubernetes.arguments", return_value=["ssh"]), \
             patch("scripts.operations.adapters.kubernetes.subprocess.run") as run:
            client._stop_remote_proxy(1234, "/run/atlas-kubekit-kubectl-proxy-test.pid")
        command = run.call_args.args[0]
        self.assertEqual(command[0:2], ["ssh", "ubuntu@10.20.1.28"])
        cleanup = command[-1]
        self.assertIn('if [ "$p" = 1234 ]; then', cleanup)
        self.assertIn('kill -TERM "$p"', cleanup)
        self.assertNotIn('kill -TERM --', cleanup)


if __name__ == "__main__":
    unittest.main()
