"""Real private admission/outbox composition; only the host process is fake."""
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from tests.test_home_host_action_intents import HomeHostActionIntentTests
from codex_web.services.home_host_commands import admit_host_command, execute_host_command, HOST_COMMAND
from codex_web.services.control_plane_broker import ControlPlaneBrokerService, ControlPlaneBrokerRequestError
from codex_web.services.action_intents import ActionIntentConflictError
from codex_web.trusted_host_backend import trusted_host_deployment_options


class HomeHostCommandTests(HomeHostActionIntentTests):
    def command_payload(self):
        return {'parameters': {'argv': ['/usr/bin/true']}, 'idempotency_key': 'original-command'}

    async def test_real_broker_admission_executes_once_and_preserves_original(self):
        result = await execute_host_command(self.service, self.assignment, self.worker_actor, self.command_payload())
        self.assertEqual(result['item']['status'], 'succeeded')
        self.assertEqual(self.effects, 1)
        again = await execute_host_command(self.service, self.assignment, self.worker_actor, self.command_payload())
        self.assertEqual(again['item']['id'], result['item']['id'])
        self.assertEqual(self.effects, 1)
        original = self.intents.get(result['item']['id'], self.actor)
        self.assertIsNone(original.request.idempotency_key)
        self.assertEqual(original.retry_policy.max_attempts, 1)
        self.assertEqual(self.current().status, 'running')

    async def test_uncertain_after_host_effect_never_replays(self):
        self.after_spawn = lambda: self.select(None, 1)
        result = await execute_host_command(self.service, self.assignment, self.worker_actor, self.command_payload())
        self.assertEqual(result['item']['status'], 'uncertain')
        self.assertEqual(self.effects, 1)
        with self.assertRaises(Exception):
            await execute_host_command(self.service, self.assignment, self.worker_actor, self.command_payload())
        self.assertEqual(self.effects, 1)

    async def test_changed_same_key_command_is_conflict_before_effect(self):
        admit_host_command(self.service, self.assignment, self.worker_actor, self.command_payload())
        changed = self.command_payload(); changed['parameters']['argv'] = ['/usr/bin/false']
        with self.assertRaises(ActionIntentConflictError):
            admit_host_command(self.service, self.assignment, self.worker_actor, changed)
        self.assertEqual(self.effects, 0)

    async def test_worker_cannot_override_assignment_or_resource(self):
        for field in ('assignment_id', 'fence', 'resource_ids', 'requested_by'):
            payload = self.command_payload(); payload['parameters'][field] = 'override'
            with self.assertRaises(ValidationError):
                admit_host_command(self.service, self.assignment, self.worker_actor, payload)
        self.assertEqual(self.effects, 0)

    async def test_changed_home_selection_denies_before_admission(self):
        self.select(None, 1)
        with self.assertRaises(Exception):
            admit_host_command(self.service, self.assignment, self.worker_actor, self.command_payload())
        self.assertEqual(self.effects, 0)


class HostCommandWireTests(unittest.TestCase):
    def test_route_and_payload_contract(self):
        resolved = ControlPlaneBrokerService._resolve_operation('POST', HOST_COMMAND.path_template)
        self.assertEqual(resolved.operation.id, HOST_COMMAND.id)
        for method, target in [('GET', HOST_COMMAND.path_template), ('POST', HOST_COMMAND.path_template + '?x=1')]:
            with self.assertRaises(ControlPlaneBrokerRequestError):
                ControlPlaneBrokerService._resolve_operation(method, target)
        public = ControlPlaneBrokerService.public_operation(HOST_COMMAND)
        self.assertIn('idempotency_key', public['request_body']['required'])

    def test_host_execution_requires_explicit_uid_and_opt_in(self):
        self.assertIsNone(trusted_host_deployment_options({}))
        self.assertIsNone(trusted_host_deployment_options({'CODEX_WEB_TRUSTED_HOME_HOST_UID': '1000'}))
        with patch('codex_web.trusted_host_backend.os.geteuid', return_value=1000):
            self.assertEqual(trusted_host_deployment_options({'CODEX_WEB_TRUSTED_HOME_HOST_ENABLED': 'true',
                'CODEX_WEB_TRUSTED_HOME_HOST_UID': '1000'}), {'enabled': True, 'expected_uid': 1000})
            for value in ('', '0', '-1', '١٠٠٠'):
                with self.assertRaises(ValueError):
                    trusted_host_deployment_options({'CODEX_WEB_TRUSTED_HOME_HOST_ENABLED': '1',
                        'CODEX_WEB_TRUSTED_HOME_HOST_UID': value})


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([unittest.TestSuite(HomeHostCommandTests(n) for n in HomeHostCommandTests.__dict__
        if n.startswith('test_')), loader.loadTestsFromTestCase(HostCommandWireTests)])
