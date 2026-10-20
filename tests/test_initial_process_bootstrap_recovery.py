"""Explicit recovery of a real retained initial request on a bound live-history source."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from codex_web.models import BotBinding, QueuedNativeResumeEvidence
from codex_web.execution_workers import AssignmentStatus
from codex_web.storage.turn_queue import TurnQueueRepository
from codex_web.storage.bot_state import BotStateRepositories
from tests import test_custody_bootstrap_recovery as custody


class InitialProcessBootstrapRecoveryTests(unittest.IsolatedAsyncioTestCase):
    reopen = custody.CustodyBootstrapRecoveryTests.reopen
    make_runtime = custody.CustodyBootstrapRecoveryTests.make_runtime
    recover = custody.CustodyBootstrapRecoveryTests.recover
    custody_refusal = custody.CustodyBootstrapRecoveryTests.custody_refusal
    persist = custody.CustodyBootstrapRecoveryTests.persist
    create = custody.CustodyBootstrapRecoveryTests.create

    def setUp(self):
        custody.CustodyBootstrapRecoveryTests.setUp(self)
        source = self.worker_store.assignment('old-assignment').model_copy(update={
            'status': AssignmentStatus.CANCELLED, 'fence': 2,
            'execution_profile_id': 'repository-write',
            'failure_code': 'thread_bootstrap_cancelled',
            'failure_message': 'Pending initial native session belongs to a previous process'})
        self.service.store.store.record_apply(self.worker_store.assignment_namespace,
            upserts={source.id: source.model_dump(mode='json')})
        self.proof = dict(code='initial_bootstrap_process_lost', message='Controller refused before input',
            phase='before_turn_input', threadId='archived', preflight_attempt_id=self.attempt.id,
            admission_claim_id='owned-claim', snapshot_sha256=self.digest,
            bootstrap_id='old-bootstrap', assignment_id='old-assignment')
        self.attempt = self.service.queued_disposition(self.attempt.id, status='blocked',
            detail=self.proof, claim_id='owned-claim', preinput_refusal=True)
        self.current_binding = BotBinding(id='current-james', provider='slack',
            external_conversation_id='conversation', thread_id='archived', project_id='project',
            route_prefix='James', created_at=1, updated_at=1)
        self.bot_bindings = BotStateRepositories(self.service.store.store,
            connections_file=Path(self.directory.name) / 'connections.json',
            bindings_file=Path(self.directory.name) / 'bindings.json').bindings
        self.bot_bindings.save([self.current_binding])
        self.queue = TurnQueueRepository(self.service.store.store, Path(self.directory.name) / 'queue.json')
        self.other = self.queued.model_copy(update={'id': 'held-unknown', 'execution_id': 'held-turn',
                                                   'message': 'Independent historical unknown input'})
        self.queue.save({'archived': [self.queued, self.other]})

    def change_source(self, **changes):
        source = self.worker_store.assignment('old-assignment').model_copy(update=changes)
        self.service.store.store.record_apply(self.worker_store.assignment_namespace,
            upserts={source.id: source.model_dump(mode='json')})

    async def assert_no_publication(self):
        with self.assertRaises(HTTPException):
            await self.recover()
        self.assertIsNone(self.index.replacement_thread_id('archived'))
        self.assertEqual(self.queue.get('archived'), [self.queued, self.other])

    async def test_current_bound_nonarchived_source_recovers_same_retained_original(self):
        from tests import test_thread_list_pagination as listing
        archive = listing.ThreadListPaginationTests()._service(listing._Runtime(), self.index)
        archive.runtime_request_for_thread = AsyncMock(side_effect=HTTPException(503, 'runtime unavailable'))
        archive.project_bindings = lambda project: self.bot_bindings.load()
        archive.control_actor = self.admin
        archive.bootstrap_bindings = self.bindings
        archive.binding_service = SimpleNamespace(workers=SimpleNamespace(store=self.worker_store))
        with self.assertRaises(HTTPException) as denied:
            await archive.archive('archived')
        self.assertEqual(denied.exception.status_code, 503)
        self.assertFalse(self.index.get(self.current_binding.thread_id).archived)
        before = self.service.store.get(self.attempt.id).queued_turn_snapshot
        queue_before = self.queue.get('archived')
        preview = self.runtime.preview(self.attempt.id, project=self.project, actor=self.admin)
        self.assertTrue(preview['can_create'])
        result = await self.recover()
        self.assertEqual(result['threadId'], 'fresh')
        self.assertFalse(result['native_turn_started'])
        self.assertEqual(self.service.store.get(self.attempt.id).queued_turn_snapshot, before)
        self.assertEqual(self.queue.get('archived'), queue_before)
        self.assertFalse(self.index.get('archived').archived)
        self.assertEqual(self.bot_bindings.load(), [self.current_binding])
        self.assertEqual(self.index.replacement_thread_id('archived'), 'fresh')
        self.assertIsNone(self.service.store.get(self.attempt.id).queued_start_receipt)

    async def test_changed_source_scope_actor_profile_or_cancellation_refuses_before_creation(self):
        original = self.worker_store.assignment('old-assignment')
        for changes in ({'status': AssignmentStatus.FAILED}, {'failure_code': 'other-failure'},
                        {'created_by': 'different-actor'}, {'fence': 0},
                        {'execution_workspace_id': 'different-workspace'},
                        {'execution_profile_id': 'other-profile'},
                        {'resource_ids': ('another-repository',)},
                        {'agent_profile': original.agent_profile.model_copy(update={'profile_revision': 7})}):
            with self.subTest(changes=changes):
                self.change_source(**changes)
                await self.assert_no_publication()
                self.threads.create.assert_not_awaited()
                self.service.store.store.record_apply(self.worker_store.assignment_namespace,
                    upserts={original.id: original.model_dump(mode='json')})

    async def test_source_native_resume_or_possible_start_cannot_gain_recovery_authority(self):
        before = self.service.store.get(self.attempt.id)
        evidence = QueuedNativeResumeEvidence(runtime_generation='generation', rpc_request_id='rpc',
            provider_native_session_id='archived', outcome='resumed', observed_at=101)
        for changes in ({'queued_resume_evidence': evidence}, {'started_at': 101},
                        {'status': 'reconciliation_required'}):
            with self.subTest(changes=changes):
                self.service.store.update(before.id, lambda row: before.model_copy(update=changes))
                await self.assert_no_publication()
                self.threads.create.assert_not_awaited()

    async def test_source_change_during_creation_retains_reservation_without_route(self):
        async def change_after_creation(**values):
            response = await self.create(**values)
            self.change_source(created_by='different-actor')
            return response
        self.threads.create.side_effect = change_after_creation
        await self.assert_no_publication()
        self.assertEqual(self.service.store.get(self.attempt.id).bootstrap_recovery.state, 'pending')

    async def test_fence_change_and_source_archival_during_creation_cannot_hide_drift(self):
        async def change_after_creation(**values):
            response = await self.create(**values)
            self.change_source(fence=3)
            self.index.upsert(self.index.get('archived').model_copy(update={'archived': True}))
            return response
        self.threads.create.side_effect = change_after_creation
        await self.assert_no_publication()

    async def test_active_source_after_creation_cannot_publish_a_replacement(self):
        async def change_after_creation(**values):
            response = await self.create(**values)
            self.runtime.thread_is_active = lambda thread: thread == 'archived'
            return response
        self.threads.create.side_effect = change_after_creation
        await self.assert_no_publication()

    async def test_fresh_actor_change_during_creation_refuses_atomic_publication(self):
        async def change_after_creation(**values):
            response = await self.create(**values)
            fresh = self.worker_store.assignment('assignment').model_copy(update={'created_by': 'different-actor'})
            self.service.store.store.record_apply(self.worker_store.assignment_namespace,
                upserts={fresh.id: fresh.model_dump(mode='json')})
            return response
        self.threads.create.side_effect = change_after_creation
        await self.assert_no_publication()

    async def test_controller_claim_change_during_creation_refuses_publication(self):
        async def change_after_creation(**values):
            response = await self.create(**values)
            self.service.store.update(self.attempt.id, lambda row: row.model_copy(update={
                'queued_admission_claim_id': 'different-claim'}))
            return response
        self.threads.create.side_effect = change_after_creation
        await self.assert_no_publication()

    async def test_operator_revocation_during_creation_refuses_publication(self):
        from tests.test_execution_preflight import actor
        async def revoke_after_creation(**values):
            response = await self.create(**values)
            self.runtime.actor_resolver = lambda identity, project: actor(admin=False).model_copy(update={'identity_id': 'admin'})
            return response
        self.threads.create.side_effect = revoke_after_creation
        await self.assert_no_publication()

    def compose_real_identity(self):
        from codex_web.identity import (AuthenticationAssurance, HumanIdentity, IdentityState,
            Membership, MembershipRole, Organization, PrincipalKind, TenantScope, Workspace)
        from codex_web.services.identity import IdentityService
        from codex_web.storage.identity_state import IdentityStateStore
        identity = IdentityService(IdentityStateStore(self.service.store.store))
        identity.store.save(IdentityState(
            organizations=[Organization(id='org', name='Organization')],
            workspaces=[Workspace(id='ws', organization_id='org', name='Workspace')],
            humans=[HumanIdentity(id='admin', display_name='Operator')],
            memberships=[Membership(identity_id='admin', principal_kind=PrincipalKind.HUMAN,
                organization_id='org', workspace_id='ws', roles=[MembershipRole.ADMIN])]))
        scope = TenantScope(organization_id='org', workspace_id='ws')
        credentials = identity.create_session(identity_id='admin', scope=scope,
            assurance=AuthenticationAssurance.MFA)
        self.admin = identity.authenticate_session(credentials.session_token).actor
        self.assertEqual(self.admin.assurance, AuthenticationAssurance.MFA)
        self.expected_creation_actor = identity.actor_for_identity('admin', scope=scope)
        self.assertEqual(self.expected_creation_actor.assurance, AuthenticationAssurance.PRIMARY)
        self.runtime.identity = identity
        self.runtime.actor_resolver = lambda identity_id, project: identity.actor_for_identity(identity_id, scope=scope)
        return identity

    async def test_real_authenticated_mfa_request_accepts_current_primary_membership_projection(self):
        self.compose_real_identity()
        result = await self.recover()
        self.assertTrue(result['bootstrap_created'])
        self.assertFalse(result['native_turn_started'])
        self.assertEqual(self.index.replacement_thread_id('archived'), 'fresh')

    async def test_real_disabled_identity_during_creation_cannot_publish(self):
        identity = self.compose_real_identity()
        async def disable_after_creation(**values):
            response = await self.create(**values)
            state = identity.store.load()
            state.humans[0].disabled_at = 102
            identity.store.save(state)
            return response
        self.threads.create.side_effect = disable_after_creation
        await self.assert_no_publication()

    async def test_real_revoked_admin_membership_during_creation_cannot_publish(self):
        identity = self.compose_real_identity()
        async def revoke_after_creation(**values):
            response = await self.create(**values)
            state = identity.store.load()
            state.memberships[0].revoked_at = 102
            identity.store.save(state)
            return response
        self.threads.create.side_effect = revoke_after_creation
        await self.assert_no_publication()

    async def test_created_receipt_reconciles_without_creating_another_bootstrap(self):
        await self.recover()
        await self.recover()
        self.assertEqual(self.threads.create.await_count, 1)


if __name__ == '__main__':
    unittest.main()
