"""Ordinary recovery through the real creator and canonical repository admission.

Only provider startup, profile resolution/routing and Git provisioning are fakes;
assignment/workspace admission, settings, retained requests and routing use SQLite.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.models import BotBinding, IndexedThread, QueuedTurn
from codex_web.resources import ResourceAlias, ResourceCreate, ResourceType
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.services.thread_execution_settings import ThreadExecutionSettingsService
from codex_web.services.thread_recovery import ThreadRecoveryService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.runtime_state import RuntimeStateRepositories
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from codex_web.storage.thread_index import ThreadIndexRepository
from tests import test_thread_bootstrap_routing as bootstrap
from tests import test_turn_execution_binding as admission


class ReplacementRepositoryScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_replacement_preserves_explicit_scope_across_reopen(self):
        await self.check_replacement('web')

    async def test_bot_replacement_preserves_explicit_scope_across_reopen(self):
        await self.check_replacement('bot')

    async def check_replacement(self, origin):
        fixture = admission.TurnExecutionBindingTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture._publish_secret()
        root = Path(fixture.temp.name)

        def repository(name, *, purpose=None):
            path = root / name
            path.mkdir()
            resource = fixture.resources.create(ResourceCreate(
                resource_type=ResourceType.REPOSITORY, name=name,
                aliases=[ResourceAlias(namespace='filesystem', value=str(path))]),
                actor=fixture.actor)
            fixture.resources.bind_project(project=fixture.project,
                resource_id=resource.id, actor=fixture.actor, purpose=purpose)
            return resource.id

        secondary = repository('secondary')
        reference = repository('reference')
        default = repository('different-default', purpose='execution-default')
        primary = fixture.repository.id
        writable = (primary, secondary)
        readonly = (reference,)
        profile_binding = AgentProfileExecutionBinding(profile_id='owned-coder',
            profile_revision=4, profile_record_id='owned-coder-rev4')
        # No implicit/latest revision: the resolving seam requires the exact pin.
        profiles = bootstrap._AgentProfiles(SimpleNamespace(revision=4,
            sandbox_requirement='workspace-write', execution_profile_id='repository-write'))
        routing = bootstrap._RoutingService(None, agent_profile=profile_binding)
        bindings = ThreadBootstrapBindingService(ThreadBootstrapBindingStore(fixture.sqlite))
        host = bootstrap._Host()
        host.project = fixture.project
        state = RuntimeStateRepositories(fixture.sqlite,
            thread_settings_file=root/'settings.json', active_turns_file=root/'active.json',
            work_item_states_file=root/'work.json')
        settings = ThreadExecutionSettingsService(
            load_settings=state.thread_settings.load, save_settings=state.thread_settings.save,
            get_setting=state.thread_settings.get, put_setting=state.thread_settings.put,
            delete_setting=state.thread_settings.delete,
            bindings=SimpleNamespace(for_thread=lambda _: []), load_bindings=lambda: [],
            save_bindings=lambda _: None, gitlab_routing_enabled_for_project=lambda _: False,
            binding_report_name=lambda _: None, binding_prefix=lambda _: None)
        settings.remember('old', sandbox='workspace-write', approval_policy='on-request',
            repository_resource_id=primary, writable_repository_resource_ids=writable,
            read_only_repository_resource_ids=readonly, execution_profile_id='repository-write')
        original_settings = settings.get('old').model_dump(mode='json')
        index = ThreadIndexRepository(fixture.sqlite, root/'index.json')
        index.upsert(IndexedThread(id='old', name='Original', project_id=fixture.project.id))
        provider = bootstrap._Session()
        creator = bootstrap._thread_service(host, binding_service=fixture.service,
            session_manager=bootstrap._SessionManager(provider), bootstrap_bindings=bindings,
            control_actor=fixture.actor, agent_profiles=profiles, routing_service=routing)
        creator.settings = settings
        # The creator's supported settings seam is exercised by recovery below;
        # recovery remembers its full explicit collection after bootstrap.
        creator_host = SimpleNamespace(
            _binding_prefix=lambda b: b.route_prefix,
            _logical_binding_name=lambda b: b.route_prefix,
            _retarget_logical_bot_bindings=lambda b, thread: b.model_copy(update={'thread_id':thread}),
            _retarget_bot_thread_state=lambda *_: None,
            _archive_replaced_bot_thread=AsyncMock(return_value=True),
            hub=SimpleNamespace(publish=AsyncMock()))
        recovery = ThreadRecoveryService(creator_host, projects=bootstrap._ProjectRuntime(host),
            settings=settings, naming=SimpleNamespace(set_name=AsyncMock()), thread_index=index,
            runtime_request=AsyncMock(), event_sink=lambda _: None,
            truncate_text=lambda text, limit: str(text)[:limit], thread_creator=creator.create)
        queued = QueuedTurn(id='original-request', thread_id='old', project_id=fixture.project.id,
            message='Original immutable request', source='web', created_at=100,
            execution_id='original-execution', agent_profile_id='owned-coder',
            agent_profile_revision=4, agent_profile_actor_id=fixture.actor.identity_id,
            repository_resource_id=primary, writable_repository_resource_ids=writable,
            read_only_repository_resource_ids=readonly, execution_profile_id='repository-write',
            reply_target={'provider':'slack', 'external_conversation_id':'fixture-only',
                'thread_id':'old', 'updated_at':100})
        preflight = ExecutionPreflightService(ExecutionPreflightStore(fixture.sqlite))
        attempt = preflight.retain_queued(queued, organization_id=fixture.actor.organization_id,
            workspace_id=fixture.actor.workspace_id)
        original_snapshot = attempt.queued_turn_snapshot
        if origin == 'web':
            successor = await recovery.replace_stale_web_thread('old', fixture.project, 'archived',
                agent_profile_id='owned-coder', agent_profile_revision=4, actor=fixture.actor)
        else:
            bot = BotBinding(id='owned-binding', provider='slack', external_conversation_id='fixture',
                project_id=fixture.project.id, thread_id='old', route_prefix='Owned',
                sandbox='workspace-write', approval_policy='on-request', created_at=1, updated_at=1)
            recovery.agent_profile_resolver = lambda _: (
                SimpleNamespace(profile_id='owned-coder', revision=4), fixture.actor)
            successor = (await recovery.replace_stale_bot_thread(bot, 'archived')).thread_id
        # Check actual persisted admission, not forwarded kwargs or a fabricated assignment.
        assignment = fixture.workers.list_assignments(fixture.actor)[0]
        workspace = fixture.workspaces.get(assignment.execution_workspace_id, fixture.actor)
        self.assertEqual(assignment.repository_scope.writable_repository_ids, writable)
        self.assertEqual(assignment.repository_scope.read_only_repository_ids, readonly)
        self.assertEqual(workspace.writable_repository_ids, writable)
        self.assertNotIn(default, assignment.resource_ids)
        self.assertEqual(assignment.agent_profile, profile_binding)
        self.assertEqual(profiles.calls[0]['profile_id'], 'owned-coder')
        self.assertEqual(profiles.calls[0]['revision'], 4)
        self.assertIs(profiles.calls[0]['actor'], fixture.actor)
        self.assertIs(routing.calls[0][1], fixture.actor)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(provider.requests[0][0], 'thread/start')
        self.assertEqual(settings.get(successor).writable_repository_resource_ids, writable)
        self.assertEqual(settings.get(successor).read_only_repository_resource_ids, readonly)
        # New store objects must resolve the same edge and original immutable request.
        reopened = SQLiteStateStore(root/'state.sqlite3')
        restarted = ThreadIndexRepository(reopened, root/'index.json')
        self.assertEqual(restarted.replacement_thread_id('old', project_id=fixture.project.id,
            organization_id=fixture.actor.organization_id, workspace_id=fixture.actor.workspace_id), successor)
        self.assertEqual(ExecutionWorkerStore(reopened).assignment(assignment.id), assignment)
        self.assertEqual(ThreadBootstrapBindingService(ThreadBootstrapBindingStore(reopened))
            .get_by_thread(successor, fixture.actor).assignment_id, assignment.id)
        reopened_settings = RuntimeStateRepositories(reopened,
            thread_settings_file=root/'settings.json', active_turns_file=root/'active.json',
            work_item_states_file=root/'work.json').thread_settings
        self.assertEqual(reopened_settings.get(successor), settings.get(successor))
        provenance = reopened.get(index.REPLACEMENTS_NAMESPACE)['edges']['old']['provenance']
        self.assertEqual(provenance['writable_repository_resource_ids'], list(writable))
        self.assertEqual(provenance['read_only_repository_resource_ids'], list(readonly))
        self.assertEqual(provenance['actor_identity_id'], fixture.actor.identity_id)
        self.assertEqual(provenance['agent_profile_id'], 'owned-coder')
        self.assertEqual(provenance['agent_profile_revision'], 4)
        self.assertEqual(ExecutionPreflightStore(reopened).get(attempt.id).queued_turn_snapshot,
            original_snapshot)
        self.assertEqual(state.thread_settings.get('old').model_dump(mode='json'), original_settings)
