"""Original-key recovery through real fenced broker/SQLite/READY owners.

Only the external GitLab HTTP edge is synthetic. Discarding a received response
models caller loss deterministically; no test depends on a slow provider.
"""
import copy
import json
import time
import subprocess
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

from codex_web.agent_profiles import AgentProfileUpdate
from tests import test_gitlab_pull_request_ready as ready


class RepositoryDeliveryKeyLookupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = ready.GitLabPullRequestReadyTests()
        self.f.addCleanup = self.addCleanup
        self.f.addAsyncCleanup = self.addAsyncCleanup
        await self.f.asyncSetUp()

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    def path(self, **selectors):
        return '/api/action-intents/delivery-evidence?' + urlencode({
            'repository_resource_id': self.f.target.id,
            'action_id': ready.ACTION, 'idempotency_key': 'lost-original', **selectors})

    async def create_original(self, *, lost=False):
        if lost:
            self.f.mode = 'lost'
        status, _, reply = await self.f.submit(key='lost-original')
        self.assertEqual(status, 200, reply)
        return reply['item']

    async def test_lost_reply_recovers_same_original_and_receipts_without_provider_calls(self):
        original = await self.create_original(lost=True)
        self.assertEqual(original['status'], 'uncertain')
        calls = list(self.f.calls)
        # The caller now knows only its original key, not the response/intent ID.
        status, _, reply = await self.f._request('GET', self.path())
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply['intent']['id'], original['id'])
        self.assertEqual(reply['intent']['status'], 'uncertain')
        self.assertEqual(reply['receipts'][-1]['id'], original['last_receipt_id'])
        self.assertEqual(len(self.f.intents.list(self.f.admin)), 1)
        self.assertEqual(self.f.calls, calls)
        self.assertEqual(sum(method == 'PUT' for method, _ in calls), 1)
        status, _, by_id = await self.f._request('GET', '/api/action-intents/' +
            original['id'] + '/delivery-evidence?repository_resource_id=' + self.f.target.id)
        self.assertEqual(status, 200, by_id)
        self.assertEqual(reply['intent'], by_id['intent'])
        self.assertNotIn('ready-fixture-token', json.dumps(reply))
        self.assertNotIn('lost-original', json.dumps(reply))

    async def test_successful_original_lookup_is_read_only(self):
        original = await self.create_original()
        before = list(self.f.calls)
        status, _, reply = await self.f._request('GET', self.path())
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply['intent']['id'], original['id'])
        self.assertTrue(reply['verifications'][-1]['verified'])
        self.assertEqual(self.f.calls, before)

    async def test_unknown_key_and_wrong_action_do_not_scan_or_replay(self):
        await self.create_original()
        before = list(self.f.calls)
        # Canonical exact-key index is used, not the administrator intent list.
        with patch.object(self.f.intents, 'list', side_effect=AssertionError('catalog scan forbidden')):
            for selectors in ({'idempotency_key': 'unknown'},
                              {'action_id': 'code-host.pull-request.merge'}):
                status, _, reply = await self.f._request('GET', self.path(**selectors))
                self.assertEqual(status, 200, reply)
                self.assertEqual(reply['outcome'], 'unknown')
                self.assertTrue(reply['absence_is_not_permission_to_replay'])
        self.assertEqual(self.f.calls, before)

    async def test_pending_original_remains_pending_without_claim(self):
        original_create = self.f.intents.create
        retained = []
        def create_only(*args, **kwargs):
            intent = original_create(*args, **kwargs)
            retained.append(intent)
            return intent
        with patch.object(self.f.intents, 'create', side_effect=create_only), patch.object(
                self.f.service, '_claim_and_execute_repository_intent', return_value={}):
            status, _, _ = await self.f.submit(key='lost-original')
        self.assertEqual(status, 200)
        self.assertEqual(retained[0].status, 'pending')
        status, _, reply = await self.f._request('GET', self.path())
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply['intent']['status'], 'pending')
        self.assertEqual(reply['receipts'], [])
        self.assertEqual(self.f.calls, [])

    async def test_invalid_selectors_and_mutating_method_refuse(self):
        for suffix in ('', '?action_id=' + ready.ACTION,
                       '?action_id=' + ready.ACTION + '&idempotency_key=x&idempotency_key=y'):
            status, _, _ = await self.f._request('GET', '/api/action-intents/delivery-evidence' + suffix)
            self.assertIn(status, (400, 403))
        for selectors in ({'idempotency_key': 'x'*501}, {'idempotency_key': ' '},
                          {'action_id': 'code-host.issue.create'}, {'extra': 'override'},
                          {'repository_resource_id': 'foreign'}):
            status, _, _ = await self.f._request('GET', self.path(**selectors))
            self.assertIn(status, (400, 403))
        self.assertEqual(self.f.calls, [])

    async def test_blank_duplicate_extra_and_foreign_project_selectors_refuse(self):
        original = await self.create_original()
        before = list(self.f.calls)
        for suffix in ('&idempotency_key=', '&action_id=', '&extra=',
                       '&project_id=foreign', '&project_id='):
            status, _, reply = await self.f._request('GET', self.path() + suffix)
            self.assertEqual(status, 400, reply)
            self.assertNotIn(original['id'], json.dumps(reply))
        status, _, reply = await self.f._request('GET', self.path(project_id='project-a'))
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply['intent']['id'], original['id'])
        self.assertEqual(self.f.calls, before)

    async def test_mutating_lookup_method_refuses(self):
        status, _, _ = await self.f._request('POST', self.path(), payload={})
        self.assertEqual(status, 403)
        self.assertEqual(self.f.calls, [])

    async def test_ready_origin_fence_and_execution_refuse(self):
        original = await self.create_original()
        saved = self.f.intents.store.get(original['id'])
        for change in ({'fence': self.f.assignment.fence+1}, {'assignment_id': 'foreign'}):
            def alter(state):
                state.intents = [item.model_copy(update={'readiness_origin':
                    saved.readiness_origin.model_copy(update=change)}) if item.id == saved.id else item
                    for item in state.intents]
                return state
            self.f.intents.store.update(alter)
            status, _, _ = await self.f._request('GET', self.path())
            self.assertEqual(status, 403)
        def restore_with_wrong_execution(state):
            state.intents = [saved.model_copy(update={'execution_id': 'foreign'})
                if item.id == saved.id else item for item in state.intents]
            return state
        self.f.intents.store.update(restore_with_wrong_execution)
        status, _, _ = await self.f._request('GET', self.path())
        self.assertEqual(status, 403)

    async def test_wrong_requester_and_project_refuse_without_disclosure(self):
        original = await self.create_original()
        saved = self.f.intents.store.get(original['id'])
        for change in ({'requested_by': 'foreign'}, {'project_id': 'foreign'},
                       {'resource_ids': ('foreign',)}):
            def alter(state):
                state.intents = [saved.model_copy(update=change) if item.id == saved.id else item
                    for item in state.intents]
                return state
            self.f.intents.store.update(alter)
            status, _, reply = await self.f._request('GET', self.path())
            self.assertEqual(status, 403, reply)
            self.assertNotIn(saved.id, json.dumps(reply))

    async def test_disabled_or_ambiguous_binding_refuses(self):
        await self.create_original()
        before = list(self.f.calls)
        registry = self.f.intents.execution.registry
        registry.set_enabled(self.f.binding.id, False, actor=self.f.admin)
        status, _, _ = await self.f._request('GET', self.path())
        self.assertEqual(status, 403)
        registry.set_enabled(self.f.binding.id, True, actor=self.f.admin)
        from codex_web.action_providers import ActionProviderBindingCreate
        registry.bind(ActionProviderBindingCreate(provider_type='gitlab', provider_instance='gitlab.test',
            project_id='project-a', resource_ids=(self.f.target.id,), credential_ref=self.f.secret.id),
            actor=self.f.admin, resources=self.f.resources)
        status, _, _ = await self.f._request('GET', self.path())
        self.assertEqual(status, 403)
        self.assertEqual(self.f.calls, before)

    async def test_superseded_ready_declaration_refuses(self):
        payload = copy.deepcopy(self.f.profile_payload)
        for profile in payload['profiles']:
            profile['control_plane_operations'] = [value for value in profile.get('control_plane_operations', [])
                if value != ready.OPERATION]
        self.f._publish_profile(payload, '1.1', project='project-a')
        status, _, _ = await self.f._request('GET', self.path())
        self.assertEqual(status, 403)
        self.assertEqual(self.f.calls, [])

    async def test_revoked_worker_identity_refuses(self):
        def disable(state):
            state.services = [item.model_copy(update={'disabled_at': time.time()})
                if item.id == self.f.worker_actor.identity_id else item for item in state.services]
            return state
        self.f.identity.store.update(disable)
        status, _, _ = await self.f._request('GET', self.path())
        self.assertEqual(status, 403)
        self.assertEqual(self.f.calls, [])

    async def test_current_profile_change_refuses_even_unknown_key(self):
        self.f.agent_profiles.update('orchestrator', AgentProfileUpdate(
            description='Revoked current eligibility', reason='fixture current profile drift'), actor=self.f.admin)
        status, _, _ = await self.f._request('GET', self.path())
        self.assertEqual(status, 403)

    async def test_expired_lease_and_changed_fence_refuse(self):
        original = self.f.assignment
        for change in ({'lease': original.lease.model_copy(update={'expires_at': 1})},
                       {'fence': original.fence+1}):
            self.f.change_assignment(**change)
            status, _, _ = await self.f._request('GET', self.path())
            self.assertEqual(status, 403)
            self.f.change_assignment(lease=original.lease, fence=original.fence)


class DeliveryKeyLookupPresentationTests(unittest.TestCase):
    def test_existing_inspector_renders_exact_key_read_and_no_replay_guidance(self):
        subprocess.run(['node', '--input-type=module', '-e', r'''
import fs from 'node:fs';
import assert from 'node:assert/strict';
let source=fs.readFileSync('static/control_plane_broker_view.js','utf8')
 .replace(/^import .*$/gm,'').replace(/^export \{.*$/gm,'').replace('export function','function');
source+=';return brokerDeclarationHtml;';
const render=new Function('referenceLink','projectPath','coordinationDeclarationHtml','mergeRequirementsFormHtml',source)(()=>'',()=>'',()=>'',()=>'');
const html=render({brokered_control_plane:{enabled:true,operations:[{
 id:'repository.read.delivery-evidence',path_template:'/api/action-intents/{intent_id}/delivery-evidence',
 key_lookup:{path_template:'/api/action-intents/delivery-evidence'}}]}},'fixture');
assert.ok(html.includes('Lost intent ID: GET /api/action-intents/delivery-evidence'));
assert.ok(html.includes('exact action_id and idempotency_key; unknown never permits replay'));
'''], check=True, capture_output=True, timeout=10)
