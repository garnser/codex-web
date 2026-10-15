"""Controlled real API -> canonical service -> SQLite -> GitHub transport tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.responses import JSONResponse

from codex_web.api.authorization import install_api_authorization, ApiAuthorizationError
from codex_web.api.work_items import build_work_items_router
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind, MembershipRole
from codex_web.integrations.github_client import GitHubClient
from codex_web.models import GitHubTaskSourceSettings, TaskSourceIdentity, WorkItemState, WorkItemProgressUpdate
from codex_web.services.github_task_source import GitHubTaskSource
from codex_web.services.task_source_projection import ensure_projection
from codex_web.services.task_source_runtime import TaskSourceRegistry, TaskSourceWritebackService
from codex_web.services.work_items import WorkItemService
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.services.work_item_dependencies import WorkItemRuntimeDependencies
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests.test_work_item_state_machine import _Host
from tests.test_api_authorization import FakeAuthority
from codex_web.authority import AuthorityDecisionOutcome


class GitHubProgressProjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = SQLiteStateStore(self.root / 'state.db')
        self.repo = ModelMapRepository(self.store, namespace='work_item_states',
            legacy_path=self.root / 'work.json', model=WorkItemState)
        self.state = WorkItemState(ref='acme/app#4', project_id='project',
            current_owner='vp-engineering', next_action='before',
            source_identity=TaskSourceIdentity(source_type='github', source_instance='https://api.github.test',
                external_id='acme/app#4', revision='r1'),
            last_meaningful_update_at=1, updated_at=1, created_at=1)
        self.repo.put(self.state.ref, self.state)
        host = _Host(self.root)
        deps = WorkItemRuntimeDependencies.from_host(host)
        self.deps = replace(deps, get_state=self.repo.get, save_state=lambda state: self.repo.put(state.ref, state),
            update_state=self.repo.update, load_states=self.repo.load, save_states=self.repo.save)
        self.machine = WorkItemStateMachine(dependencies=self.deps, store=self.store)
        self.mapping = {'vp-engineering': 'engineer'}
        self.failure = None
        self.hook = None
        self.interleave = None
        self.provider = {'number': 4, 'title': 'Issue', 'state': 'open', 'updated_at': 'r1',
            'assignees': [{'login': 'human'}], 'labels': []}
        self.calls = []
        self.registry = TaskSourceRegistry()
        self.registry.register('github', lambda state: GitHubTaskSource('https://api.github.test', 'PRIVATE-TOKEN',
            client=GitHubClient(transport=httpx.MockTransport(self.transport)),
            settings=GitHubTaskSourceSettings(assignee_mapping=self.mapping)))
        self.service = object.__new__(WorkItemService)
        self.service.state_machine = self.machine
        self.service.work_items = self.deps
        self.service.task_source_registry = self.registry
        self.service.continuity = SimpleNamespace(schedule_actionable_owner_dispatch=Mock(),
            schedule_actionable_owner_continuity_check=Mock())
        self.service.recovery = SimpleNamespace(schedule=Mock())
        self.service.publish_event = AsyncMock()
        from codex_web.services.task_source_work_items import TaskSourceWorkItemProjector
        self.service.task_source_projector = TaskSourceWorkItemProjector(None, self.machine, dependencies=self.deps)
        self.restart_coordinator()
        self.actor = AuthenticationActor(identity_id='operator', principal_kind=PrincipalKind.HUMAN,
            organization_id='local', workspace_id='default', roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.MFA)
        self.authority = FakeAuthority()
        app = FastAPI()
        app.include_router(build_work_items_router(self.service))
        auth = install_api_authorization(app, self.authority)
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            request.state.tenant_scope = self.actor.tenant
            try:
                auth.authorize_request(request, self.actor)
            except ApiAuthorizationError as exc:
                return JSONResponse({'detail': str(exc)}, status_code=exc.status_code)
            return await call_next(request)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.path = '/api/work-items/acme/app%234'

    def restart_coordinator(self):
        self.service.task_source_writeback = TaskSourceWritebackService(None, self.registry, dependencies=self.deps, append_event=self.machine._append_work_item_event)

    async def transport(self, request):
        self.calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        if self.interleave:
            self.interleave(request)
        if request.method == 'GET':
            if '/assignees/' in request.url.path:
                if self.failure == 'validation-denied':
                    return httpx.Response(403, json={'message': 'PRIVATE-TOKEN ' + 'x' * 10000})
                return httpx.Response(204)
            return httpx.Response(200, json=dict(self.provider))
        # Assert the canonical mutation AND pending evidence predate provider IO.
        committed = self.repo.get(self.state.ref)
        assert committed.next_action == 'qualified next action'
        assert committed.source_projection.status == 'pending'
        if self.failure == '422':
            return httpx.Response(422, json={'message': 'PRIVATE-TOKEN ' + 'x' * 10000})
        if self.failure == 'timeout-before':
            raise httpx.ReadTimeout('PRIVATE-TOKEN provider payload', request=request)
        if request.method == 'POST':
            login = json.loads(request.content)['assignees'][0]
            if login not in {v['login'] for v in self.provider['assignees']}:
                self.provider['assignees'].append({'login': login})
        elif request.method == 'PATCH':
            self.provider.update(json.loads(request.content))
        self.provider['updated_at'] = 'r2'
        if self.failure == 'timeout-after':
            raise httpx.ReadTimeout('PRIVATE-TOKEN provider payload', request=request)
        return httpx.Response(200, json=self.provider)

    def progress(self):
        response = self.client.post(self.path + '/progress', json={
            'actor': 'vp-engineering', 'current_owner': 'vp-engineering',
            'next_action': 'qualified next action', 'note': 'controlled progress'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['ok'])
        readback = self.client.get(self.path)
        self.assertEqual(readback.status_code, 200)
        self.assertEqual(readback.json()['next_action'], 'qualified next action')
        self.assertEqual(readback.json()['current_owner'], None if readback.json()['current_stage'] == 'closed' else 'vp-engineering')
        self.assertEqual(readback.json(), response.json()['item'])
        return readback.json()['source_projection']

    def retry(self, revision):
        return self.client.post(self.path + '/projection-retry', json={'expected_revision': revision})

    def test_422_commit_readback_restart_retry_only_projection(self):
        self.failure = '422'
        projection = self.progress()
        self.assertEqual(projection['status'], 'failed')
        self.assertEqual(projection['http_status'], 422)
        self.assertNotIn('PRIVATE-TOKEN', json.dumps(projection))
        events = (self.root / 'work_item_events.jsonl').read_bytes()
        dispatches = self.service.continuity.schedule_actionable_owner_dispatch.call_count
        self.restart_coordinator()
        self.failure = None
        response = self.retry(projection['desired_revision'])
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()['item']
        self.assertEqual(result['source_projection']['status'], 'succeeded')
        self.assertEqual(result['source_projection']['actor'], 'operator')
        self.assertEqual(result['source_projection']['attempts'], 2)
        after_events = [json.loads(line) for line in (self.root / 'work_item_events.jsonl').read_text().splitlines()]
        before_events = [json.loads(line) for line in events.splitlines()]
        domain = lambda items: [row for row in items if not row['event_type'].startswith('task_source_projection_')]
        self.assertEqual(domain(after_events), domain(before_events))
        self.assertTrue(any(row['event_type'] == 'task_source_projection_failed' for row in after_events))
        self.assertTrue(any(row['event_type'] == 'task_source_projection_succeeded' for row in after_events))
        self.assertEqual(self.service.continuity.schedule_actionable_owner_dispatch.call_count, dispatches)
        self.assertEqual({v['login'] for v in self.provider['assignees']}, {'human', 'engineer'})
        writes = sum(method != 'GET' for method, _, _ in self.calls)
        self.assertEqual(self.retry(projection['desired_revision']).status_code, 200)
        self.assertEqual(sum(method != 'GET' for method, _, _ in self.calls), writes)

    def test_timeout_after_mutation_readback_recovers_without_second_write(self):
        self.failure = 'timeout-after'
        projection = self.progress()
        self.assertEqual(projection['reason'], 'provider_timeout_outcome_unknown')
        self.restart_coordinator()
        self.failure = None
        before = sum(method != 'GET' for method, _, _ in self.calls)
        response = self.retry(projection['desired_revision'])
        self.assertEqual(response.json()['item']['source_projection']['status'], 'succeeded')
        self.assertEqual(sum(method != 'GET' for method, _, _ in self.calls), before)

    def test_timeout_before_mutation_can_retry_projection(self):
        self.failure = 'timeout-before'
        projection = self.progress()
        self.failure = None
        self.assertEqual(self.retry(projection['desired_revision']).json()['item']['source_projection']['status'], 'succeeded')

    def test_missing_mapping_preserves_manual_assignees_until_explicit_mapping(self):
        self.mapping = {}
        projection = self.progress()
        self.assertEqual(projection['reason'], 'github_assignee_mapping_missing')
        self.assertFalse(any(method != 'GET' for method, _, _ in self.calls))
        self.assertEqual(self.provider['assignees'], [{'login': 'human'}])
        self.mapping = {'vp-engineering': 'engineer'}
        self.assertEqual(self.retry(projection['desired_revision']).json()['item']['source_projection']['status'], 'succeeded')

    def test_invalid_and_ambiguous_mapping_is_rejected_by_canonical_configuration(self):
        for mapping in ({'role': '@bad'}, {'role': 'a--b'}, {'role': 'a' * 40}, {' ': 'valid'}, {'one': 'same', 'two': 'SAME'}):
            with self.subTest(mapping=mapping), self.assertRaises(ValidationError):
                GitHubTaskSourceSettings(assignee_mapping=mapping)

    def test_provider_denial_is_visible_and_does_not_grant_role_or_clear_humans(self):
        self.failure = 'validation-denied'
        projection = self.progress()
        self.assertEqual(projection['http_status'], 403)
        self.assertFalse(any(method != 'GET' for method, _, _ in self.calls))
        self.assertEqual(self.repo.get(self.state.ref).current_owner, 'vp-engineering')

    def test_newer_external_change_conflicts_before_mutation(self):
        self.failure = '422'
        projection = self.progress()
        self.provider['updated_at'] = 'external-r3'
        self.failure = None
        before = len(self.calls)
        response = self.retry(projection['desired_revision'])
        result = response.json()['item']['source_projection']
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['reason'], 'provider_revision_changed')
        self.assertTrue(all(method == 'GET' for method, _, _ in self.calls[before:]))

    def test_newer_canonical_state_and_cross_tenant_or_denied_retry_cannot_write(self):
        self.failure = '422'
        projection = self.progress()
        self.failure = None
        state = self.repo.get(self.state.ref)
        state.current_owner = 'cto'
        self.machine._save_work_item_state(state)
        before = len(self.calls)
        self.assertEqual(self.retry(projection['desired_revision']).status_code, 409)
        revision = self.repo.get(self.state.ref).source_projection.desired_revision
        self.authority.outcome = AuthorityDecisionOutcome.DENY
        self.assertEqual(self.retry(revision).status_code, 403)
        self.authority.outcome = AuthorityDecisionOutcome.ALLOW
        self.actor = self.actor.model_copy(update={'workspace_id': 'other'})
        self.assertEqual(self.retry(revision).status_code, 404)
        self.assertEqual(len(self.calls), before)

    def test_newer_canonical_change_during_provider_read_is_fenced(self):
        def change():
            state = self.repo.get(self.state.ref)
            state.current_owner = 'cto'
            self.machine._save_work_item_state(state)
        self.hook = change
        response = self.client.post(self.path + '/progress', json={'next_action': 'qualified next action'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['item']['current_owner'], 'cto')
        self.assertFalse(any(method != 'GET' for method, _, _ in self.calls))
        self.assertEqual(self.repo.get(self.state.ref).source_projection.desired_owner, 'cto')

    def test_committed_pending_state_survives_response_loss_before_provider_call(self):
        # Process dies after canonical commit, before entering writeback.
        from codex_web.models import WorkItemProgressUpdate
        committed = self.machine._structured_progress(self.state.ref, WorkItemProgressUpdate(next_action='qualified next action'))
        self.assertEqual(committed.source_projection.status, 'pending')
        # Reopen the actual database as a new process would.
        reopened = SQLiteStateStore(self.root / 'state.db')
        repo = ModelMapRepository(reopened, namespace='work_item_states', legacy_path=self.root / 'unused.json', model=WorkItemState)
        persisted = repo.get(self.state.ref)
        self.assertEqual(persisted.source_projection.desired_revision, committed.source_projection.desired_revision)
        self.restart_coordinator()
        response = self.retry(persisted.source_projection.desired_revision)
        self.assertEqual(response.json()['item']['source_projection']['status'], 'succeeded')

    def test_human_state_changes_during_assignability_or_assignee_await_are_conflicts(self):
        for initial, changed, stage in [('open', 'closed', 'implementation_active')]:
            for boundary in ['assignability', 'assignee']:
                with self.subTest(initial=initial, boundary=boundary):
                    self.provider.update(state=initial, updated_at='r1', assignees=[{'login': 'human'}])
                    state = self.state.model_copy(deep=True)
                    state.current_stage = stage
                    self.repo.put(state.ref, state)
                    self.restart_coordinator()
                    fired = []
                    def interleave(request):
                        match = (request.method == 'GET' and '/assignees/' in request.url.path) if boundary == 'assignability' else request.method == 'POST'
                        if match and not fired:
                            fired.append(True)
                            self.provider.update(state=changed, updated_at='human-r2')
                            self.provider['assignees'].append({'login': 'manual-user'})
                    self.interleave = interleave
                    start = len(self.calls)
                    projection = self.progress()
                    self.assertEqual(projection['status'], 'conflict')
                    self.assertEqual(projection['reason'], 'provider_state_changed')
                    self.assertEqual(projection['protected_source_state'], initial)
                    self.assertEqual(projection['observed_state'], changed)
                    self.assertEqual(self.provider['state'], changed)
                    self.assertFalse(any(method == 'PATCH' for method, _, _ in self.calls[start:]))
                    self.interleave = None
                    self.restart_coordinator()
                    retried = self.retry(projection['desired_revision']).json()['item']['source_projection']
                    self.assertEqual(retried['status'], 'conflict')
                    self.assertEqual(self.provider['state'], changed)

    def test_no_noncommutative_state_patch_without_supported_atomic_precondition(self):
        # A state PATCH await would itself admit a human state transition.
        # GitHub has no documented state CAS, so that path fails closed.
        for initial, stage in [('closed', 'implementation_active'), ('open', 'closed')]:
            with self.subTest(initial=initial):
                self.provider.update(state=initial, updated_at='r1', assignees=[{'login': 'human'}])
                state = self.state.model_copy(deep=True)
                state.current_stage = stage
                self.repo.put(state.ref, state)
                projection = self.progress()
                self.assertEqual(projection['reason'], 'github_state_transition_requires_reconciliation')
                self.assertEqual(projection['status'], 'conflict')
                self.assertFalse(any(method == 'PATCH' for method, _, _ in self.calls))
                self.assertEqual(self.provider['state'], initial)

    def test_concurrent_manual_assignees_survive_each_await_and_response_loss(self):
        for boundary in ['assignability', 'assignee']:
            for timeout in [None, 'timeout-after']:
                with self.subTest(boundary=boundary, timeout=timeout):
                    self.provider.update(state='open', updated_at='r1', assignees=[{'login': 'human'}])
                    self.repo.put(self.state.ref, self.state)
                    self.restart_coordinator()
                    fired = []
                    def interleave(request):
                        match = (request.method == 'GET' and '/assignees/' in request.url.path) if boundary == 'assignability' else request.method == 'POST'
                        if match and not fired:
                            fired.append(True)
                            self.provider['assignees'].append({'login': 'manual-user'})
                            self.provider['updated_at'] = 'manual-r2'
                    self.interleave = interleave
                    self.failure = timeout
                    projection = self.progress()
                    self.interleave = None
                    self.failure = None
                    if timeout:
                        self.restart_coordinator()
                        projection = self.retry(projection['desired_revision']).json()['item']['source_projection']
                    self.assertEqual(projection['status'], 'succeeded')
                    self.assertEqual({v['login'] for v in self.provider['assignees']}, {'human', 'manual-user', 'engineer'})

    def test_response_loss_with_human_close_stays_conflicted_after_restart(self):
        def interleave(request):
            if request.method == 'POST':
                self.provider.update(state='closed', updated_at='human-r2')
        self.interleave = interleave
        self.failure = 'timeout-after'
        projection = self.progress()
        self.assertEqual(projection['reason'], 'provider_timeout_outcome_unknown')
        self.restart_coordinator()
        self.interleave = None
        self.failure = None
        response = self.retry(projection['desired_revision'])
        self.assertEqual(response.json()['item']['source_projection']['status'], 'conflict')
        self.assertEqual(self.provider['state'], 'closed')
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.calls))

    def test_storage_failure_is_not_reported_as_provider_success(self):
        import asyncio
        from codex_web.services.task_source_projection import TaskSourceProjectionStorageError
        state = self.machine._structured_progress(self.state.ref,
            WorkItemProgressUpdate(next_action='qualified next action'))
        fault = replace(self.deps, update_state=Mock(side_effect=RuntimeError('database unavailable')))
        self.service.task_source_writeback.dependencies = fault
        with self.assertRaises(TaskSourceProjectionStorageError):
            asyncio.run(self.service.task_source_writeback.sync(state))
        self.assertFalse(self.calls)
        self.assertEqual(self.repo.get(state.ref).source_projection.status, 'pending')

    def test_revoked_execution_fence_during_provider_await_prevents_projection_mutation(self):
        import asyncio
        from codex_web.models import WorkItemProgressUpdate
        state = self.machine._structured_progress(self.state.ref, WorkItemProgressUpdate(next_action='qualified next action'))
        live_fence = [True]
        self.hook = lambda: live_fence.__setitem__(0, False)
        def authorize():
            if not live_fence[0]:
                raise RuntimeError('assignment fence changed')
        result = asyncio.run(self.service.task_source_writeback.retry_projection(state,
            expected_revision=state.source_projection.desired_revision, actor='operator', authorization_guard=authorize))
        self.assertEqual(result.source_projection.reason, 'projection_authority_changed')
        self.assertEqual(result.source_projection.status, 'conflict')
        self.assertFalse(any(method != 'GET' for method, _, _ in self.calls))

    def test_mapping_change_during_assignability_is_visible_before_mutation(self):
        def interleave(request):
            if '/assignees/' in request.url.path:
                self.mapping = {'vp-engineering': 'replacement-user'}
        self.interleave = interleave
        projection = self.progress()
        self.assertEqual(projection['status'], 'conflict')
        self.assertEqual(projection['reason'], 'provider_configuration_changed')
        self.assertFalse(any(method != 'GET' for method, _, _ in self.calls))

    def test_provider_identity_mismatch_never_updates_receipt_or_source_target(self):
        self.provider['number'] = 999
        projection = self.progress()
        self.assertEqual(projection['reason'], 'provider_identity_mismatch')
        self.assertEqual(projection['status'], 'conflict')
        self.assertEqual(self.repo.get(self.state.ref).source_identity.external_id, 'acme/app#4')
        self.assertFalse(any(method != 'GET' for method, _, _ in self.calls))

    def test_operator_reconciliation_supersedes_failed_desire_without_progress_replay(self):
        self.failure = '422'
        projection = self.progress()
        self.failure = None
        self.provider.update(state='closed', updated_at='2026-10-15T10:00:00Z')
        response = self.client.post(self.path + '/reconcile', json={})
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()['item']
        self.assertEqual(item['current_stage'], 'closed')
        self.assertNotEqual(item['source_projection']['desired_revision'], projection['desired_revision'])
        self.assertEqual(self.retry(projection['desired_revision']).status_code, 409)
        response = self.retry(item['source_projection']['desired_revision'])
        self.assertEqual(response.json()['item']['source_projection']['status'], 'succeeded')
        events = [json.loads(line) for line in (self.root / 'work_item_events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(row['event_type'] == 'progress_updated' for row in events), 1)
        self.assertFalse(any(method == 'PATCH' for method, _, _ in self.calls))

    def test_provider_accounts_are_never_inferred_as_canonical_roles(self):
        source = self.registry.resolve(self.state)
        snapshot = source._snapshot(self.provider, repo='acme/app')
        projection = source.project(snapshot)
        self.assertIsNone(projection.owner)
        self.assertFalse(projection.owner_known)
        self.provider['assignees'].append({'login': 'engineer'})
        projection = source.project(source._snapshot(self.provider, repo='acme/app'))
        self.assertEqual(projection.owner, 'vp-engineering')
