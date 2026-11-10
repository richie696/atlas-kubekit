"""Upgrade ordering, failure stop and fresh-plan continuation across a partial upgrade."""

import copy
import unittest
from unittest.mock import patch

from scripts.operations.configuration import normalize, template
from scripts.operations.lifecycle import upgrade
from scripts.operations.models import OperationError
from tests.test_lifecycle_maintenance import node


class Client:
    def __init__(self):
        self.nodes = {
            name: node(name, name.startswith("cp")) for name in ("cp1", "cp2", "cp3", "w1")
        }
        self.server = "v1.36.5"

    def get(self, kind, name=None, namespace=None, optional=False):
        if kind == "node":
            return (
                copy.deepcopy(self.nodes[name])
                if name
                else dict(items=copy.deepcopy(list(self.nodes.values())))
            )
        if kind in ("deployment", "daemonset", "statefulset"):
            if kind == "deployment" and name == "coredns":
                return dict(
                    metadata=dict(uid="coredns-test-uid"),
                    spec=dict(replicas=3, template=dict(spec={})),
                )
            return dict(items=[])
        return None

    def version(self):
        return self.server

    def call(self, args, **kwargs):
        if "etcdctl" in args:
            return [dict(health=True)] * 3
        if "--dry-run=server" in args and "replace" in args:
            return kwargs["value"]
        return ""


class Host:
    def __init__(self, environment, name):
        self.environment, self.name = environment, name

    def call(self, action, parameters, *args, **kwargs):
        if action == "upgrade-observe":
            version = self.environment.client.nodes[self.name]["status"]["nodeInfo"][
                "kubeletVersion"
            ]
            return dict(
                identity=dict(hostname=self.name, identityState="ok"),
                installed={key: version[1:] + "-1.1" for key in ("kubeadm", "kubelet", "kubectl")},
            )
        self.environment.effects.append(self.name)
        if self.environment.fail == self.name:
            raise OperationError("ops.host.uncertain")
        self.environment.client.nodes[self.name]["status"]["nodeInfo"]["kubeletVersion"] = (
            parameters["version"]
        )
        if parameters["firstControlPlane"]:
            self.environment.client.server = parameters["version"]
        return dict(version=parameters["version"])


