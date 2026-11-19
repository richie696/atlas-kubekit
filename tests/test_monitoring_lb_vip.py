# SPDX-License-Identifier: MIT
import unittest
from unittest.mock import patch

from scripts.operations.capabilities import monitoring_lb_vip


class LoadBalancerVipMetricsCapabilityTest(unittest.TestCase):
    def test_apply_passes_the_observed_host_state_as_compare_and_swap_guard(self):
        expected = {"installed": False, "active": False, "enabled": False, "digest": None}
        config = {"cluster": {"apiVIP": "10.20.1.39", "appVIP": ""}, "nodes": []}
        value = {"configuration": config,
                 "expected": {"configurationRevision": "rev", "nodes": {"lb1": expected}}}
        observed = []

        class Host:
            def call(self, action, parameters, run_id, timeout):
                observed.append((action, parameters, run_id, timeout))
                return {"identity": {}, "state": {"installed": True, "active": True,
                                                     "enabled": True, "digest": "desired"}}

        class Environment:
            def verify(self, configuration):
                return configuration, {}

            def host(self, configuration, name):
                return Host()

        checkpoints = []
        with patch.object(monitoring_lb_vip, "parameters", side_effect=lambda raw: raw), \
             patch.object(monitoring_lb_vip, "_nodes", return_value=[
                 {"name": "lb1", "interface": "enp6s18"}]), \
             patch.object(monitoring_lb_vip, "_vips", return_value=["10.20.1.39"]), \
             patch.object(monitoring_lb_vip, "check_identity"):
            monitoring_lb_vip.apply(value, Environment(), checkpoints.append, "plan")

        self.assertEqual(observed[0][0], "lb-vip-metrics-apply")
        self.assertEqual(observed[0][1]["expectedState"], expected)
        self.assertEqual(checkpoints, ["monitoring-lb-vip-exporter-lb1-start",
                                       "monitoring-lb-vip-exporter-lb1-complete"])


if __name__ == "__main__":
    unittest.main()
