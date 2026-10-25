"""Canonical non-alias routing must retain ordinary autonomy dispatch."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_contract_seed import execution_role_catalog_seed_payload
from codex_web.identity import TenantScope
from codex_web.models import Project, WorkItemState
from codex_web.owner_routing import OwnerRoutingError
from codex_web.services.autonomy import AutonomyService
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_role_definitions import install_execution_role_definitions
from codex_web.services.owner_routing import OwnerRoutingService
from codex_web.services.projects import ProjectService
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.workspaces import WorkspaceMapper


class AutonomyOptionalOwnerPinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        store = SQLiteStateStore(root / 'state.sqlite3')
        catalog = patch('codex_web.execution_contracts._catalog_provider', None)
        catalog.start()
        self.addCleanup(catalog.stop)
        repository = ProjectRepository(root / 'projects.json', store=store,
            workspace_mapper=WorkspaceMapper(root=root))
        repository.save([Project(id='project', name='Project', path=str(root),
            organization_id='org', workspace_id='workspace')])
        projects = ProjectService(repository)
        registry = DefinitionRegistryService(DefinitionRegistryStore(store))
        roles = install_execution_role_definitions(registry, projects=projects)
        payload = execution_role_catalog_seed_payload()
        payload['owner_to_execution_role']['operator'] = 'orchestrator'
        draft = registry.create_draft(DefinitionDraftCreate(definition_id='execution-roles.default',
            kind='execution-role-catalog', definition_schema_version='1.0', payload=payload,
            scope_type='project', scope_id='project', actor='fixture'))
        registry.approve_publication(draft.record_id, actor='reviewer', reference='PRIVATE-TEST',
            reason='canonical optional owner pin regression')
        registry.publish(draft.record_id, DefinitionPublishRequest(actor='fixture'))
        self.state = WorkItemState(ref='group/repo#1', organization_id='org', workspace_id='workspace',
            project_id='project', current_owner='orchestrator', current_stage='implementation_active',
            created_at=1, updated_at=1, last_meaningful_update_at=1)
        scope = TenantScope(organization_id='org', workspace_id='workspace')
        self.routing = OwnerRoutingService(
            routing=lambda state: roles.owner_routing(project_id=state.project_id, scope=scope).for_work_item(state),
            get_state=lambda _: self.state, projects=projects, load_bindings=lambda: [],
            binding_prefix=lambda _: 'orchestrator', bootstraps=None, assignments=None,
            profiles=None, control_actor=None, actor_resolver=None, replacement_resolver=None)
        self.binding = NS(id='binding', thread_id='thread', project_id='project', is_master=True)
        self.runtime = NS(owner_routing=self.routing,
            dispatch_event=AsyncMock(return_value={'ok': True}),
            thread_is_active=lambda _: False, thread_queue_depth=lambda _: 0)
        self.autonomy = AutonomyService(runtime=self.runtime)

    async def dispatch(self, **kwargs):
        return await self.autonomy._bounded_reasoning_dispatch(self.binding, 'current work',
            'owner-work-watchdog', work_item=self.state, cycle_key='current-work',
            payload={'project_id': 'project'}, **kwargs)

    async def test_canonical_nonalias_none_uses_ordinary_dispatch(self):
        self.assertIsNone(await self.routing.prepare_compatible_pin(self.state, self.binding,
            'orchestrator', writable_repository_ids=[]))
        result = await self.dispatch()
        self.assertTrue(result['ok'])
        self.runtime.dispatch_event.assert_awaited_once_with(self.binding, 'current work',
            'owner-work-watchdog', work_item_ref=self.state.ref, repository_resource_id=None,
            writable_repository_resource_ids=(), require_idle=True)

    async def test_no_pin_retains_existing_terminal_recovery_boundary(self):
        held = object()
        queue = [held]
        recovery = NS(resolve_binding=Mock(return_value=self.binding),
            terminal=Mock(return_value=NS(status='lost')), recover=AsyncMock(return_value={'blocked': True}))
        self.runtime.owner_lane_recovery = recovery
        self.runtime.thread_queue_depth = lambda _: len(queue)
        result = await self.dispatch(recovery_reason='terminal-runner')
        self.assertEqual(result, {'blocked': True})
        recovery.recover.assert_awaited_once_with(self.binding, self.state, 'current work')
        self.assertEqual(queue, [held])
        self.runtime.dispatch_event.assert_not_awaited()

    async def test_typed_routing_denial_never_falls_through(self):
        self.routing.prepare_compatible_pin = AsyncMock(side_effect=OwnerRoutingError('owner_recipient_scope_unavailable'))
        self.routing.record_failure = Mock()
        self.runtime.owner_lane_recovery = NS(resolve_binding=Mock())
        result = await self.dispatch(recovery_reason='terminal-runner')
        self.assertEqual(result['code'], 'owner_recipient_scope_unavailable')
        self.routing.record_failure.assert_called_once()
        self.runtime.owner_lane_recovery.resolve_binding.assert_not_called()
        self.runtime.dispatch_event.assert_not_awaited()

    async def test_selected_pin_scope_and_busy_gate_preserved(self):
        pin = NS(recipient=NS(thread_id='recipient'))
        self.routing.prepare_compatible_pin = AsyncMock(return_value=pin)
        self.runtime.thread_queue_depth = lambda _: 1
        result = await self.dispatch()
        self.assertEqual(result['reason'], 'owner_lane_busy')
        self.runtime.dispatch_event.assert_not_awaited()
        self.runtime.thread_queue_depth = lambda _: 0
        result = await self.dispatch()
        self.assertEqual(result['threadId'], 'recipient')
        self.assertIs(self.runtime.dispatch_event.await_args.kwargs['owner_routing_pin'], pin)
        self.assertIs(self.runtime.dispatch_event.await_args.args[0], self.binding)
