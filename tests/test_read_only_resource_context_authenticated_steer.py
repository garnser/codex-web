"""Authenticated HTTP -> real TurnService -> canonical native scope steering.

Private synthetic fixtures only; execute in the isolated operator qualifier.
"""
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from codex_web.api.turns import build_turns_router
from codex_web.execution_workers import AssignmentCancelRequest
from codex_web.identity import AuthenticationAssurance
from tests import test_read_only_resource_context_reconciliation as existing
from tests import test_turn_execution_binding as binding_fixture
from tests.read_only_resource_context_harness import NativeContextHarness, create_requester
from tests.test_recovery_scheduled_postgres import _PostgresFixture


class AuthenticatedSteerTests(unittest.IsolatedAsyncioTestCase):
    setUp = existing.RetainedNullContextSteerTests.setUp
    _setup_source = existing.ReconciledReadContextTests._setup_source
    _claim = existing.ReconciledReadContextTests._claim
    _dispatch = existing.ReconciledReadContextTests._dispatch
    _repo_harness = existing.RetainedNullContextSteerTests._repo_harness
    _retained = existing.RetainedNullContextSteerTests._retained

    async def _queued_context(self):
        self._setup_source()
        f = self.f
        f.database = self.database
        f.resources.bind_project(project=f.project, resource_id=self.database.id, actor=f.actor)
        requester = create_requester(self, f.identity, f.execution_profiles.registry, f.actor,
                                     f.project, (f.repository.id, self.database.id))
        developer = f.service.prepare_bootstrap(bootstrap_id='steer-dev', execution_id='steer-dev-exec',
            project_id=f.project.id, sandbox='workspace-write', approval_policy='on-request',
            explicit_repository_id=f.repository.id, agent_profile=self.developer_pin)
        dev = self._claim(developer)
        await self._dispatch(dev, 'handoff', {'from_agent': dev.agent_profile.profile_id,
            'to_agent': self.pin.profile_id, 'reason': 'Private authenticated steering acceptance'})
        pending = self.items.get(self.ref)
        f.workers.cancel_bootstrap(dev.id, AssignmentCancelRequest(expected_fence=dev.fence,
            reason='Private terminal sender'), actor=f.actor)
        reviewer = f.service.prepare_bootstrap(bootstrap_id='steer-vp-old', execution_id='steer-vp-old-exec',
            project_id=f.project.id, sandbox='workspace-write', approval_policy='on-request',
            explicit_repository_id=f.repository.id, agent_profile=self.pin)
        vp = self._claim(reviewer)
        await self._dispatch(vp, 'ack', {'actor': self.pin.profile_id,
            'expected_handoff_requested_at': pending.handoff.requested_at, 'accepted': True})
        self.bindings.bind(bootstrap_id='steer-vp-old', thread_id='same-thread',
            execution_id=reviewer.execution_id, assignment_id=vp.id,
            execution_workspace_id=reviewer.workspace_id, actor=f.actor)
        f.workers.cancel_bootstrap(vp.id, AssignmentCancelRequest(expected_fence=vp.fence,
            reason='Private terminal repo-only rollover'), actor=f.actor)
        harness = NativeContextHarness(self, f, items=self.items, profiles=self.profiles,
            pin=self.pin, bindings=self.bindings, requester=requester, worker_actor=self.worker_actor, ref=self.ref)
        response = await harness.submit()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['queued'])
        self.assertEqual(harness.started_assignments, [])
        original, = harness.host._thread_queue('same-thread')
        self.assertEqual(original.resource_context.requester_identity_id, requester.identity_id)
        return harness, original

    async def _positive(self, latest):
        harness, original = await self._queued_context()
        snapshot = original.model_dump(mode='json')
        path = '/api/threads/same-thread/queue/' + ('steer' if latest else original.id + '/steer')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url='http://synthetic') as client:
            response = await client.post(path, headers={'x-private-fixture-session': harness.session_token})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['ok'])
            self.assertEqual(response.json()['steeredId'], original.id)
            self.assertEqual(harness.host._thread_queue('same-thread'), [])
            duplicate = await client.post(path, headers={'x-private-fixture-session': harness.session_token})
            self.assertEqual(duplicate.status_code, 404)
        self.assertEqual(len(harness.started_assignments), 1)
        session = harness.sessions[harness.started_assignments[0]]
        starts = [p for m,p in session.requests if m == 'turn/start']
        self.assertEqual(len(starts), 1)
        actual = session.validate_current()
        self.assertEqual(actual.resource_context, original.resource_context)
        self.assertEqual(actual.agent_profile.profile_id, self.pin.profile_id)
        workspace = self.f.workspaces.get(actual.execution_workspace_id, self.f.actor)
        lease = self.f.workspaces.store.lease(workspace.lease_id)
        self.assertEqual(lease.resource_modes[self.database.id], 'read')
        self.assertEqual(lease.resource_modes[self.f.repository.id], 'write')
        self.assertEqual(original.model_dump(mode='json'), snapshot)
        self.assertEqual(harness.host.queues['held-thread'][0].model_dump(mode='json'), harness.held)

    async def test_authenticated_specific_same_requester_starts_once(self):
        await self._positive(False)

    async def test_authenticated_latest_same_requester_starts_once(self):
        await self._positive(True)

    async def test_wrong_authenticated_requester_preserves_order_before_interrupt(self):
        harness, original = await self._queued_context()
        other = create_requester(self, self.f.identity, self.f.execution_profiles.registry,
            self.f.actor, self.f.project, (self.f.repository.id, self.database.id))
        credentials = self.f.identity.create_session(identity_id=other.identity_id,
            scope=other.tenant, assurance=AuthenticationAssurance.PRIMARY)
        head = original.model_copy(update={'id': 'retained-head', 'message': 'Untouched preceding input'})
        harness.host.queues['same-thread'].insert(0, head)
        before = [q.model_dump(mode='json') for q in harness.host._thread_queue('same-thread')]
        with patch.object(harness.runtime, 'thread_is_active', return_value=True), patch.object(
                harness.runtime, 'request_for_thread', new=AsyncMock(side_effect=AssertionError('No interruption permitted'))):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url='http://synthetic') as client:
                for suffix in (original.id + '/steer', 'steer'):
                    response = await client.post('/api/threads/same-thread/queue/' + suffix,
                        headers={'x-private-fixture-session': credentials.session_token},
                        json={'actor': harness.requester.identity_id})
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json()['detail']['code'], 'resource_context_requester_changed')
                    self.assertEqual([q.model_dump(mode='json') for q in harness.host._thread_queue('same-thread')], before)
        self.assertEqual(harness.started_assignments, [])
        self.assertEqual(harness.host.queues['held-thread'][0].model_dump(mode='json'), harness.held)

    async def test_missing_http_requester_cannot_use_retained_identity(self):
        harness, original = await self._queued_context()
        # No authenticated session: request_actor must refuse at the HTTP
        # boundary rather than adopting the identity stored on the input.
        app = FastAPI()
        app.include_router(build_turns_router(harness.web, object()))
        before = [q.model_dump(mode='json') for q in harness.host._thread_queue('same-thread')]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic') as client:
            for suffix in (original.id + '/steer', 'steer'):
                response = await client.post('/api/threads/same-thread/queue/' + suffix,
                    json={'actor': harness.requester.identity_id})
                self.assertEqual(response.status_code, 401, response.text)
                self.assertEqual(response.json()['detail'], 'authentication required')
        self.assertEqual([q.model_dump(mode='json') for q in harness.host._thread_queue('same-thread')], before)
        self.assertEqual(harness.started_assignments, [])

    async def test_authenticated_null_context_does_not_inherit_current_settings(self):
        harness, session, requester = await self._repo_harness()
        original, tail = self._retained(harness, requester)
        before = original.model_dump(mode='json')
        tail_before = tail.model_dump(mode='json')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url='http://synthetic') as client:
            response = await client.post('/api/threads/same-thread/queue/' + original.id + '/steer',
                headers={'x-private-fixture-session': harness.session_token})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(session.validate_current().resource_context)
        self.assertEqual(original.model_dump(mode='json'), before)
        self.assertEqual([q.model_dump(mode='json') for q in harness.host._thread_queue('same-thread')], [tail_before])
        self.assertEqual(len([p for m,p in session.requests if m == 'turn/start']), 1)
        self.assertEqual(harness.host._thread_run_settings('same-thread').resource_context, self.context)
        self.assertEqual(harness.host.queues['held-thread'][0].model_dump(mode='json'), harness.held)


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'isolated PostgreSQL DSN required')
class PostgresAuthenticatedSteerTests(_PostgresFixture, AuthenticatedSteerTests):
    def setUp(self):
        with patch.object(binding_fixture, 'SQLiteStateStore', self.state_store_factory):
            super().setUp()
