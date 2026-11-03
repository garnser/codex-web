"""Canonical READ-primary RPC capture; private preflight SQLite, controlled wire only."""
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock

from codex_web.execution_workers import ExecutionAssignment
from codex_web.resources import RepositoryExecutionTarget, RepositoryExecutionScope
from codex_web.resource_context import ReadOnlyResourceContext
from codex_web.services.queued_native_resume import QueuedNativeResumeCapture, validate_resume_observation
from codex_web.services.queued_native_start import (
    QueuedNativeStartCapture, NativeStartAdmissionRefused, validate_attribution,
    reconcile_captured_start,
)
from tests import test_queued_native_start as fixtures


class ReadOnlyQueuedNativePrimaryTests(unittest.IsolatedAsyncioTestCase):
    def case(self):
        # Reuse the qualified runtime/preflight owner composition, not inherited
        # test collection. The supplied session validator owns current custody;
        # these tests do not claim native process or live lease qualification.
        f = fixtures.QueuedNativeStartTests(methodName='runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.original = f.original.model_copy(update={
            'id': 'read-original', 'execution_id': 'read-requested',
            'execution_profile_id': 'repository-read',
            'writable_repository_resource_ids': (),
            'read_only_repository_resource_ids': ('repo',),
        })
        f.attempt = f.service.retain_queued(f.original, organization_id='org', workspace_id='ws')
        _, f.claim = f.service.claim_queued_admission(f.attempt.id)
        f.service.begin_queued_start(f.attempt.id, claim_id=f.claim)
        target = RepositoryExecutionTarget(organization_id='org', workspace_id='ws', project_id='project',
            read_only_primary_repository_id='repo', read_only_repository_ids=('repo',), source='explicit')
        f.assignment = ExecutionAssignment.model_validate({**f.assignment.model_dump(),
            'sandbox': 'read-only', 'execution_profile_id': 'repository-read',
            'repository_target': target, 'repository_scope': RepositoryExecutionScope.from_target(target)})
        f.capture = QueuedNativeStartCapture(f.service, f.attempt.id, f.claim,
            'read-requested', 'thread', 'admin').bind_assignment(f.assignment,
                worker_id='worker', fence=7, validator=lambda: f.assignment)
        f.runtime.generation = 'generation'
        return f

    def wire(self, f):
        async def send(message, **kwargs):
            kwargs['start_guard']()
            if message['method'] == 'thread/resume':
                response = {'id': message['id'], 'result': {'thread': {'id': 'thread'}}}
            else:
                response = f.response(message['id'])
            await f.runtime._dispatch_message(response, generation='generation')
        f.runtime._send = AsyncMock(side_effect=send)

    async def test_readonly_primary_real_rpc_keeps_original_scope_and_captures_positive(self):
        f = self.case()
        self.wire(f)
        original = f.original.model_dump(mode='json')
        try:
            await f.runtime.request('turn/start', {'threadId': 'thread'}, start_capture=f.capture)
        except ValueError as exc:
            self.fail('Actual READ primary must pass native capture without being dropped: ' + str(exc))
        saved = f.saved()
        self.assertEqual(saved.status, 'started')
        self.assertEqual(saved.queued_turn_snapshot, original)
        correlation = saved.queued_start_correlation
        self.assertEqual(correlation.repository_resource_id, 'repo')
        self.assertEqual(correlation.writable_repository_resource_ids, ())
        self.assertEqual(correlation.read_only_repository_resource_ids, ('repo',))
        self.assertEqual(correlation.snapshot_sha256, f.service.queued_snapshot_digest(original))
        self.assertEqual((correlation.assignment_id, correlation.fence), (f.assignment.id, f.assignment.fence))
        validate_attribution(f.service, saved, correlation)
        self.assertEqual(saved.queued_start_receipt.provider_native_turn_id, 'native-turn')
        self.assertEqual(f.assignment.repository_scope.writable_repository_ids, ())
        self.assertEqual(f.assignment.repository_target.read_only_primary_repository_id, 'repo')
        f.runtime._send.assert_awaited_once()
        before = saved.model_dump(mode='json')
        reconcile_captured_start(f.service, saved.id)
        self.assertEqual(f.saved().model_dump(mode='json'), before)

    async def test_readonly_resume_then_start_retains_same_primary_and_exact_observations(self):
        f = self.case()
        self.wire(f)
        resume = QueuedNativeResumeCapture(f.service, f.attempt.id, f.claim,
            'read-requested', 'thread', 'admin').bind_assignment(f.assignment,
                worker_id='worker', fence=7, validator=lambda: f.assignment)
        await f.runtime.request('thread/resume', {'threadId': 'thread'}, start_capture=resume)
        correlation, evidence = validate_resume_observation(f.service, f.saved())
        self.assertEqual(evidence.outcome, 'resumed')
        self.assertEqual(correlation.repository_resource_id, 'repo')
        await f.runtime.request('turn/start', {'threadId': 'thread'}, start_capture=f.capture)
        self.assertEqual(f.saved().status, 'started')
        self.assertEqual(f.saved().queued_resume_correlation, correlation)
        self.assertEqual(f.saved().queued_resume_evidence, evidence)
        self.assertEqual(f.runtime._send.await_count, 2)
        self.assertEqual(f.saved().queued_turn_snapshot, f.original.model_dump(mode='json'))

    async def test_foreign_read_primary_or_missing_primary_proof_refuses_before_native_send(self):
        for change in ('foreign', 'context_only'):
            with self.subTest(change=change):
                f = self.case()
                target = RepositoryExecutionTarget(organization_id='org', workspace_id='ws', project_id='project',
                    read_only_primary_repository_id='other', read_only_repository_ids=('other',), source='explicit')
                updates = ({'repository_target': target, 'repository_scope': RepositoryExecutionScope.from_target(target),
                            'resource_ids': ('other',)} if change == 'foreign' else {'repository_target': None})
                f.assignment = ExecutionAssignment.model_validate({**f.assignment.model_dump(), **updates})
                capture = replace(f.capture, assignment=f.assignment)
                with self.assertRaisesRegex(ValueError, 'retained admission'):
                    await f.runtime.request('turn/start', {'threadId': 'thread'}, start_capture=capture)
                f.runtime._send.assert_not_called()
                self.assertIsNone(f.saved().queued_start_correlation)
                self.assertIsNone(f.saved().queued_start_receipt)
                self.assertEqual(f.saved().queued_turn_snapshot, f.original.model_dump(mode='json'))

    async def test_retained_read_scope_profile_and_snapshot_drift_do_not_become_start_proof(self):
        for name, value in (('repository_resource_id', 'other'),
                            ('writable_repository_resource_ids', ('other',)),
                            ('read_only_repository_resource_ids', ('repo', 'other')),
                            ('agent_profile_revision', 11), ('execution_profile_id', 'repository-write'),
                            ('resource_context', ReadOnlyResourceContext(schema_version='1.0',
                                read_only_resource_ids=('repo',), requester_identity_id='admin',
                                work_item_ref='group/repo#43'))):
            with self.subTest(field=name):
                f = self.case()
                c = f.capture.prepare(generation='generation', rpc_request_id='rpc-original', native_session_id='thread')
                f.service.store.update(f.attempt.id, lambda current: current.model_copy(update={name: value}))
                with self.assertRaises(ValueError):
                    f.capture.observe(c, generation='generation', message=f.response())
                self.assertIsNone(f.saved().queued_start_evidence)
                self.assertIsNone(f.saved().queued_start_receipt)
                f.runtime._send.assert_not_called()
        f = self.case()
        c = f.capture.prepare(generation='generation', rpc_request_id='rpc-original', native_session_id='thread')
        f.service.store.update(f.attempt.id, lambda current: current.model_copy(update={
            'queued_turn_snapshot': {**current.queued_turn_snapshot, 'repository_resource_id': None}}))
        with self.assertRaisesRegex(ValueError, 'retained original'):
            f.capture.observe(c, generation='generation', message=f.response())
        self.assertIsNone(f.saved().queued_start_receipt)

    async def test_current_read_custody_drift_at_actual_write_lock_still_refuses(self):
        for change in ('fence', 'profile', 'scope'):
            with self.subTest(change=change):
                f = self.case()
                original = f.assignment
                async def send(message, **kwargs):
                    if change == 'fence':
                        f.assignment = original.model_copy(update={'fence': 8})
                    elif change == 'profile':
                        f.assignment = original.model_copy(update={'agent_profile':
                            original.agent_profile.model_copy(update={'profile_revision': 11})})
                    else:
                        f.assignment = original.model_copy(update={'repository_scope': None})
                    kwargs['start_guard']()
                    self.fail('A changed assignment cannot reach native write')
                f.runtime._send = AsyncMock(side_effect=send)
                with self.assertRaises(NativeStartAdmissionRefused):
                    await f.runtime.request('turn/start', {'threadId': 'thread'}, start_capture=f.capture)
                self.assertIsNone(f.saved().queued_start_evidence)
                self.assertIsNone(f.saved().queued_start_receipt)
                self.assertEqual(f.saved().queued_turn_snapshot, f.original.model_dump(mode='json'))
