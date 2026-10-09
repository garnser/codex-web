from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException

from codex_web.models import BotBinding, IndexedThread
from codex_web.services.thread_naming import ThreadNamingService
from tests import test_thread_list_pagination as listing

_Runtime = listing._Runtime
_thread = listing._thread
from tests.test_thread_recovery_service import recovery_service


class OrchestratorArchivalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.helpers = listing.ThreadListPaginationTests()
        self.repository = self.helpers._repository(Path(self.directory.name))

    async def test_failed_provider_archive_remains_archived_after_discovery_and_restart(self):
        old = _thread(1, name='Orchestrator')
        current = _thread(2, name='Orchestrator')
        self.repository.upsert_many([old, current])
        request = AsyncMock(side_effect=RuntimeError('provider unavailable'))
        recovery = recovery_service(SimpleNamespace(), thread_index=self.repository, runtime_request=request)
        self.assertFalse(await recovery.archive_replaced_bot_thread(old.id, current.id))
        reopened = self.helpers._repository(Path(self.directory.name))
        self.assertTrue(reopened.get(old.id).archived)
        runtime = _Runtime([old.model_dump(), current.model_dump()])
        service = self.helpers._service(runtime, reopened)
        for _ in range(2):
            response = await service.list('home', search='Orchestrator')
            self.assertEqual([row['id'] for row in response['data']], [current.id])
        # History projection remains discoverable through the archive index.
        archived, _, _ = reopened.page(project_id='home', archived=True, search=None, after=None, limit=10)
        self.assertEqual([row.id for row in archived], [old.id])

    async def test_missing_predecessor_index_uses_replacement_project_for_archive(self):
        current = _thread(2, name='Orchestrator')
        self.repository.upsert(current)
        recovery = recovery_service(SimpleNamespace(), thread_index=self.repository, runtime_request=AsyncMock(side_effect=RuntimeError('offline')))
        await recovery.archive_replaced_bot_thread('missing-old', current.id)
        old = self.repository.get('missing-old')
        self.assertTrue(old.archived)
        self.assertEqual(old.project_id, current.project_id)

    async def test_explicit_unarchive_allows_runtime_discovery_again(self):
        old = _thread(1, name='Orchestrator', archived=True)
        self.repository.upsert(old)
        runtime = _Runtime([old.model_copy(update={'archived': False}).model_dump()])
        service = self.helpers._service(runtime, self.repository)
        service.runtime_request_for_thread = AsyncMock(return_value={})
        await service.unarchive(old.id)
        response = await service.list('home')
        self.assertEqual([row['id'] for row in response['data']], [old.id])

    async def test_project_bound_recovery_thread_is_repaired_once_and_scope_is_preserved(self):
        current = _thread(2, name='Orchestrator').model_copy(update={'project_id': None})
        self.repository.upsert(current)
        other = _thread(3, project_id='other', name='Orchestrator')
        self.repository.upsert(other)
        def binding(number, tid, project='home'):
            return BotBinding(id=str(number), provider='slack', external_conversation_id=str(number), thread_id=tid,
                project_id=project, route_prefix='Orchestrator', thread_name='Orchestrator', created_at=1, updated_at=2)
        bindings = [binding(1, current.id), binding(2, current.id), binding(3, other.id, 'other')]
        service = self.helpers._service(_Runtime(), self.repository)
        service.project_bindings = Mock(return_value=bindings)
        response = await service.list('home', search='Orchestrator')
        self.assertEqual([row['id'] for row in response['data']], [current.id])
        self.assertEqual(self.repository.get(current.id).project_id, 'home')
        self.assertEqual(self.repository.get(other.id).project_id, 'other')
        service.project_bindings.assert_called_once_with('home')
        before = self.repository.revision(project_id='home')
        await service.list('home')
        self.assertEqual(self.repository.revision(project_id='home'), before)

    async def test_naming_preserves_project_and_archival_when_provider_metadata_is_partial(self):
        old = _thread(1, name='Orchestrator', archived=True)
        self.repository.upsert(old)
        request = AsyncMock(return_value={'thread': {'cwd': '/repo/home'}})
        naming = ThreadNamingService(request, self.repository, lambda: [], event_sink=lambda _: None)
        await naming.set_name(old.id, 'Orchestrator history')
        indexed = self.repository.get(old.id)
        self.assertEqual(indexed.project_id, 'home')
        self.assertTrue(indexed.archived)
        self.assertEqual(indexed.path, old.path)

    async def test_local_archive_retains_dead_unbound_assignment_history(self):
        old = _thread(1, name='Orchestrator')
        self.repository.upsert(old)
        service = self.helpers._service(_Runtime(), self.repository)
        service.runtime_request_for_thread = AsyncMock(side_effect=HTTPException(503, 'runtime unavailable'))
        service.project_bindings = lambda _: []
        service.control_actor = object()
        service.bootstrap_bindings = SimpleNamespace(get_by_thread=lambda *_: SimpleNamespace(assignment_id='old-assignment'))
        service.binding_service = SimpleNamespace(workers=SimpleNamespace(store=SimpleNamespace(assignment=lambda _: None)))
        result = await service.archive(old.id)
        self.assertTrue(result['archived'])
        self.assertFalse(result['providerArchived'])
        self.assertTrue(result['archivePending'])
        self.assertTrue(self.repository.get(old.id).archived)

    async def test_local_archive_rejects_current_binding_or_viable_worker(self):
        old = _thread(1, name='Orchestrator')
        self.repository.upsert(old)
        service = self.helpers._service(_Runtime(), self.repository)
        service.runtime_request_for_thread = AsyncMock(side_effect=HTTPException(503, 'runtime unavailable'))
        service.control_actor = object()
        service.bootstrap_bindings = SimpleNamespace(get_by_thread=lambda *_: SimpleNamespace(assignment_id='old-assignment'))
        assignment = SimpleNamespace(status='running', lease=None)
        service.binding_service = SimpleNamespace(workers=SimpleNamespace(store=SimpleNamespace(assignment=lambda _: assignment)))
        service.project_bindings = lambda _: []
        with self.assertRaises(HTTPException):
            await service.archive(old.id)
        self.assertFalse(self.repository.get(old.id).archived)
        assignment.status = 'failed'
        service.project_bindings = lambda _: [SimpleNamespace(thread_id=old.id)]
        with self.assertRaises(HTTPException):
            await service.archive(old.id)
        self.assertFalse(self.repository.get(old.id).archived)

    async def test_local_archive_rejects_active_turn_or_provider_authority_denial(self):
        old = _thread(1, name='Orchestrator')
        self.repository.upsert(old)
        service = self.helpers._service(_Runtime(), self.repository, active={old.id: SimpleNamespace(updated_at=1)})
        service.project_bindings = lambda _: []
        service.control_actor = object()
        service.binding_service = SimpleNamespace()
        service.bootstrap_bindings = SimpleNamespace()
        for status in (503, 403):
            service.runtime_request_for_thread = AsyncMock(side_effect=HTTPException(status, 'unavailable'))
            with self.assertRaises(HTTPException):
                await service.archive(old.id)
            self.assertFalse(self.repository.get(old.id).archived)

    async def test_replacement_is_indexed_in_its_project(self):
        binding = BotBinding(id='binding', provider='slack', external_conversation_id='channel', thread_id='old',
            project_id='home', thread_name='Orchestrator', created_at=1, updated_at=2)
        host = SimpleNamespace(_binding_prefix=lambda b: b.thread_name,
            _retarget_logical_bot_bindings=lambda b, tid: b.model_copy(update={'thread_id': tid}),
            _retarget_bot_thread_state=Mock(), _archive_replaced_bot_thread=AsyncMock(return_value=True),
            _logical_binding_name=lambda _: 'orchestrator', hub=SimpleNamespace(publish=AsyncMock()))
        recovery = recovery_service(host, thread_index=self.repository, thread_creator=AsyncMock(return_value={'thread': {'id': 'new'}}))
        await recovery.replace_stale_bot_thread(binding, 'provider unavailable')
        self.assertEqual(self.repository.get('new').project_id, 'home')
