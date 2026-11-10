"""Upgrades preserve scheduling intent without restoring old images or copying secrets."""
import copy
import json
import unittest

from scripts.operations.lifecycle import dns_layout, health
from scripts.operations.models import OperationError
from scripts.operations.changes.plans import safe_parameters


class Client:
    def __init__(self, value):
        self.value = value
        self.writes = 0
    def get(self, *args):
        return copy.deepcopy(self.value)
    def replace(self, value):
        self.writes += 1
        self.value = copy.deepcopy(value)
        return value


class DnsTests(unittest.TestCase):
    def deployment(self):
        return dict(metadata=dict(uid='deployment-test-uid'), spec=dict(replicas=3,
            template=dict(spec=dict(nodeSelector={'node-role.kubernetes.io/control-plane':''},
                containers=[dict(image='old-image',env=[dict(value='PRIVATE-DATA')])]))))

    def test_restoration_keeps_new_image_and_does_not_copy_secrets_to_plan(self):
        original = self.deployment()
        layout = dns_layout.project(original)
        self.assertNotIn('PRIVATE-DATA', json.dumps(layout))
        safe_parameters(dict(dnsLayout=layout))
        migrated = copy.deepcopy(original)
        migrated['spec']['template']['spec']['containers'][0]['image'] = 'new-image'
        migrated['spec']['template']['spec'].pop('nodeSelector')
        client = Client(migrated)
        dns_layout.restore(client, layout)
        self.assertEqual(dns_layout.capture(client), layout)
        self.assertEqual(client.value['spec']['template']['spec']['containers'][0]['image'], 'new-image')
        self.assertEqual(client.value['spec']['template']['spec']['containers'][0]['env'][0]['value'], 'PRIVATE-DATA')

    def test_replaced_deployment_is_rejected_without_writes(self):
        original = self.deployment()
        layout = dns_layout.project(original)
        original['metadata']['uid'] = 'another-deployment'
        client = Client(original)
        with self.assertRaises(OperationError):
            dns_layout.restore(client, layout)
        self.assertEqual(client.writes, 0)

    def test_old_ready_replicas_do_not_prove_rollout_completion(self):
        item = dict(metadata=dict(name='coredns',namespace='kube-system',uid='deployment',generation=2),
                    spec=dict(replicas=3),status=dict(readyReplicas=3,availableReplicas=3,
                    updatedReplicas=0,observedGeneration=2))
        class Workloads:
            def get(self, kind):
                return dict(items=[item] if kind=='deployment' else [])
        with self.assertRaises(OperationError):
            health.require_workloads(Workloads())
        item['status']['updatedReplicas'] = 3
        health.require_workloads(Workloads())


if __name__ == '__main__':
    unittest.main()
