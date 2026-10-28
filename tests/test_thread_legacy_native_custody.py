"""Listing a legacy thread must not replace canonical native custody."""
from contextlib import ExitStack
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_web.agent_runtime import AgentRuntimeSessionRequest
from codex_web.agent_runtime_usage import AgentRuntimeUsage
from codex_web.models import IndexedThread, Project
from codex_web.services.project_runtime import ProjectRuntimeService
from codex_web.services.projects import ProjectService
from codex_web.services.thread_scope import ThreadScopeService
from codex_web.services.threads import ThreadService
from codex_web.storage.thread_index import ThreadIndexRepository
from tests import test_agent_runtime_telemetry as telemetry_fixtures


class ThreadLegacyNativeCustodyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = telemetry_fixtures.AgentRuntimeTelemetryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.actor = self.fixture.actor
        self.sessions = self.fixture.session_service
        self.store = self.fixture.sessions
        self.db = self.store.store
        self.native = 'reasoning-native-thread'
        self.project = Project(id='project-a', name='private', path=self.fixture.temp.name,
            organization_id=self.actor.organization_id, workspace_id=self.actor.workspace_id)
        self.projects = ProjectService(SimpleNamespace(load=lambda: [self.project]))
        self.index = ThreadIndexRepository(self.db, Path(self.fixture.temp.name) / 'threads.json')
        self.index.upsert(IndexedThread(id=self.native, name='reasoning', cwd=self.project.path,
            project_id=self.project.id))
        self.runtime = SimpleNamespace(request=AsyncMock(return_value={'data': []}))
        self.service = ThreadService(runtime_transport=self.runtime,
            runtime_request_for_thread=AsyncMock(side_effect=AssertionError('native input forbidden')),
            event_sink=lambda _: None, project_runtime=ProjectRuntimeService(self.projects),
            thread_index=self.index, agent_sessions=self.sessions, control_actor=self.actor,
            thread_scope=ThreadScopeService(self.projects, self.sessions, self.index))

    def original(self, **changes):
        actor = self.actor.model_copy(update={k: changes.pop(k) for k in
            ('organization_id', 'workspace_id') if k in changes})
        project_id = changes.pop('project_id', self.project.id)
        session = self.sessions.adopt(provider_id='openai', runtime_id='codex-reasoning',
            runtime_type='codex-app-server', provider_native_session_id=self.native,
            request=AgentRuntimeSessionRequest(project_id=project_id,
                execution_id='thread-bootstrap-original', assignment_id='assignment-original',
                execution_workspace_id='execws-original', worker_id='worker-original',
                model='private-model'), actor=actor, capability_snapshot=())
        if changes:
            session = session.model_copy(update=changes)
            self.store.upsert(session)
        return session

    def aliases(self):
        return self.db.record_page(self.store.records_namespace,
            key_prefix=self.store._native_prefix(self.native), limit=2)

    async def test_list_preserves_nondefault_runtime_and_bounded_terminal_usage(self):
        original = self.original()
        before = original.model_dump(mode='json')
        turn = 'actual-original-turn'
        self.fixture.usage.upsert(AgentRuntimeUsage(
            id=self.fixture.service._record_id(original, turn),
            organization_id=original.organization_id, workspace_id=original.workspace_id,
            project_id=original.project_id, provider_id=original.provider_id,
            runtime_id=original.runtime_id, runtime_type=original.runtime_type,
            agent_session_id=original.id, assignment_id=original.assignment_id,
            execution_id=original.execution_id, execution_workspace_id=original.execution_workspace_id,
            provider_native_session_id=self.native, provider_native_turn_id=turn,
            input_tokens=17, output_tokens=3, terminal_outcome='succeeded', observed_at=100))
        rows = await self.service.list(self.project.id, actor=self.actor)
        self.assertEqual(rows['data'][0]['agentSessionId'], original.id)
        aliases, cursor = self.aliases()
        self.assertEqual(len(aliases), 1)
        self.assertIsNone(cursor)
        self.assertEqual(self.store.inspect_native(self.native).model_dump(mode='json'), before)
        observed = self.fixture.service.current_native_turns(self.actor,
            project_id=self.project.id, provider_id='openai', pairs=((self.native, turn),))
        self.assertTrue(observed['complete'])
        self.assertEqual(observed['items'][0]['usage']['input_tokens'], 17)
        self.service.runtime_request_for_thread.assert_not_awaited()

    def test_existing_default_and_closed_nondefault_sessions_are_not_rewritten(self):
        for runtime in ('codex-reasoning', 'codex'):
            with self.subTest(runtime=runtime):
                session = self.original(runtime_id=runtime, status='closed')
                before = session.model_dump(mode='json')
                item = {'id': self.native, 'cwd': self.project.path}
                self.service._adopt_legacy_thread(item, project_id=self.project.id)
                self.assertEqual(item['agentSessionId'], session.id)
                self.assertEqual(self.store.inspect_native(self.native).model_dump(mode='json'), before)

    def test_unowned_legacy_adoption_remains_default_runtime_and_idempotent(self):
        item = {'id': self.native, 'cwd': self.project.path}
        self.service._adopt_legacy_thread(item, project_id=self.project.id)
        first = self.store.inspect_native(self.native)
        self.service._adopt_legacy_thread(item, project_id=self.project.id)
        self.assertEqual((first.runtime_id, first.provider_id), ('codex', 'openai'))
        self.assertEqual(item['agentSessionId'], first.id)
        self.assertEqual(len(self.aliases()[0]), 1)

    def test_foreign_tenant_workspace_and_project_are_not_selected_or_adopted(self):
        for changes in ({'organization_id': 'foreign'}, {'workspace_id': 'foreign'},
                        {'project_id': 'foreign'}):
            with self.subTest(changes=changes):
                # Separate IDs avoid mutating a canonical identity across tenants.
                native = self.native
                self.native = native + '-' + next(iter(changes))
                session = self.original(**changes)
                item = {'id': self.native, 'cwd': self.project.path}
                with patch.object(self.sessions, 'adopt', side_effect=AssertionError('foreign adoption')):
                    self.service._adopt_legacy_thread(item, project_id=self.project.id)
                self.assertNotIn('agentSessionId', item)
                self.assertEqual(self.store.inspect_native(self.native).id, session.id)
                self.native = native

    async def test_ambiguous_runtime_aliases_refuse_without_deleting_history(self):
        session = self.original()
        self.store.upsert(session.model_copy(update={'id': 'other-retained-session', 'runtime_id': 'codex'}))
        before = self.aliases()
        rows = await self.service.list(self.project.id, actor=self.actor)
        self.assertEqual(rows['data'][0]['id'], self.native)
        self.assertNotIn('agentSessionId', rows['data'][0])
        self.assertEqual(self.aliases(), before)

    def test_stale_invalid_alias_targets_refuse_without_legacy_fallback(self):
        session = self.original()
        key = self.store._native_key(session)
        for value in ({'session_id': 'missing'}, {'session_id': session.id, 'extra': True}):
            with self.subTest(value=value):
                self.db.record_apply(self.store.records_namespace, upserts={key: value})
                item = {'id': self.native}
                self.service._adopt_legacy_thread(item, project_id=self.project.id)
                self.assertNotIn('agentSessionId', item)
                self.assertEqual(self.db.record_get(self.store.records_namespace, key), value)

    def test_adoption_lookup_is_bounded_and_does_not_scan_unrelated_history(self):
        session = self.original()
        self.db.record_apply(self.store.records_namespace,
            upserts={'sessions:unrelated-invalid': {'not': 'a canonical session'}})
        with ExitStack() as stack:
            for obj, names in ((self.store, ('load', 'list')),
                               (self.db, ('record_items', 'get', 'document_get'))):
                for name in names:
                    stack.enter_context(patch.object(obj, name, side_effect=AssertionError('history scan')))
            page = stack.enter_context(patch.object(self.db, 'record_page', wraps=self.db.record_page))
            item = {'id': self.native}
            self.service._adopt_legacy_thread(item, project_id=self.project.id)
        self.assertEqual(item['agentSessionId'], session.id)
        self.assertEqual(page.call_count, 2)
        self.assertEqual([c.kwargs['limit'] for c in page.call_args_list], [100, 2])
        self.assertTrue(all(c.kwargs['key_prefix'] == self.store._native_prefix(self.native)
                            for c in page.call_args_list))

    def test_truncated_global_ownership_refuses_before_legacy_adoption(self):
        self.original()
        with patch.object(self.db, 'record_page', return_value=({}, 'more')):
            item = {'id': self.native}
            self.service._adopt_legacy_thread(item, project_id=self.project.id)
            self.assertNotIn('agentSessionId', item)

    def test_disappearing_custody_does_not_authorize_legacy_adoption(self):
        self.original()
        with patch.object(self.store, 'native_custody', return_value=None), \
                patch.object(self.sessions, 'adopt', side_effect=AssertionError('adoption')):
            item = {'id': self.native}
            self.service._adopt_legacy_thread(item, project_id=self.project.id)
        self.assertNotIn('agentSessionId', item)
