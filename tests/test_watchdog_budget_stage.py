"""Diagnostic stage attribution; no claim about a production expiry cause.

Real watchdog, serial executor, canonical worker and controlled external process.
Only the fixture's trusted clock/sleep boundary determines lease expiry.
"""
import asyncio
import unittest

from tests import test_runtime_supervision_custody as fixtures


class WatchdogBudgetStageTests(unittest.IsolatedAsyncioTestCase):
    _create_assignment = fixtures.SupervisionCustodyTests._create_assignment
    _session = fixtures.SupervisionCustodyTests._session
    asyncSetUp = fixtures.SupervisionCustodyTests.asyncSetUp
    asyncTearDown = fixtures.SupervisionCustodyTests.asyncTearDown
    session = fixtures.SupervisionCustodyTests.session

    async def test_next_cycle_budget_failure_does_not_claim_previous_renewal(self):
        session, initial_sleep = await self.session()
        renewed = []
        actual_renew = session._heartbeat_and_renew

        def renew(*args):
            result = actual_renew(*args)
            renewed.append(result)
            return result

        async def sleep(_):
            if renewed:
                self.delegation.now = session._bound_assignment.lease.expires_at + 1

        session._heartbeat_and_renew = renew
        session._sleep = sleep
        initial_sleep.set()
        await session.watchdog_task
        self.assertEqual(len(renewed), 1)
        observation = session.status().failure_observation
        self.assertEqual(observation['code'], 'execution_timeout')
        self.assertEqual(observation['phase'], 'lease')
        self.assertEqual(observation['lease_budget_stage'], 'validation')
        self.assertEqual(observation['last_supervision_stage'], 'renewal')
        self.assertLess(observation['last_supervision_cycle'], observation['watchdog_cycle'])
        self.assertIn('renewal_seconds', observation['timings'])
        self.assertEqual(observation['timings_scope'], 'last_observed_stage_samples_may_precede_failure')

    async def test_pre_renewal_budget_failure_is_not_validation_failure(self):
        session, initial_sleep = await self.session()
        actual_validate = session.validate_current
        validated = []

        def validate():
            result = actual_validate()
            validated.append(result)
            # Successful validation finishes within its existing real budget;
            # only the next budget evaluation sees the now-expired fixture lease.
            self.delegation.now = result.lease.expires_at + 1
            return result

        session.validate_current = validate
        initial_sleep.set()
        await session.watchdog_task
        self.assertEqual(len(validated), 1)
        observation = session.status().failure_observation
        self.assertEqual(observation['code'], 'execution_timeout')
        self.assertEqual(observation['phase'], 'lease')
        self.assertEqual(observation['lease_budget_stage'], 'renewal')
        self.assertEqual(observation['last_supervision_stage'], 'validation')
        self.assertEqual(observation['last_supervision_cycle'], observation['watchdog_cycle'])
        self.assertIn('validation_seconds', observation['timings'])
        self.assertNotIn('renewal_seconds', observation['timings'])


def load_tests(loader, standard_tests, pattern):
    return loader.loadTestsFromTestCase(WatchdogBudgetStageTests)
