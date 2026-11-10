"""History is never executable, and failed maintenance has explicit ownership."""
from datetime import datetime, timedelta, timezone
import unittest

from scripts.operations.changes import continuation, execution, plans
from scripts.operations.changes.coordinator import ClusterCoordinator
from scripts.operations.lifecycle import scheduling
from scripts.operations.models import OperationError
from tests.test_change_execution import Client


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.coordinator = ClusterCoordinator(self.client)
        self.plan = plans.make('cluster-upgrade', self.client.cluster_identity(), {},
                               {'version': 'v1.36.6'}, now=datetime.now(timezone.utc)-timedelta(hours=2))

    def test_expired_history_can_be_replanned_but_never_executed(self):
        with self.assertRaises(OperationError):
            plans.load(self.plan.record())
        previous = plans.load(self.plan.record(), history=True)
        with self.coordinator.hold(previous.id):
            self.coordinator.record(previous.id, 'failed', 'upgrade-cp2', capability=previous.capability)
        values = continuation.parameters(previous, self.coordinator)
        self.assertEqual(values['resumeOwners'], [previous.id])
        self.assertNotIn('resumeOwners', previous.parameters)
        with self.assertRaises(OperationError):
            execution.execute(previous.record(), previous.id, self.coordinator, lambda p: {},
                              lambda p,c: self.fail('expired plan executed'), lambda p:p)

    def test_crashed_running_journal_is_cancelled_before_lock_is_released(self):
        with self.coordinator.hold(self.plan.id):
            self.coordinator.record(self.plan.id, 'running', 'upgrade-cp2', capability='cluster-upgrade')
            self.coordinator.retain = True
        status = self.coordinator.status()
        with self.assertRaises(OperationError):
            continuation.parameters(self.plan, self.coordinator)
        self.coordinator.recover(status['data']['owner'], status['metadata']['resourceVersion'],
                                 'OLD COORDINATOR AND REMOTE JOBS STOPPED')
        record = self.coordinator.journal(self.plan.id)
        self.assertEqual((record['state'], record['step'], record['capability']),
                         ('cancelled', 'upgrade-cp2', 'cluster-upgrade'))
        self.assertEqual(continuation.parameters(self.plan, self.coordinator)['resumeOwners'], [self.plan.id])

    def test_namespace_cannot_fork_a_second_cluster_lock(self):
        with self.assertRaises(OperationError):
            ClusterCoordinator(self.client, 'another-namespace')


if __name__ == '__main__':
    unittest.main()