class Environment:
    def __init__(self):
        self.client = Client()
        self.effects = []
        self.fail = None

    def verify(self, config):
        return config, self.client.nodes

    def host(self, config, name):
        return Host(self, name)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        config = template()
        config["versions"]["kubernetes"] = "v1.36.5"
        config["nodes"].extend(
            dict(name=name, address="192.0.2." + str(index), interface="eth0")
            for index, name in enumerate(("cp2", "cp3", "w1"), 20)
        )
        self.values = dict(configuration=normalize(config), version="v1.36.6", deleteEmptyDir=False)
        self.environment = Environment()

    def test_minor_jump_and_downgrade_are_rejected(self):
        for target in ("v1.37.0", "v1.36.4", "v1.36.5", "1.36.6"):
            with self.assertRaises(OperationError):
                upgrade.parameters(dict(self.values, version=target))

    def test_unhealthy_etcd_blocks_before_host_mutation(self):
        with patch.object(self.environment.client, "call", return_value=[dict(health=False)] * 3):
            with self.assertRaises(OperationError):
                upgrade.observe(self.values, self.environment)
        self.assertEqual(self.environment.effects, [])

    def test_serial_order_and_failure_do_not_continue_to_next_node(self):
        self.environment.fail = "cp2"
        with (
            patch.object(upgrade.maintenance, "observe"),
            patch.object(upgrade.maintenance, "apply"),
        ):
            with self.assertRaises(OperationError):
                upgrade.apply(self.values, self.environment, lambda step: None, "a" * 64)
        self.assertEqual(self.environment.effects, ["cp1", "cp2"])
        self.assertEqual(self.environment.client.server, "v1.36.6")

    def test_new_plan_continues_mixed_patch_state_without_reupgrading_completed_node(self):
        self.environment.client.server = "v1.36.6"
        self.environment.client.nodes["cp1"]["status"]["nodeInfo"]["kubeletVersion"] = "v1.36.6"
        with (
            patch.object(upgrade.maintenance, "observe"),
            patch.object(upgrade.maintenance, "apply"),
            patch.object(upgrade.configuration_cluster, "publish") as publish,
        ):
            result = upgrade.apply(self.values, self.environment, lambda step: None, "b" * 64)
        self.assertEqual(self.environment.effects, ["cp2", "cp3", "w1"])
        self.assertEqual(result["nodes"]["cp1"]["state"], "already-current")
        self.assertEqual(publish.call_args[0][1]["versions"]["kubernetes"], "v1.36.6")

    def test_kubeadm_dns_changes_are_restored_before_the_next_node_drain(self):
        original = upgrade.dns_layout.capture(self.environment.client)
        layout = copy.deepcopy(original)
        host_call = Host.call

        def upgrade_host(host, action, parameters, *args, **kwargs):
            result = host_call(host, action, parameters, *args, **kwargs)
            if action == "upgrade-apply" and host.name.startswith("cp"):
                layout["replicas"] = 2
                layout["placement"] = {"nodeSelector": {"kubernetes.io/os": "linux"}}
            return result

        def check_drain(values, environment):
            # A CoreDNS PDB requiring two healthy replicas permits a drain only
            # after kubeadm's two-replica default is restored to the saved three.
            if environment.effects and values["action"] == "drain":
                self.assertEqual(layout, original)

        def restore(client, value):
            layout.clear()
            layout.update(copy.deepcopy(value))

        with (
            patch.object(Host, "call", upgrade_host),
            patch.object(upgrade.maintenance, "observe", side_effect=check_drain),
            patch.object(upgrade.maintenance, "apply"),
            patch.object(upgrade.dns_layout, "restore", side_effect=restore),
            patch.object(upgrade.configuration_cluster, "publish"),
        ):
            upgrade.apply(self.values, self.environment, lambda step: None, "c" * 64)
        self.assertEqual(self.environment.effects, ["cp1", "cp2", "cp3", "w1"])

    def test_continuation_repairs_completed_control_plane_dns_before_pdb_retry(self):
        original = upgrade.dns_layout.capture(self.environment.client)
        current = copy.deepcopy(original)
        current["replicas"] = 2
        self.environment.client.server = "v1.36.6"
        self.environment.client.nodes["cp1"]["status"]["nodeInfo"]["kubeletVersion"] = "v1.36.6"
        values = dict(self.values, dnsLayout=original, resumeOwners=["a" * 64])

        def check_drain(values, environment):
            if values["action"] == "drain" and current != original:
                raise OperationError("ops.lifecycle.pdb")

        def restore(client, layout):
            current.clear()
            current.update(copy.deepcopy(layout))

        with (
            patch.object(
                upgrade.ClusterCoordinator,
                "journal",
                return_value={"capability": "cluster-upgrade", "state": "failed"},
            ),
            patch.object(
                upgrade.dns_layout, "capture", side_effect=lambda _: copy.deepcopy(current)
            ),
            patch.object(upgrade.dns_layout, "restore", side_effect=restore),
            patch.object(upgrade.maintenance, "observe", side_effect=check_drain),
            patch.object(upgrade.maintenance, "apply"),
            patch.object(upgrade.configuration_cluster, "publish"),
        ):
            with self.assertRaises(OperationError):
                upgrade.observe(dict(values, resumeOwners=[]), self.environment)
            facts = upgrade.observe(values, self.environment)
            self.assertTrue(facts["nodes"]["cp2"]["drainCheckDeferredUntilDnsRestore"])
            self.assertEqual(current["replicas"], 2)
            self.assertEqual(self.environment.effects, [])
            result = upgrade.apply(values, self.environment, lambda step: None, "b" * 64)
        self.assertEqual(result["nodes"]["cp1"]["state"], "already-current")
        self.assertEqual(self.environment.effects, ["cp2", "cp3", "w1"])
        self.assertEqual(current, original)


if __name__ == "__main__":
    unittest.main()
