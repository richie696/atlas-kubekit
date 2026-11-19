# SPDX-License-Identifier: MIT
import unittest

from scripts.operations.monitoring.observer.state import State, advance


class ObserverStateTest(unittest.TestCase):
    def test_failure_and_recovery_need_consecutive_observations(self):
        state, event = advance(State(), "failed", 2, 2)
        self.assertIsNone(event)
        state, event = advance(state, "failed", 2, 2)
        self.assertEqual((state.status, event), ("firing", "firing"))
        state, event = advance(state, "failed", 2, 2)
        self.assertIsNone(event)
        state, event = advance(state, "healthy", 2, 2)
        self.assertEqual((state.status, event), ("firing", None))
        state, event = advance(state, "healthy", 2, 2)
        self.assertEqual((state.status, event), ("healthy", "resolved"))

    def test_unknown_never_resolves_and_interrupts_consecutive_success(self):
        state, event = advance(State(), "failed", 1, 2)
        self.assertEqual(event, "firing")
        state, event = advance(state, "healthy", 1, 2)
        self.assertIsNone(event)
        state, event = advance(state, "unknown", 1, 2)
        self.assertEqual((state.status, state.last_observation, event), ("firing", "unknown", None))
        state, event = advance(state, "healthy", 1, 2)
        self.assertEqual((state.status, event), ("firing", None))
        state, event = advance(state, "healthy", 1, 2)
        self.assertEqual((state.status, event), ("healthy", "resolved"))

    def test_bad_policy_or_observation_is_rejected(self):
        for observation, fail, recover in (("healthy", 0, 1), ("healthy", True, 1), ("missing", 1, 1)):
            with self.subTest(observation=observation, fail=fail), self.assertRaises(ValueError):
                advance(State(), observation, fail, recover)


if __name__ == "__main__":
    unittest.main()
