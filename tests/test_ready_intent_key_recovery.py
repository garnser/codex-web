"""Lost worker responses recover the original READY intent without replay, offline."""
import json
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from tests import test_gitlab_pull_request_ready as fixture


class ReadyIntentKeyRecoveryTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the composed broker/authority/provider fixture without inherited tests.
    asyncSetUp = fixture.GitLabPullRequestReadyTests.asyncSetUp
    asyncTearDown = fixture.GitLabPullRequestReadyTests.asyncTearDown
    _assignment = staticmethod(fixture.GitLabPullRequestReadyTests._assignment)
    _publish_worker_authority = fixture.GitLabPullRequestReadyTests._publish_worker_authority
    _publish_profile = fixture.GitLabPullRequestReadyTests._publish_profile
    _request = fixture.GitLabPullRequestReadyTests._request
    change_assignment = fixture.GitLabPullRequestReadyTests.change_assignment
    respond = fixture.GitLabPullRequestReadyTests.respond
    submit = fixture.GitLabPullRequestReadyTests.submit

    def lookup_path(self, **changes):
        query = dict(repository_resource_id=self.target.id,
                     action_id='code-host.pull-request.ready', idempotency_key='lost-worker-response')
        query.update(changes)
        return '/api/action-intents/by-key/delivery-evidence?' + urlencode(query)

    async def test_lost_worker_response_finds_same_single_intent_without_provider_io(self):
        # Execute the real broker path, then discard the response before the
        # worker receives any IDs. Capture the original only via private store.
        original_dispatch = self.service._execute_repository_action
        async def lose_response(*args, **kwargs):
            await original_dispatch(*args, **kwargs)
            raise RuntimeError('controlled offline response loss')
        with patch.object(self.service, '_execute_repository_action', side_effect=lose_response):
            status, _, _ = await self.submit(key='lost-worker-response')
        self.assertEqual(status, 500)
        original = self.intents.store.find_idempotent('local', 'default', self.binding.id,
            'code-host.pull-request.ready', 'lost-worker-response')
        self.assertIsNotNone(original)
        count = len(self.calls)
        status, _, found = await self._request('GET', self.lookup_path())
        self.assertEqual(status, 200, found)
        self.assertEqual(found['lookup_status'], 'found')
        self.assertEqual(found['intent']['id'], original.id)
        audit = self.service.audit.load().events[-1]
        self.assertEqual(audit.target_ref, original.id)
        self.assertNotIn('lost-worker-response', audit.model_dump_json())
        history = self.intents.history(original.id, self.admin)
        self.assertEqual([x['id'] for x in found['receipts']], [x['id'] for x in history['receipts']])
        self.assertEqual([x['id'] for x in found['verifications']], [x['id'] for x in history['verifications']])
        self.assertEqual(len(self.calls), count)
        self.assertEqual(sum(method == 'PUT' for method, _ in self.calls), 1)
        self.assertTrue(found['receipts'])
        self.assertEqual(len(self.intents.list(self.admin)), 1)
        self.assertNotIn('ready-fixture-token', json.dumps(found))

    async def test_unknown_conflicting_and_invalid_selectors_do_not_execute(self):
        status, _, found = await self._request('GET', self.lookup_path())
        self.assertEqual(status, 200, found)
        self.assertEqual(found['lookup_status'], 'unknown')
        self.assertFalse(found['replay_authorized'])
        self.assertIsNone(found['intent'])
        for changes in ({'action_id':'code-host.pull-request.merge'}, {'binding_id':'foreign-binding'},
                        {'repository_resource_id':'foreign-repository'}, {'idempotency_key':''}):
            with self.subTest(changes=changes):
                status, _, result = await self._request('GET', self.lookup_path(**changes))
                self.assertIn(status, (400, 403), result)
        status, _, _ = await self._request('GET', self.lookup_path() + '&idempotency_key=other')
        self.assertEqual(status, 400)
        self.assertEqual(self.calls, [])

    async def test_original_scope_and_ready_origin_are_enforced(self):
        _, _, body = await self.submit(key='lost-worker-response')
        intent_id = body['item']['id']
        original = self.intents.get(intent_id, self.admin)
        for changes in ({'requested_by':'foreign'}, {'organization_id':'foreign'},
                        {'workspace_id':'foreign'}, {'project_id':'foreign'},
                        {'resource_ids':('foreign',)}, {'execution_id':'foreign'},
                        {'binding_id':'foreign'}, {'action_id':'code-host.pull-request.merge'},
                        {'idempotency_key':'conflicting'},
                        {'readiness_origin':original.readiness_origin.model_copy(update={'assignment_id':'foreign'})},
                        {'readiness_origin':original.readiness_origin.model_copy(update={'fence':99})}):
            with self.subTest(changes=changes):
                # Each case is an independent authorization probe; rate limiting
                # itself is covered by the existing broker transport tests.
                self.broker._request_times.clear()
                altered = original.model_copy(update=changes)
                with patch.object(self.intents.store, 'find_idempotent', return_value=altered):
                    status, _, result = await self._request('GET', self.lookup_path())
                self.assertEqual(status, 403, result)

    async def test_live_fence_and_profile_revocation_deny_even_unknown_key(self):
        self.change_assignment(fence=self.assignment.fence + 1)
        status, _, _ = await self._request('GET', self.lookup_path())
        self.assertEqual(status, 403)
        self.change_assignment(fence=self.assignment.fence)
        self.change_assignment(lease=self.assignment.lease.model_copy(update={'expires_at':0}))
        status, _, _ = await self._request('GET', self.lookup_path())
        self.assertEqual(status, 403)
        self.change_assignment(lease=self.assignment.lease)
        self._publish_profile(self.profile_payload, '1.1', project='project-a')
        status, _, _ = await self._request('GET', self.lookup_path())
        self.assertEqual(status, 403)

    async def test_pending_and_uncertain_records_are_not_reexecuted(self):
        from codex_web.action_intents import ActionIntentStatus
        _, _, body = await self.submit(key='lost-worker-response')
        original = self.intents.get(body['item']['id'], self.admin)
        count = len(self.calls)
        for state in (ActionIntentStatus.PENDING, ActionIntentStatus.EXECUTING,
                      ActionIntentStatus.UNCERTAIN, ActionIntentStatus.REQUIRES_RECONCILIATION):
            with self.subTest(state=state):
                def change(snapshot):
                    snapshot.intents = [x.model_copy(update={'status':state}) for x in snapshot.intents]
                    return snapshot
                self.intents.store.update(change, intent_ids=(original.id,))
                status, _, found = await self._request('GET', self.lookup_path())
                self.assertEqual(status, 200, found)
                self.assertEqual(found['intent']['id'], original.id)
                self.assertEqual(found['intent']['status'], state.value)
                self.assertFalse(found['replay_authorized'])
        self.assertEqual(len(self.calls), count)

    async def test_missing_ready_declaration_and_worker_authority_deny_lookup(self):
        from tests.test_administrative_broker_runtime import AdministrativeBrokerRuntimeTests
        AdministrativeBrokerRuntimeTests._publish_worker_authority(self, repository_evidence_read=False)
        status, _, _ = await self._request('GET', self.lookup_path())
        self.assertEqual(status, 403)
        AdministrativeBrokerRuntimeTests._publish_worker_authority(self, repository_evidence_read=True)
        self.profile_payload['profiles'][-1]['control_plane_operations'] = ['repository.read.delivery-evidence']
        self._publish_profile(self.profile_payload, '1.1', project='project-a')
        status, _, _ = await self._request('GET', self.lookup_path())
        self.assertEqual(status, 403)

    async def test_current_pin_without_ready_declaration_denies_before_index(self):
        from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
        self.profile_payload['profiles'][-1]['control_plane_operations'] = ['repository.read.delivery-evidence']
        self._publish_profile(self.profile_payload, '1.2', project='project-a')
        _, reference = self.profiles.resolve('orchestration-admin', organization_id='local',
            workspace_id='default', project_id='project-a')
        assignment = self.assignment.model_copy(update={'execution_profile_definition':reference})
        with patch.object(self.intents.store, 'find_idempotent', side_effect=AssertionError('index must not be read')):
            with self.assertRaisesRegex(ControlPlaneBrokerDeniedError, 'not explicitly declared'):
                self.service._read_delivery_evidence(assignment, self.admin, 'by-key', {
                    'repository_resource_id':[self.target.id], 'action_id':['code-host.pull-request.ready'],
                    'idempotency_key':['unknown']})

    async def test_disabled_binding_and_catalog_contract(self):
        capability = self.service.public_assignment_capability(self.assignment)
        operation = next(x for x in capability['operations'] if x['id'] == 'repository.read.delivery-evidence')
        selector = operation['exact_key_lookup']
        self.assertEqual(selector['path_template'], '/api/action-intents/by-key/delivery-evidence')
        self.assertEqual(selector['supported_action_ids'], ['code-host.pull-request.ready'])
        self.intents.execution.registry.set_enabled(self.binding.id, False, actor=self.admin)
        status, _, result = await self._request('GET', self.lookup_path())
        self.assertEqual(status, 403, result)
        self.assertEqual(self.calls, [])
