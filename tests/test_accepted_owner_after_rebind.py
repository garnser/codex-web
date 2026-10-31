"""Canonical accepted START lineage survives recovery, never old-pin replay."""
import unittest
import time

from codex_web.agent_runtime import AgentSession, AgentSessionStatus
from codex_web.execution_subjects import ExecutionSubject
from codex_web.execution_workers import AssignmentStatus, ExecutionRuntimeBinding, NetworkPolicy
from codex_web.owner_routing import OwnerRoutingError
from codex_web.resource_context import ReadOnlyResourceContext
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingNotFoundError, ThreadBootstrapBindingService,
)
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from tests import test_owner_cancelled_new_handoff as fixtures


class AcceptedOwnerAfterRebindTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = fixtures.CancelledNewHandoffRecipientTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        self.sealed_source = False
        f.f.worker.service_identity_id = 'private-worker-identity'
        # Complete the existing model_construct fixture before START is sealed.
        runtime = ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex', capability_revision=1)
        for assignment in [*f.f.assignments.values(), f.n.template]:
            assignment.network = NetworkPolicy()
            assignment.runtime_binding = runtime
        for session in f.n.store.load().sessions:
            f.n.store.upsert(session.model_copy(update={'provider_id': 'openai', 'runtime_id': 'codex'}))
        self.bindings = ThreadBootstrapBindingService(
            ThreadBootstrapBindingStore(f.f.f.env.env.state))
        for old in f.f.bootstraps.values():
            self.bindings.bind(bootstrap_id=old.id, thread_id=old.thread_id,
                execution_id=old.execution_id, assignment_id=old.assignment_id,
                execution_workspace_id=old.execution_workspace_id, actor=f.f.f.actor)
        f.n.routing.bootstraps = self.bindings
        async def create(**kwargs):
            result = await f.create(**kwargs)
            if self.sealed_source:
                from codex_web.execution_workers import NativeCredentialCustody
                assignment = f.f.assignments[f.n.bound.assignment_id]
                # Typed seal observation fixture, matching the real CAS shape
                # established separately by the canonical seal diagnostic.
                assignment.native_credential_custody = NativeCredentialCustody(
                    runtime_secret_ref='private-runtime-reference',
                    admitted_at=time.time() - 1, requester_identity_id=assignment.created_by,
                    worker_identity_id=f.f.worker.service_identity_id, fence=assignment.fence,
                    procedure_secret_refs=('private-procedure-reference',))
                assignment.secret_refs = ('private-runtime-reference', 'private-procedure-reference')
            bound = self.bindings.bind(bootstrap_id=f.n.bound.bootstrap_id,
                thread_id=f.n.bound.thread_id, execution_id=f.n.bound.execution_id,
                assignment_id=f.n.bound.assignment_id,
                execution_workspace_id=f.n.bound.execution_workspace_id,
                actor=f.f.f.actor)
            f.n.bound = bound
            f.f.bootstraps[bound.thread_id] = bound
            return result
        f.n.threads.create.side_effect = create

    def select(self):
        return self.f.n.routing.compatible_pin(self.state, self.f.n.binding,
            'orchestrator', writable_repository_ids=('platform',))

    async def rebound(self, *, sealed_source=False):
        self.sealed_source = sealed_source
        f = self.f
        await f.test_actual_native_start_own_exclusion_delivery_and_ack()
        self.state = f.f.f.env.states.get(f.n.state.ref)
        self.original_pin = self.select()
        self.old_binding = self.bindings.get_by_thread(self.original_pin.recipient.thread_id, f.f.f.actor)
        old = f.f.assignments[self.old_binding.assignment_id]
        attempts = f.f.f.preflight.for_work_item(organization_id='org-a',
            workspace_id='workspace-a', project_id='project-a', ref=self.state.ref)
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        receipt, correlation = attempt.queued_start_receipt, attempt.queued_start_correlation
        self.assertEqual(receipt.thread_id, self.old_binding.thread_id)
        self.assertEqual(receipt.provider_native_session_id, self.old_binding.thread_id)
        self.assertEqual(correlation.assignment_id, old.id)
        self.assertEqual(old.execution_id, correlation.execution_id)
        self.assertEqual(old.execution_workspace_id, self.old_binding.execution_workspace_id)
        self.assertTrue(old.execution_workspace_id)
        # Positive typed native metadata is derived from the actual retained
        # START capture above, not absence or a guessed compatible session.
        self.historical_session = AgentSession(organization_id=old.organization_id,
            workspace_id=old.workspace_id, project_id=old.project_id,
            provider_id=receipt.provider_id, runtime_id=receipt.runtime_id, runtime_type='codex',
            provider_native_session_id=receipt.provider_native_session_id,
            assignment_id=correlation.assignment_id, execution_id=correlation.execution_id,
            execution_workspace_id=old.execution_workspace_id,
            worker_id=correlation.worker_id, resource_ids=old.resource_ids,
            status=AgentSessionStatus.READY)
        f.n.store.upsert(self.historical_session)
        self.assertEqual(f.n.routing.context_for_pin(self.original_pin, f.n.binding),
            (f.f.f.profile, f.f.f.actor))
        self.retained = [a.model_dump(mode='json') for a in attempts]
        # Existing assignment fixture models terminal drain; actual canonical
        # binding service performs the expected-assignment rebind atomically.
        self.retired = old.model_copy(deep=True, update={'status': AssignmentStatus.CANCELLED,
            'fence': old.fence + 1, 'lease': None})
        f.f.assignments[old.id] = self.retired
        token = 'd' * 32
        self.current = old.model_copy(deep=True, update={'id': 'recovered-current-owner-assignment',
            'execution_id': 'thread-bootstrap-' + token,
            'execution_workspace_id': 'recovered-current-owner-workspace',
            'lease': old.lease.model_copy(update={'lease_token': 'private-rebound-lease'}),
            'subject': ExecutionSubject(kind='thread_bootstrap', ref='bootstrap-' + token)})
        if sealed_source:
            from codex_web.execution_workers import NativeCredentialCustody
            self.current.native_credential_custody = NativeCredentialCustody(
                runtime_secret_ref=old.native_credential_custody.runtime_secret_ref)
            self.current.secret_refs = (old.native_credential_custody.runtime_secret_ref,)
        f.f.assignments[self.current.id] = self.current
        self.bindings.rebind(bootstrap_id='bootstrap-' + token,
            thread_id=self.old_binding.thread_id, execution_id=self.current.execution_id,
            assignment_id=self.current.id, execution_workspace_id=self.current.execution_workspace_id,
            actor=f.f.f.actor, expected_assignment_id=old.id)
        with self.assertRaises(ThreadBootstrapBindingNotFoundError):
            self.bindings.get_by_bootstrap(self.old_binding.bootstrap_id, f.f.f.actor)
        with self.assertRaises(OwnerRoutingError):
            f.n.routing.context_for_pin(self.original_pin, f.n.binding)

    def assert_originals_preserved(self):
        f = self.f
        self.assertEqual(f.f.assignments[self.retired.id], self.retired)
        self.assertEqual([a.model_dump(mode='json') for a in f.f.f.preflight.for_work_item(
            organization_id='org-a', workspace_id='workspace-a', project_id='project-a',
            ref=self.state.ref)], self.retained)
        f.n.threads.create.assert_awaited_once()

    async def test_fresh_pin_follows_same_native_rebind_with_positive_retired_native_custody(self):
        await self.rebound()
        fresh = self.select()
        self.assertEqual(fresh.recipient.assignment_id, self.current.id)
        self.assertEqual(fresh.recipient.thread_id, self.old_binding.thread_id)
        self.assertNotEqual(fresh.recipient, self.original_pin.recipient)
        self.assertEqual(self.f.n.routing.context_for_pin(fresh, self.f.n.binding),
            (self.f.f.f.profile, self.f.f.f.actor))
        self.assertEqual(self.f.n.store.get(self.historical_session.id,
            organization_id='org-a', workspace_id='workspace-a'), self.historical_session)
        self.assert_originals_preserved()

    async def test_fresh_pin_accepts_exact_current_ready_session_without_reusing_old_pin(self):
        await self.rebound()
        session = self.historical_session.model_copy(update={'assignment_id': self.current.id,
            'execution_id': self.current.execution_id,
            'execution_workspace_id': self.current.execution_workspace_id,
            'worker_id': self.current.assigned_worker_id})
        self.f.n.store.upsert(session)
        fresh = self.select()
        self.assertEqual(fresh.recipient.assignment_id, self.current.id)
        self.assertEqual(self.f.n.routing.context_for_pin(fresh, self.f.n.binding),
            (self.f.f.f.profile, self.f.f.f.actor))
        with self.assertRaises(OwnerRoutingError):
            self.f.n.routing.context_for_pin(self.original_pin, self.f.n.binding)
        self.assert_originals_preserved()

    async def test_missing_foreign_failed_or_inconsistent_native_custody_never_proves_recovery(self):
        await self.rebound()
        store = self.f.n.store
        before = store.load().model_copy(deep=True)
        store.update(lambda state: state.model_copy(update={
            'sessions': [s for s in state.sessions if s.id != self.historical_session.id]}))
        with self.assertRaises(OwnerRoutingError):
            self.select()
        store.update(lambda state: before.model_copy(deep=True))
        for change in ({'organization_id': 'foreign'}, {'project_id': 'foreign'},
                       {'status': AgentSessionStatus.FAILED}, {'assignment_id': 'unknown'},
                       {'execution_id': 'foreign'}, {'execution_workspace_id': 'foreign'},
                       {'worker_id': 'foreign'}, {'resource_ids': ('foreign',)},
                       {'provider_id': 'foreign'}, {'runtime_id': 'foreign'}):
            with self.subTest(change=change):
                changed = self.historical_session.model_copy(update=change)
                store.update(lambda state: state.model_copy(update={
                    'sessions': [changed if s.id == changed.id else s for s in state.sessions]}))
                with self.assertRaises(OwnerRoutingError):
                    self.select()
                store.update(lambda state: before.model_copy(deep=True))
        store.upsert(self.historical_session.model_copy(update={'id': 'ambiguous-native', 'runtime_id': 'other'}))
        with self.assertRaises(OwnerRoutingError):
            self.select()
        store.update(lambda state: before.model_copy(deep=True))
        self.assert_originals_preserved()

    async def test_active_or_leased_historical_assignment_is_not_retirement_proof(self):
        await self.rebound()
        f = self.f
        for update in ({'status': AssignmentStatus.RUNNING},
                       {'lease': self.current.lease}, {'status': AssignmentStatus.LOST}):
            with self.subTest(update=tuple(update)):
                f.f.assignments[self.retired.id] = self.retired.model_copy(deep=True, update=update)
                with self.assertRaises(OwnerRoutingError):
                    self.select()
        f.f.assignments[self.retired.id] = self.retired
        self.assert_originals_preserved()

    async def test_changed_runtime_context_network_policy_or_secret_scope_refuses_current_pin(self):
        await self.rebound()
        f = self.f
        context = ReadOnlyResourceContext(schema_version='1.0', read_only_resource_ids=('foreign',),
            requester_identity_id=f.f.f.actor.identity_id, work_item_ref=self.state.ref)
        changes = [
            {'runtime_binding': self.current.runtime_binding.model_copy(update={'runtime_id': 'other'})},
            {'resource_context': context},
            {'network': NetworkPolicy(enabled=True, allowed_hosts=('example.invalid',))},
            {'sandbox': 'workspace-write' if self.current.sandbox != 'workspace-write' else 'read-only'},
            {'approval_policy': 'never' if self.current.approval_policy != 'never' else 'on-request'},
            {'secret_refs': (*self.current.secret_refs, 'private-foreign-secret-reference')},
            {'created_by': 'foreign'},
            {'agent_profile': self.current.agent_profile.model_copy(update={'profile_revision': 999})},
            {'repository_scope': self.current.repository_scope.model_copy(update={'writable_repository_ids': ('foreign',)})},
        ]
        for change in changes:
            self.assertTrue(any(getattr(self.current, key) != value for key, value in change.items()),
                'Negative control must change canonical authority')
            with self.subTest(change=tuple(change)):
                f.f.assignments[self.current.id] = self.current.model_copy(deep=True, update=change)
                with self.assertRaises(OwnerRoutingError):
                    self.select()
        f.f.assignments[self.current.id] = self.current
        self.assert_originals_preserved()

    async def test_missing_historical_anchor_and_stale_definition_still_hold(self):
        await self.rebound()
        f = self.f
        del f.f.assignments[self.retired.id]
        with self.assertRaises(OwnerRoutingError):
            self.select()
        f.f.assignments[self.retired.id] = self.retired
        f.f.f.env.env.publish()
        with self.assertRaises(OwnerRoutingError):
            self.select()
        # Existing unresolved-original coverage is also retained in the
        # accepted-owner module; do not fabricate or replay a provider event.
        f.n.threads.create.assert_awaited_once()

    async def test_exact_pending_owner_pin_survives_only_recorded_initial_seal_expansion(self):
        await self.rebound(sealed_source=True)
        f = self.f
        pending = self.select()
        original_pin = pending.model_dump(mode='json')
        self.assertEqual(f.n.routing.context_for_pin(pending, f.n.binding),
            (f.f.f.profile, f.f.f.actor))
        self.assertIsNone(self.current.native_credential_custody.admitted_at)
        # Canonical typed observation, NOT a substitute SecretUse/provider/CAS
        # implementation: Root's independent real-owner diagnostic covers CAS.
        marker = self.retired.native_credential_custody.model_copy(update={
            'admitted_at': time.time(), 'fence': self.current.fence,
            'requester_identity_id': self.current.created_by,
            'worker_identity_id': f.f.worker.service_identity_id})
        sealed = self.current.model_copy(deep=True, update={
            'native_credential_custody': marker,
            'secret_refs': (marker.runtime_secret_ref, *marker.procedure_secret_refs)})
        f.f.assignments[self.current.id] = sealed
        self.assertEqual(f.n.routing.context_for_pin(pending, f.n.binding),
            (f.f.f.profile, f.f.f.actor))
        self.assertEqual(pending.model_dump(mode='json'), original_pin)
        with self.assertRaises(OwnerRoutingError):
            f.n.routing.context_for_pin(self.original_pin, f.n.binding)
        for change in (
            {'secret_refs': (*sealed.secret_refs, 'private-unrecorded-reference')},
            {'network': NetworkPolicy(enabled=True, allowed_hosts=('example.invalid',))},
            {'fence': sealed.fence + 1},
            {'native_credential_custody': marker.model_copy(update={'worker_identity_id': 'foreign'})},
        ):
            with self.subTest(change=tuple(change)):
                f.f.assignments[self.current.id] = sealed.model_copy(deep=True, update=change)
                with self.assertRaises(OwnerRoutingError):
                    f.n.routing.context_for_pin(pending, f.n.binding)
        f.f.assignments[self.current.id] = sealed
        self.assertEqual(pending.model_dump(mode='json'), original_pin)
        self.assert_originals_preserved()
