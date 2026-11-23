# SPDX-License-Identifier: MIT
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lb.apply_route import apply, load_request, recover_pending_transaction


def request(route=None):
    return {"sshUser": "ubuntu", "nodes": [
        {"name": "lb1", "ip": "10.20.1.40"},
        {"name": "lb2", "ip": "10.20.1.41"}],
        "vip": "10.20.1.43",
        "route": route or {"name": "monitoring-https", "mode": "tcp", "listenPort": 443,
                            "bind": "*", "backends": ["10.20.1.34:32443"],
                            "websocket": False}}


class FakeApp:
    def __init__(self, states):
        self.states = states
        self.config = {}
        self.probes = []
        self.applied = None

    def ensure_nodes(self):
        return None

    def get_states(self):
        return [(node, {"state": state, "hostname": node["name"]})
                for node, state in self.states]

    def ssh(self, node, payload):
        self.probes.append((node["name"], payload))
        return {"reachable": payload["backends"]}

    def change_state(self, mutate):
        for _node, state in self.states:
            mutate(state)
        self.applied = self.states[0][1]

    def t(self, key):
        return key


def states(routes=None, vips=None):
    value = {"vips": vips if vips is not None else ["10.20.1.43"],
             "routes": routes or []}
    return [({"name": name, "ip": ip}, json.loads(json.dumps(value)))
            for name, ip in (("lb1", "10.20.1.40"), ("lb2", "10.20.1.41"))]


class LoadBalancerMonitoringRouteTests(unittest.TestCase):
    def test_request_validates_pair_vip_and_fixed_https_route(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(request()))
            parsed = load_request(path)
        self.assertEqual(parsed["vip"], "10.20.1.43")
        self.assertEqual(parsed["route"]["listenPort"], 443)

    def test_request_rejects_duplicate_nodes_or_unapproved_port(self):
        data = request()
        data["nodes"][1]["name"] = "lb1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "unique lb1/lb2"):
                load_request(path)
            data = request()
            data["route"]["listenPort"] = 80
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "invalid monitoring HTTPS"):
                load_request(path)

    def test_preflights_both_lbs_then_applies_route_transaction(self):
        app = FakeApp(states())
        with patch("builtins.print"):
            self.assertEqual(apply(app, request()), 0)
        self.assertEqual([node for node, _ in app.probes], ["lb1", "lb2"])
        self.assertEqual(app.applied["routes"], [request()["route"]])

    def test_retries_transient_backend_preflight_before_applying(self):
        class TransientApp(FakeApp):
            def __init__(self):
                super().__init__(states())
                self.failures = 1

            def ssh(self, node, payload):
                self.probes.append((node["name"], payload))
                if payload["action"] == "probe-backends" and self.failures:
                    self.failures -= 1
                    raise RuntimeError("backend warming up")
                return {"reachable": payload["backends"]}

        app = TransientApp()
        with patch("scripts.lb.apply_route.time.sleep"), patch("builtins.print"):
            self.assertEqual(apply(app, request()), 0)
        self.assertEqual(len(app.probes), 3)
        self.assertEqual(app.applied["routes"], [request()["route"]])

    def test_requires_vip_on_lb_pair_and_refuses_unowned_443_conflict(self):
        app = FakeApp(states(vips=[]))
        with self.assertRaisesRegex(RuntimeError, "VIP is not registered"):
            apply(app, request())
        conflict = {"name": "other-https", "mode": "tcp", "listenPort": 443,
                    "bind": "*", "backends": ["10.20.1.99:443"], "websocket": False}
        app = FakeApp(states(routes=[conflict]))
        with self.assertRaisesRegex(RuntimeError, "already owned"):
            apply(app, request())

    def test_exact_existing_route_is_idempotent(self):
        app = FakeApp(states(routes=[request()["route"]]))
        with patch("builtins.print"):
            self.assertEqual(apply(app, request()), 0)
        self.assertIsNone(app.applied)

    def test_pending_partial_lb_transaction_is_rolled_back_before_retry(self):
        desired = request()["route"]
        new_state = {"vips": ["10.20.1.43"], "routes": [desired]}
        old_state = {"vips": ["10.20.1.43"], "routes": []}

        class PendingApp(FakeApp):
            def __init__(self):
                super().__init__([(node, json.loads(json.dumps(new_state)))
                                  for node, _state in states()])
                self.owners = {"lb1": "txn-123", "lb2": "txn-123"}
                self.actions = []

            def get_states(self):
                return [(node, {"state": state, "hostname": node["name"],
                                "transactionOwner": self.owners.get(node["name"])})
                        for node, state in self.states]

            def ssh(self, node, payload):
                self.actions.append((node["name"], payload["action"]))
                if payload["action"] == "transaction-info":
                    return {"pending": True, "token": "txn-123", "committed": node["name"] == "lb1",
                            "oldState": old_state, "newState": new_state}
                if payload["action"] == "rollback":
                    self.owners.pop(node["name"], None)
                    for row, state in self.states:
                        if row["name"] == node["name"]:
                            state.clear(); state.update(json.loads(json.dumps(old_state)))
                    return {"rolledBack": True}
                return {"reachable": payload.get("backends", [])}

        app = PendingApp()
        with patch("builtins.print"):
            recover_pending_transaction(app, request(), app.get_states())
        self.assertEqual([action for _, action in app.actions],
                         ["transaction-info", "transaction-info", "rollback", "rollback"])
        self.assertEqual(app.get_states()[0][1]["state"], old_state)

    def test_pending_transaction_with_unrelated_change_is_not_auto_recovered(self):
        desired = request()["route"]
        old_state = {"vips": ["10.20.1.43"], "routes": []}
        new_state = {"vips": ["10.20.1.43", "10.20.1.44"], "routes": [desired]}

        class UnsafeApp(FakeApp):
            def get_states(self):
                return [(node, {"state": old_state, "hostname": node["name"],
                                "transactionOwner": "txn-123"}) for node, _ in self.states]

            def ssh(self, node, payload):
                if payload["action"] == "transaction-info":
                    return {"pending": True, "token": "txn-123", "committed": False,
                            "oldState": old_state, "newState": new_state}
                raise AssertionError("unsafe transaction must not be mutated")

        app = UnsafeApp(states())
        with self.assertRaisesRegex(RuntimeError, "unrelated changes"):
            recover_pending_transaction(app, request(), app.get_states())


if __name__ == "__main__":
    unittest.main()
