"""Repeat deployment compares LB markers without weakening data-disk policy."""
import copy
import unittest
from unittest.mock import patch

from scripts.operations.configuration import template
from scripts.operations import deployment_runtime
from scripts.operations.models import OperationError


class DeploymentGuardTests(unittest.TestCase):
    def test_lb_without_data_disk_accepts_both_persisted_markers(self):
        current = template()
        desired = copy.deepcopy(current)
        current["nodes"][0]["dataDevice"] = "none"
        with patch.object(deployment_runtime.configuration_cluster, "read", return_value=current):
            deployment_runtime.guard(object(), desired)
        with patch.object(deployment_runtime.configuration_cluster, "read", return_value=desired):
            deployment_runtime.guard(object(), current)

    def test_kubernetes_disk_or_lb_interface_change_still_conflicts(self):
        current = template()
        for index, field, value in ((1, "dataDevice", "none"), (0, "interface", "eth9")):
            desired = copy.deepcopy(current)
            desired["nodes"][index][field] = value
            with patch.object(deployment_runtime.configuration_cluster, "read", return_value=current):
                with self.assertRaises(OperationError) as error:
                    deployment_runtime.guard(object(), desired)
            self.assertEqual(error.exception.key, "ops.config.conflict")


if __name__ == "__main__":
    unittest.main()
