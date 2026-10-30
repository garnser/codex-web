"""Settled lock refusal is not renewal, and cannot revive stale custody."""
import asyncio
import threading
import unittest
from unittest.mock import Mock, patch

from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionStaleError
from codex_web.services.codex_auth_delegation import CodexAuthDelegationStaleError
from codex_web.services.execution_workers import WorkerConflictError, WorkerLeaseError
from codex_web.execution_workers import WorkerLifecycle
from codex_web.storage.postgres_state import PostgresStateStore
from codex_web.storage.state_store import StorageLockUnavailable, StorageOperationBudget, storage_operation_budget
from tests import test_runtime_supervision_custody as fixture


class CancelledStatement(Exception):
    sqlstate = '57014'


class LockClassificationTests(unittest.TestCase):
    def test_only_budgeted_advisory_acquisition_is_classified(self):
        cursor = Mock()
        original = CancelledStatement('private synthetic cancellation')
        cursor.execute.side_effect = original
        with self.assertRaises(CancelledStatement) as refused:
            PostgresStateStore._lock(cursor, 'synthetic')
        self.assertIs(refused.exception, original)
        token = storage_operation_budget.set(StorageOperationBudget(1))
        try:
            with self.assertRaises(StorageLockUnavailable) as known:
                PostgresStateStore._lock(cursor, 'synthetic')
            self.assertIs(known.exception.__cause__, original)
            cursor.execute.side_effect = ConnectionError('private synthetic disconnect')
            with self.assertRaises(ConnectionError):
                PostgresStateStore._lock(cursor, 'synthetic')
        finally:
            storage_operation_budget.reset(token)


class NativeRenewalContentionTests(unittest.IsolatedAsyncioTestCase):
    _create_assignment = fixture.SupervisionCustodyTests._create_assignment
    _session = fixture.SupervisionCustodyTests._session
    asyncSetUp = fixture.SupervisionCustodyTests.asyncSetUp
    asyncTearDown = fixture.SupervisionCustodyTests.asyncTearDown
    session = fixture.SupervisionCustodyTests.session

    @staticmethod
    def refused():
        raise StorageLockUnavailable('synthetic settled acquisition refusal')

    async def test_settled_refusal_retains_exact_live_claim_without_renewing(self):
        session, _ = await self.session()
        before = self.worker_service.store.assignment(session.assignment_id)
        self.worker_service.renew = Mock(wraps=self.worker_service.renew)
        current = await session._supervise('renewal', self.refused, seconds=.5)
        self.assertEqual(current, before)
        self.assertEqual(self.worker_service.store.assignment(before.id), before)
        self.worker_service.renew.assert_not_called()
        self.assertEqual(session._supervision_phase, 'renewal')

    async def test_expired_predecessor_is_not_replaced_by_current_lease(self):
        session, _ = await self.session()
        session._clock = lambda: session._bound_assignment.lease.expires_at + 1
        with self.assertRaises(TimeoutError):
            await session._supervise('renewal', self.refused, seconds=.5)

    async def test_changed_claim_token_or_fence_is_not_borrowed(self):
        session, _ = await self.session()
        original = self.worker_service.store.assignment(session.assignment_id)
        for field in ('lease_token', 'fence', 'execution_id', 'execution_workspace_id'):
            with self.subTest(field=field):
                def changed(state):
                    value = original
                    if field == 'lease_token':
                        value = value.model_copy(update={'lease': value.lease.model_copy(update={'lease_token': 'different-valid-length-claim-token'})})
                    elif field == 'fence':
                        value = value.model_copy(update={'fence': value.fence + 1})
                    else:
                        value = value.model_copy(update={field: 'different-' + field})
                    state.assignments[0] = value
                    return state
                self.worker_service.store.update_assignment(self.worker.id, original.id, changed)
                try:
                    with self.assertRaises((AssignmentBoundAgentProcessSessionStaleError,
                            WorkerConflictError, WorkerLeaseError, CodexAuthDelegationStaleError)):
                        await session._supervise('renewal', self.refused, seconds=.5)
                finally:
                    self.worker_service.store.update_assignment(self.worker.id, original.id,
                        lambda state: state.model_copy(update={'assignments': [original]}))

    async def test_current_credential_revocation_is_not_waived(self):
        session, _ = await self.session()
        with patch.object(session.credential_provider, 'validate_current', side_effect=CodexAuthDelegationStaleError('revoked')):
            with self.assertRaises(CodexAuthDelegationStaleError):
                await session._supervise('renewal', self.refused, seconds=.5)

    async def test_original_lease_must_remain_live_after_authority_probe(self):
        session, _ = await self.session()
        original = session._validate_current_authority
        def read_then_expire():
            current = original()
            session._clock = lambda: session._bound_assignment.lease.expires_at + 1
            return current
        with patch.object(session, '_validate_current_authority', side_effect=read_then_expire):
            with self.assertRaises(TimeoutError):
                await session._supervise('renewal', self.refused, seconds=.5)

    async def test_current_worker_revocation_is_not_waived(self):
        session, _ = await self.session()
        def revoke(state):
            state.workers[0] = state.workers[0].model_copy(update={'lifecycle': WorkerLifecycle.REVOKED})
            return state
        self.worker_service.store.update_worker(self.worker.id, revoke)
        with self.assertRaises(AssignmentBoundAgentProcessSessionStaleError):
            await session._supervise('renewal', self.refused, seconds=.5)

    async def test_unrelated_errors_and_nonrenewal_refusals_remain_failures(self):
        session, _ = await self.session()
        for error in (RuntimeError('callback failed'), CancelledStatement('non-lock SQL cancelled'), ConnectionError('disconnect')):
            with self.subTest(error=type(error).__name__):
                def fail(): raise error
                with self.assertRaises(type(error)):
                    await session._supervise('renewal', fail, seconds=.5)
        with self.assertRaises(StorageLockUnavailable):
            await session._supervise('validation', self.refused, seconds=.5)

    async def test_fresh_authority_probe_is_bounded_and_not_recursively_retried(self):
        session, _ = await self.session()
        with patch.object(session, '_validate_current_authority', side_effect=StorageLockUnavailable('second refusal')) as probe:
            with self.assertRaises(StorageLockUnavailable):
                await session._supervise('renewal', self.refused, seconds=.5)
        self.assertEqual(probe.call_count, 1)

    async def test_unsettled_original_is_not_treated_as_acquisition_refusal(self):
        session, _ = await self.session()
        release = threading.Event()
        def unfinished():
            release.wait(2)
            self.refused()
        try:
            with patch.object(session, '_validate_current_authority', wraps=session._validate_current_authority) as probe:
                with self.assertRaises(TimeoutError):
                    await session._supervise('renewal', unfinished, seconds=.02)
                probe.assert_not_called()
        finally:
            release.set()
            await asyncio.gather(*(asyncio.wrap_future(f) for f in list(session._executor_work)), return_exceptions=True)
