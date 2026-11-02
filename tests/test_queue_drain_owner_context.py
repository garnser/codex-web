"""Detached queue ownership; SQLite admission is real, native I/O is synthetic."""
from __future__ import annotations

import asyncio
import contextvars
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException

from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.models import Project, QueuedNativeStartReceipt
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.turns import TurnService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.state_store import StorageOperationBudget, check_operation_budget, storage_operation_budget
from tests import test_turn_execution as fixtures


class QueueDrainOwnerContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = fixtures._Host()
        self.host._project = lambda _: Project(id='project', name='Project', path='/private')
        self.runtime = TurnExecutionService(self.host)
        self.preflight = ExecutionPreflightService(ExecutionPreflightStore(
            SQLiteStateStore(Path(self.temp.name) / 'state.sqlite')))
        self.runtime.queued_preflight = self.preflight
        self.original = self.runtime.enqueue_turn(thread_id='thread', project_id='project',
            message='one retained request', source='home-operator-automation:private-run',
            repository_resource_id='repo', writable_repository_resource_ids=('repo',))
        self.attempt = self.preflight.retain_queued(self.original,
            organization_id='local', workspace_id='default')
        self.snapshot = self.attempt.queued_turn_snapshot
        self.runtime.start_thread_turn_now = AsyncMock(return_value={'turn': {'id': 'native-turn'}})
        self.admin = AuthenticationActor(identity_id='admin', principal_kind=PrincipalKind.HUMAN,
            organization_id='local', workspace_id='default', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA)
        # Use the normal TurnService retry owner, with no unrelated routing dependencies.
        self.turns = TurnService.__new__(TurnService)
        self.turns.preflight = self.preflight
        self.turns.execution = self.runtime
        self.turns.recovery = SimpleNamespace(replacement_thread_id=lambda _: None)

    async def test_detached_owner_survives_caller_budget_retired_after_durable_claim(self):
        parent = StorageOperationBudget(60)
        begin = self.preflight.begin_queued_start
        def retire_caller(attempt_id, *, claim_id):
            # Deterministic phase boundary: durable claim and queue pop already happened.
            parent.cancelled.set()
            return begin(attempt_id, claim_id=claim_id)
        self.preflight.begin_queued_start = retire_caller
        token = storage_operation_budget.set(parent)
        try:
            self.runtime.schedule_queue_drain('thread')
            owned = self.runtime.queue_drain_tasks['thread']
            error = None
            try:
                await owned
            except TimeoutError as exc:
                error = type(exc).__name__
            self.assertIs(storage_operation_budget.get(), parent)
        finally:
            storage_operation_budget.reset(token)
        saved = self.preflight.store.get(self.attempt.id)
        self.assertEqual(saved.status, 'started', {'status': saved.status, 'owner_error': error})
        self.assertIsNone(error)
        self.assertEqual(saved.queued_turn_snapshot, self.snapshot)
        self.runtime.start_thread_turn_now.assert_awaited_once()
        self.assertEqual(self.host._thread_queue('thread'), [])
        self.assertNotIn('thread', self.runtime.queue_drain_tasks)

    async def test_detached_owner_keeps_other_context_and_coalesces(self):
        identity = contextvars.ContextVar('private_test_identity')
        identity_token = identity.set(('admin', 'local', 'default'))
        parent = StorageOperationBudget(60)
        parent.cancelled.set()
        budget_token = storage_operation_budget.set(parent)
        entered, release = asyncio.Event(), asyncio.Event()
        async def native(*args, **kwargs):
            check_operation_budget()
            self.assertIsNone(storage_operation_budget.get())
            self.assertEqual(identity.get(), ('admin', 'local', 'default'))
            entered.set()
            await release.wait()
            return {'turn': {'id': 'native-turn'}}
        self.runtime.start_thread_turn_now = AsyncMock(side_effect=native)
        try:
            self.runtime.schedule_queue_drain('thread')
            owned = self.runtime.queue_drain_tasks['thread']
            await asyncio.wait_for(entered.wait(), 3)
            self.runtime.schedule_queue_drain('thread')
            self.assertIs(self.runtime.queue_drain_tasks['thread'], owned)
            release.set()
            await owned
            self.assertIs(storage_operation_budget.get(), parent)
        finally:
            storage_operation_budget.reset(budget_token)
            identity.reset(identity_token)
        self.runtime.start_thread_turn_now.assert_awaited_once()

    async def test_direct_drain_keeps_its_callers_budget_and_original(self):
        parent = StorageOperationBudget(60)
        parent.cancelled.set()
        token = storage_operation_budget.set(parent)
        try:
            with self.assertRaises(TimeoutError):
                await self.runtime.drain_thread_queue('thread')
        finally:
            storage_operation_budget.reset(token)
        self.assertEqual(self.preflight.store.get(self.attempt.id).status, 'queued')
        self.assertEqual([q.id for q in self.host._thread_queue('thread')], [self.original.id])
        self.runtime.start_thread_turn_now.assert_not_awaited()

    async def test_detached_unknown_native_outcome_is_held_without_replay(self):
        self.runtime.start_thread_turn_now.side_effect = RuntimeError('synthetic external transport outcome unknown')
        parent = StorageOperationBudget(60)
        parent.cancelled.set()
        token = storage_operation_budget.set(parent)
        try:
            self.runtime.schedule_queue_drain('thread')
            await self.runtime.queue_drain_tasks['thread']
        finally:
            storage_operation_budget.reset(token)
        saved = self.preflight.store.get(self.attempt.id)
        self.assertEqual(saved.status, 'reconciliation_required')
        self.assertEqual(saved.queued_turn_snapshot, self.snapshot)
        self.assertEqual(self.host._thread_queue('thread'), [])
        await self.runtime.drain_thread_queue('thread')
        self.runtime.start_thread_turn_now.assert_awaited_once()
        with self.assertRaises(HTTPException) as refusal:
            await self.turns.retry_preflight('thread', self.attempt.id, actor=self.admin)
        self.assertEqual(refusal.exception.detail['code'], 'queued_start_reconciliation_required')

    def claim_and_remove(self):
        _, claim = self.preflight.claim_queued_admission(self.attempt.id)
        self.assertIsNotNone(claim)
        self.assertEqual(self.runtime.pop_queued_turn('thread', self.original.id).id, self.original.id)
        return claim

    async def test_existing_retry_fences_admitting_owner_then_starts_same_original_once(self):
        old_claim = self.claim_and_remove()
        self.runtime.schedule_queue_drain = Mock()
        response = await self.turns.retry_preflight('thread', self.attempt.id, actor=self.admin)
        self.assertEqual(response['queuedId'], self.original.id)
        current = self.preflight.store.get(self.attempt.id)
        self.assertEqual(current.status, 'queued')
        self.assertEqual(current.attempt_number, 2)
        self.assertEqual(current.queued_turn_snapshot, self.snapshot)
        self.assertIsNone(current.queued_admission_claim_id)
        self.assertFalse(self.preflight.begin_queued_start(self.attempt.id, claim_id=old_claim))
        # The old owner's late error cannot overwrite the new phase/claim.
        late = self.preflight.queued_disposition(self.attempt.id, status='reconciliation_required', claim_id=old_claim)
        self.assertEqual(late, current)
        receipt = QueuedNativeStartReceipt(admission_claim_id=old_claim,
            requested_execution_id=current.execution_id, execution_id='old-bootstrap',
            thread_id='thread', provider_native_session_id='thread', provider_native_turn_id='old-turn',
            provider_id='openai', runtime_id='codex', observed_at=1)
        with self.assertRaises(ValueError):
            self.preflight.record_queued_native_start(self.attempt.id, receipt)
        await self.runtime.drain_thread_queue('thread')
        saved = self.preflight.store.get(self.attempt.id)
        self.assertEqual(saved.status, 'started')
        self.assertNotEqual(saved.queued_admission_claim_id, old_claim)
        self.assertEqual(saved.queued_turn_snapshot, self.snapshot)
        call = self.runtime.start_thread_turn_now.await_args.kwargs
        self.assertEqual(call['message'], self.original.message)
        self.assertEqual(call['repository_resource_id'], 'repo')
        self.assertEqual(call['writable_repository_resource_ids'], ('repo',))
        self.runtime.start_thread_turn_now.assert_awaited_once()

    async def test_begin_wins_retry_cannot_requeue_unknown_original(self):
        claim = self.claim_and_remove()
        self.assertTrue(self.preflight.begin_queued_start(self.attempt.id, claim_id=claim))
        before = self.preflight.store.get(self.attempt.id)
        with self.assertRaises(HTTPException) as refusal:
            await self.turns.retry_preflight('thread', self.attempt.id, actor=self.admin)
        self.assertEqual(refusal.exception.detail['code'], 'queued_start_reconciliation_required')
        self.assertEqual(self.preflight.store.get(self.attempt.id), before)
        self.assertEqual(self.host._thread_queue('thread'), [])
        self.runtime.start_thread_turn_now.assert_not_awaited()

    async def test_begin_wins_between_retry_read_and_cas_remains_held(self):
        claim = self.claim_and_remove()
        claim_retry = self.preflight.claim_retry
        def begin_first(*args, **kwargs):
            self.assertTrue(self.preflight.begin_queued_start(self.attempt.id, claim_id=claim))
            return claim_retry(*args, **kwargs)
        self.preflight.claim_retry = begin_first
        self.runtime.schedule_queue_drain = Mock()
        response = await self.turns.retry_preflight('thread', self.attempt.id, actor=self.admin)
        self.assertTrue(response['deduplicated'])
        self.assertNotIn('queuedId', response)
        self.assertEqual(self.preflight.store.get(self.attempt.id).status, 'reconciliation_required')
        self.assertEqual(self.host._thread_queue('thread'), [])
        self.runtime.schedule_queue_drain.assert_not_called()
        self.runtime.start_thread_turn_now.assert_not_awaited()

    async def test_existing_retry_rejects_member_and_foreign_tenant_without_mutation(self):
        self.claim_and_remove()
        before = self.preflight.store.get(self.attempt.id)
        for change in ({'roles': (MembershipRole.MEMBER,)}, {'organization_id': 'foreign'}):
            with self.subTest(change=change), self.assertRaises(HTTPException):
                await self.turns.retry_preflight('thread', self.attempt.id,
                    actor=self.admin.model_copy(update=change))
            self.assertEqual(self.preflight.store.get(self.attempt.id), before)
            self.assertEqual(self.host._thread_queue('thread'), [])
        self.runtime.start_thread_turn_now.assert_not_awaited()
