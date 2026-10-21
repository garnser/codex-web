"""Canonical incident composition, using only private SQLite and mock HTTP."""
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from codex_web.action_intents import ActionIntentStatus, ActionIntentWorkItemSuccess
from codex_web.artifact_evidence import EvidenceRequirement
from codex_web.procedure_discovery import ProcedureDiscovery
from codex_web.readonly_reconciliation_runtime import (
    build_readonly_reconciliation_runtime, validate_readonly_reconciliation_target,
    ReadOnlyGitLabTransport)
from tests import test_gitlab_pull_request_ready as fixture


class ReadOnlyReconciliationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.GitLabPullRequestReadyTests.asyncSetUp
    async def asyncTearDown(self):
        for token_id in getattr(self, '_credential_ids', ()):
            self.identity.revoke_service_token(token_id, reason='private fixture cleanup',
                revoked_by=self.admin.identity_id)
        await fixture.GitLabPullRequestReadyTests.asyncTearDown(self)
    _assignment = staticmethod(fixture.GitLabPullRequestReadyTests._assignment)
    _publish_worker_authority = fixture.GitLabPullRequestReadyTests._publish_worker_authority
    _publish_profile = fixture.GitLabPullRequestReadyTests._publish_profile
    _request = fixture.GitLabPullRequestReadyTests._request
    change_assignment = fixture.GitLabPullRequestReadyTests.change_assignment
    submit = fixture.GitLabPullRequestReadyTests.submit
    respond = fixture.GitLabPullRequestReadyTests.respond

    async def prepare_target(self):
        fixture.ready_fixture.PullRequestReadyTests.configure_security_boundary(self)
        from codex_web.models import Project
        from codex_web.storage.projects import ProjectRepository
        ProjectRepository(Path(self.temp.name)/'projects.json',store=self.sqlite).save([
            Project(id=self.assignment.project_id,name='Private fixture',path=self.temp.name)])
        status, _, body = await self.submit()
        self.assertEqual(status,200,body)
        self.assertEqual(body['item']['status'],'succeeded',body)
        intent_id = body['item']['id']
        # A completed real provider result with unresolved canonical settlement,
        # as opposed to fabricating a successful receipt for an unknown write.
        self.intents._set_status(intent_id, ActionIntentStatus.REQUIRES_RECONCILIATION,
            error='fixture concurrent admission change after provider completion')
        current = self.workers.store.assignment(self.assignment.id)
        for index in range(64-len(current.procedure_discoveries)):
            self.workers.record_procedure_discovery(current.id, ProcedureDiscovery(
                actor_identity_id=self.admin.identity_id, organization_id=self.admin.organization_id,
                workspace_id=self.admin.workspace_id, project_id=current.project_id,
                resource_ids=(self.target.id,), operation='repository.read', context_checksum=str(index),
                evaluated_at=time.time(), outcome='no_match', policy_source='canonical:test', policy_allows=True),
                expected_fence=current.fence, actor=self.admin)
        return intent_id

    def build(self, intent_id):
        credential = self.identity.create_service_token(service_identity_id=self.worker_actor.identity_id,
            scope=self.admin.tenant, scopes=['action-intent:worker','secret:use'], expires_at=time.time()+300,
            created_by=self.admin.identity_id)
        self._credential_ids = [*getattr(self, '_credential_ids', ()), credential.token_id]
        with patch('httpx.AsyncHTTPTransport',return_value=httpx.MockTransport(self.respond)):
            return build_readonly_reconciliation_runtime(self.sqlite, intent_id=intent_id,
                expected_assignment_id=self.assignment.id, expected_fence=self.assignment.fence,
                worker_token=credential.token, secret_material_dir=Path(self.temp.name)/'ready-secrets',
                projects_file=Path(self.temp.name)/'projects.json',
                gitlab_api_base='https://gitlab.test/api/v4', gitlab_provider_instance='gitlab.test',
                gitlab_web_base='https://gitlab.test')

    async def test_real_canonical_composition_settles_exhausted_origin_without_provider_mutation(self):
        intent_id = await self.prepare_target()
        original = self.workers.store.assignment(self.assignment.id).procedure_discoveries
        count = len(self.calls)
        runtime = self.build(intent_id)
        with patch.object(runtime.intents.execution_workers.store,'list_assignments',
                side_effect=AssertionError('readonly exact-origin verification must not load full worker history')):
            result = await runtime.reconcile_once()
        self.assertEqual(result.status,ActionIntentStatus.SUCCEEDED,self.intents.history(intent_id,self.admin)['verifications'][-1])
        self.assertTrue(all(method=='GET' for method,_ in self.calls[count:]))
        self.assertEqual(sum(method=='PUT' for method,_ in self.calls),1)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_discoveries[:64],original)
        self.assertTrue(self.intents.history(intent_id,self.admin)['verifications'][-1]['verified'])
        self.assertIs(runtime.intents.store.store,self.sqlite)
        runtime.intents._ready_settlement_domains()  # all required canonical stores share one boundary

    async def test_credential_revoked_after_build_refuses_before_provider_or_ledger(self):
        from codex_web.services.identity import AuthenticationError
        intent_id = await self.prepare_target()
        runtime = self.build(intent_id)
        before = self.workers.store.assignment(self.assignment.id).procedure_discoveries
        count = len(self.calls)
        self.identity.revoke_service_token(self._credential_ids[-1],reason='revoked before entry',
            revoked_by=self.admin.identity_id)
        with self.assertRaises(AuthenticationError):
            await runtime.reconcile_once()
        self.assertEqual(len(self.calls),count)
        self.assertEqual(self.workers.store.assignment(self.assignment.id).procedure_discoveries,before)
        with self.assertRaisesRegex(ValueError,'already been used'):
            await runtime.reconcile_once()

    async def test_origin_projection_progresses_past_large_terminal_history_and_rebuilds_after_old_writer(self):
        intent_id = await self.prepare_target()
        store = self.intents.store
        current = store.get(intent_id)
        terminal = current.model_copy(update={'status':ActionIntentStatus.SUCCEEDED,
            'readiness_origin':None}).model_dump(mode='json')
        self.sqlite.record_apply(store.records_namespace,upserts={
            'intents:terminal-%04d'%index:{**terminal,'id':'terminal-%04d'%index} for index in range(1100)})
        with patch.object(self.sqlite,'record_items',side_effect=AssertionError('must not load retained catalogs')):
            with self.assertRaisesRegex(ValueError,'backfill is progressing'):
                store.unsettled_readiness_origin(self.assignment.id)
            self.assertEqual([item.id for item in store.unsettled_readiness_origin(self.assignment.id)],[intent_id])
            # Independent current reader processes alternate over a catalog
            # larger than one bounded pass without resetting each other's cursor.
            for epoch in ('c'*32,'d'*32):
                with patch('codex_web.storage.action_intents._ORIGIN_INDEX_EPOCH',epoch):
                    with self.assertRaisesRegex(ValueError,'backfill is progressing'):
                        store.unsettled_readiness_origin(self.assignment.id)
            for epoch in ('c'*32,'d'*32):
                with patch('codex_web.storage.action_intents._ORIGIN_INDEX_EPOCH',epoch):
                    self.assertEqual([item.id for item in store.unsettled_readiness_origin(self.assignment.id)],[intent_id])
            # An old writer does not maintain the new secondary projection.
            alias=next(iter(store._ready_origin_indexes(current)))
            self.sqlite.record_apply(store.records_namespace,upserts={alias:None})
            with patch('codex_web.storage.action_intents._ORIGIN_INDEX_EPOCH','b'*32):
                with self.assertRaisesRegex(ValueError,'backfill is progressing'):
                    store.unsettled_readiness_origin(self.assignment.id)
                self.assertEqual([item.id for item in store.unsettled_readiness_origin(self.assignment.id)],[intent_id])

    async def test_legacy_record_guard_is_read_only_and_never_migrates(self):
        from codex_web.storage.sqlite_state import SQLiteStateStore
        store = SQLiteStateStore(Path(self.temp.name)/'legacy.sqlite3')
        legacy = {'schema_version':'1.6','intents':[], 'receipts':[], 'verifications':[], 'inbox':[]}
        store.put('action_intents',legacy)
        with self.assertRaisesRegex(ValueError,'normalized records'):
            validate_readonly_reconciliation_target(store,intent_id='missing',
                expected_assignment_id='missing',expected_fence=1)
        self.assertEqual(store.get('action_intents'),legacy)
        self.assertFalse(store.record_collection_exists('action_intents.records.v1'))

    async def test_current_credential_revocation_prevents_readonly_provider_access(self):
        intent_id = await self.prepare_target()
        runtime = self.build(intent_id)
        count = len(self.calls)
        self.secret_broker.revoke(self.secret.id,actor=self.admin)
        result = await runtime.reconcile_once()
        self.assertNotEqual(result.status,ActionIntentStatus.SUCCEEDED)
        self.assertEqual(len(self.calls),count)

    async def test_missing_canonical_workers_is_typed_fail_closed(self):
        from codex_web.services.action_providers import ActionRequirementError
        intent_id = await self.prepare_target()
        runtime = self.build(intent_id)
        count = len(self.calls)
        runtime.intents.execution_workers = None
        with self.assertRaisesRegex(ActionRequirementError,'canonical execution workers'):
            await runtime.reconcile_once()
        self.assertEqual(len(self.calls),count)

    async def test_disabled_current_profile_prevents_readonly_provider_access(self):
        from codex_web.agent_profiles import AgentProfileLifecycleChange, AgentProfileLifecycle
        intent_id = await self.prepare_target()
        runtime = self.build(intent_id)
        count = len(self.calls)
        def disable(state):
            state.revisions = [profile.model_copy(update={'lifecycle':AgentProfileLifecycle.DISABLED})
                if profile.record_id == self.profile.record_id else profile for profile in state.revisions]
            return state
        self.agent_profiles.store.update(disable)
        result = await runtime.reconcile_once()
        self.assertNotEqual(result.status,ActionIntentStatus.SUCCEEDED)
        self.assertEqual(len(self.calls),count)

    async def test_work_item_or_changed_custody_refuses_precredential(self):
        intent_id = await self.prepare_target()
        count = len(self.calls)
        for field, value in (('work_item_ref','acme/app#7'), ('work_item_success',ActionIntentWorkItemSuccess(note='unsupported owning-result mutation')),
                ('expected_evidence',(EvidenceRequirement(id='artifact',evidence_type='test_result'),))):
            def change(state):
                state.intents = [item.model_copy(update={field:value}) if item.id==intent_id else item
                    for item in state.intents]
                return state
            self.intents.store.update(change,intent_ids=(intent_id,))
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_readonly_reconciliation_target(self.sqlite,intent_id=intent_id,
                    expected_assignment_id=self.assignment.id,expected_fence=self.assignment.fence)
            def restore(state):
                state.intents = [item.model_copy(update={field:() if field=='expected_evidence' else None}) if item.id==intent_id else item
                    for item in state.intents]
                return state
            self.intents.store.update(restore,intent_ids=(intent_id,))
        self.change_assignment(fence=self.assignment.fence+1)
        with self.assertRaises(ValueError):
            validate_readonly_reconciliation_target(self.sqlite,intent_id=intent_id,
                expected_assignment_id=self.assignment.id,expected_fence=self.assignment.fence)
        self.assertEqual(len(self.calls),count)

    async def test_provider_transport_blocks_write_foreign_authority_and_path(self):
        calls=[]
        async def respond(request):
            calls.append(request.method)
            return httpx.Response(200,json={})
        transport=ReadOnlyGitLabTransport('https://gitlab.test/api/v4',transport=httpx.MockTransport(respond))
        async with httpx.AsyncClient(transport=transport) as client:
            for method,url in (('PUT','https://gitlab.test/api/v4/projects/1'),
                    ('POST','https://gitlab.test/api/v4/projects/1'),
                    ('DELETE','https://gitlab.test/api/v4/projects/1'),
                    ('GET','https://other.test/api/v4/projects/1'),
                    ('GET','https://gitlab.test/other/projects/1')):
                with self.subTest(method=method,url=url),self.assertRaises(RuntimeError):
                    await client.request(method,url)
            await client.get('https://gitlab.test/api/v4/projects/1')
        self.assertEqual(calls,['GET'])
