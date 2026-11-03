"""Normal replacement uses private canonical admission; native/Git I/O is controlled."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.execution_workers import AssignmentCancelRequest
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.models import BotBinding, ThreadRunSettings
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.services.thread_execution_settings import ThreadExecutionSettingsService
from codex_web.services.thread_recovery import ThreadRecoveryService
from codex_web.storage.runtime_state import RuntimeStateRepositories
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from codex_web.storage.thread_index import ThreadIndexRepository
from tests import test_readonly_repository_bootstrap as readonly
from tests import test_thread_bootstrap_routing as bootstrap
from tests import test_turn_execution_binding as admission


class ReadonlyThreadReplacementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = admission.TurnExecutionBindingTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f._publish_secret()
        self.root = Path(self.f.temp.name)
        self.primary = self.f.repository.id
        self.profile_id = readonly.ReadonlyRepositoryBootstrapTests.custom_profile(SimpleNamespace(f=self.f))
        self.secondary = readonly.ReadonlyRepositoryBootstrapTests.second_repository(SimpleNamespace(f=self.f)).id
        self.host = bootstrap._Host()
        self.host.project = self.f.project
        self.state = RuntimeStateRepositories(self.f.sqlite,
            thread_settings_file=self.root/'settings.json', active_turns_file=self.root/'active.json',
            work_item_states_file=self.root/'work.json')
        self.settings = ThreadExecutionSettingsService(
            load_settings=self.state.thread_settings.load, save_settings=self.state.thread_settings.save,
            get_setting=self.state.thread_settings.get, put_setting=self.state.thread_settings.put,
            delete_setting=self.state.thread_settings.delete,
            bindings=SimpleNamespace(for_thread=lambda _: []), load_bindings=lambda: [],
            save_bindings=lambda _: None, gitlab_routing_enabled_for_project=lambda _: False,
            binding_report_name=lambda _: None, binding_prefix=lambda _: None)
        self.profile_binding = AgentProfileExecutionBinding(profile_id='read-supervisor',
            profile_revision=2, profile_record_id='private-read-supervisor-2')
        self.profiles = bootstrap._AgentProfiles(SimpleNamespace(revision=2,
            sandbox_requirement='read-only', execution_profile_id=self.profile_id))
        self.routing = bootstrap._RoutingService(None, agent_profile=self.profile_binding)
        self.bindings = ThreadBootstrapBindingService(ThreadBootstrapBindingStore(self.f.sqlite))
        self.provider = bootstrap._Session(response={'thread': {'id': 'old-read'}})
        self.creator = bootstrap._thread_service(self.host, binding_service=self.f.service,
            session_manager=bootstrap._SessionManager(self.provider), bootstrap_bindings=self.bindings,
            control_actor=self.f.actor, agent_profiles=self.profiles, routing_service=self.routing)
        self.creator.settings = self.settings
        self.index = ThreadIndexRepository(self.f.sqlite, self.root/'index.json')
        recovery_host = SimpleNamespace(
            _binding_prefix=lambda b: b.route_prefix, _logical_binding_name=lambda b: b.route_prefix,
            _retarget_logical_bot_bindings=lambda b, thread: b.model_copy(update={'thread_id': thread}),
            _retarget_bot_thread_state=lambda *_: None,
            _archive_replaced_bot_thread=AsyncMock(return_value=True),
            hub=SimpleNamespace(publish=AsyncMock()))
        self.recovery = ThreadRecoveryService(recovery_host,
            projects=bootstrap._ProjectRuntime(self.host), settings=self.settings,
            naming=SimpleNamespace(set_name=AsyncMock()), thread_index=self.index,
            runtime_request=AsyncMock(), event_sink=lambda _: None,
            truncate_text=lambda text, limit: str(text)[:limit], thread_creator=self.creator.create)
        self.recovery.agent_profile_resolver = lambda _: (
            SimpleNamespace(profile_id='read-supervisor', revision=2), self.f.actor)
        self.manager = SimpleNamespace(local_worker=SimpleNamespace(
            _pending_assignment=lambda key: self.f.workers.store.assignment(key)), stop=AsyncMock())
        self.turns = TurnExecutionService(SimpleNamespace(_load_active_turns=lambda: {}),
            binding_service=self.f.service, session_manager=self.manager,
            bootstrap_bindings=self.bindings, control_actor=self.f.actor)
        self.recovery.predecessor_retirement = self.turns.retire_replacement_predecessor

    async def old(self, *, legacy=True):
        await self.creator.create(project_id=self.f.project.id, sandbox='read-only',
            approval_policy='on-request', repository_resource_id=self.primary,
            read_only_repository_resource_ids=(self.secondary,), execution_profile_id=self.profile_id,
            agent_profile_id='read-supervisor', agent_profile_revision=2, actor=self.f.actor)
        binding = self.bindings.get_by_thread('old-read', self.f.actor)
        assignment = self.f.workers.store.assignment(binding.assignment_id)
        self.terminal = self.f.workers.cancel_bootstrap(assignment.id,
            AssignmentCancelRequest(expected_fence=assignment.fence, reason='unused private bootstrap'),
            actor=self.f.actor)
        self.workspace = self.f.workspaces.get(assignment.execution_workspace_id, self.f.actor)
        self.marker = Path(self.workspace.path)/'unpublished.txt'
        self.marker.write_text('preserve source bytes')
        if legacy:
            # Actual historical remember output, not an explicit new replacement DTO.
            self.state.thread_settings.put('old-read', ThreadRunSettings(
                sandbox='read-only', approval_policy='on-request', repository_resource_id=self.primary,
                writable_repository_resource_ids=(self.primary,),
                read_only_repository_resource_ids=(self.secondary,), execution_profile_id=self.profile_id))
        self.original = self.settings.get('old-read').model_dump(mode='json')
        self.provider.response = {'thread': {'id': 'successor-read'}}
        return assignment

    async def replace(self, origin='web', **kwargs):
        if origin == 'bot':
            binding = BotBinding(id='private-bot', provider='slack', external_conversation_id='private',
                project_id=self.f.project.id, thread_id='old-read', route_prefix='Supervisor',
                sandbox='read-only', approval_policy='on-request', created_at=1, updated_at=1)
            return (await self.recovery.replace_stale_bot_thread(binding, 'archived')).thread_id
        return await self.recovery.replace_stale_web_thread('old-read', self.f.project, 'archived',
            agent_profile_id='read-supervisor', agent_profile_revision=kwargs.get('revision', 2),
            actor=self.f.actor)

    async def check_success(self, origin):
        await self.old()
        error = None
        try:
            result = await self.replace(origin)
        except Exception as exc:
            error = exc
        self.assertIsNone(error, str(error))  # OLD fails at actual READ + explicit WRITE admission.
        self.assertEqual(result, 'successor-read')
        bound = self.bindings.get_by_thread(result, self.f.actor)
        assignment = self.f.workers.store.assignment(bound.assignment_id)
        successor = self.f.workspaces.get(assignment.execution_workspace_id, self.f.actor)
        self.assertEqual(assignment.repository_scope.write_mode, 'read_only')
        self.assertIsNone(assignment.repository_target.mutable_repository_id)
        self.assertEqual(assignment.repository_target.read_only_primary_repository_id, self.primary)
        self.assertEqual(assignment.repository_scope.writable_repository_ids, ())
        self.assertEqual(set(assignment.repository_scope.read_only_repository_ids), {self.primary,self.secondary})
        self.assertTrue(all(m.access_mode == 'read' for m in successor.repository_members))
        self.assertEqual(self.settings.get(result).writable_repository_resource_ids, ())
        self.assertEqual(set(self.settings.get(result).read_only_repository_resource_ids), {self.primary,self.secondary})
        self.assertEqual(self.settings.get('old-read').model_dump(mode='json'), self.original)
        self.assertEqual(self.f.workers.store.assignment(self.terminal.id), self.terminal)
        self.assertEqual(self.marker.read_text(), 'preserve source bytes')
        self.manager.stop.assert_awaited_once_with(self.terminal.id, expected_terminal=self.terminal)
        self.assertEqual([m for m,_ in self.provider.requests], ['thread/start', 'thread/start'])
        evidence = self.f.sqlite.get(self.index.REPLACEMENTS_NAMESPACE)['edges']['old-read']['provenance']
        self.assertEqual(evidence['writable_repository_resource_ids'], [])
        self.assertEqual(set(evidence['read_only_repository_resource_ids']), {self.primary,self.secondary})

    async def test_web_legacy_read_replacement_through_actual_bootstrap(self):
        await self.check_success('web')

    async def test_bot_legacy_read_replacement_through_actual_bootstrap(self):
        await self.check_success('bot')

    async def test_mutable_profile_with_effective_read_lease_preserves_actual_read_scope(self):
        self.profile_id = 'repository-write'
        self.profiles.profile.execution_profile_id = self.profile_id
        await self.check_success('web')

    async def test_fresh_creator_remembers_admitted_read_scope_without_primary_promotion(self):
        await self.old(legacy=False)
        self.assertEqual(self.settings.get('old-read').writable_repository_resource_ids, ())
        self.assertEqual(set(self.settings.get('old-read').read_only_repository_resource_ids),
                         {self.primary,self.secondary})
        await self.replace()
        self.assertEqual(self.settings.get('successor-read').writable_repository_resource_ids, ())

    async def test_unchanged_admitted_read_settings_survive_preference_and_turn_remember(self):
        await self.old(legacy=False)
        original = self.settings.get('old-read')
        for selection in ({}, {'repository_resource_id': original.repository_resource_id,
                'writable_repository_resource_ids': original.writable_repository_resource_ids,
                'read_only_repository_resource_ids': original.read_only_repository_resource_ids,
                'execution_profile_id': original.execution_profile_id}):
            self.settings.remember('old-read', model='changed-preference', **selection)
            self.assertEqual(self.settings.get('old-read').writable_repository_resource_ids, ())
            self.assertEqual(self.settings.get('old-read').read_only_repository_resource_ids,
                             original.read_only_repository_resource_ids)
        self.settings.remember('old-read', writable_repository_resource_ids=(self.primary,))
        self.assertEqual(self.settings.get('old-read').writable_repository_resource_ids, (self.primary,))

    async def test_extra_writable_or_missing_secondary_is_not_reinterpreted(self):
        await self.old()
        for change in ({'writable_repository_resource_ids': (self.primary,self.secondary)},
                       {'read_only_repository_resource_ids': ()}):
            with self.subTest(change=change):
                row = ThreadRunSettings.model_validate(self.original).model_copy(update=change)
                self.state.thread_settings.put('old-read', row)
                with self.assertRaises(HTTPException) as caught:
                    await self.replace()
                self.assertEqual(caught.exception.detail['code'], 'readonly_replacement_scope_unverified')
                self.assertEqual(len(self.provider.requests), 1)
        self.assertIsNone(self.recovery.replacement_thread_id('old-read'))

    async def test_changed_selected_profile_is_refused_before_creator(self):
        await self.old()
        with self.assertRaises(HTTPException) as caught:
            await self.replace(revision=3)
        self.assertEqual(caught.exception.detail['code'], 'readonly_replacement_scope_unverified')
        self.assertEqual(len(self.provider.requests), 1)

    async def test_actual_workspace_or_lease_write_mode_refuses_legacy_normalization(self):
        await self.old()
        original = self.f.workspaces.store.load()
        for kind in ('member', 'lease'):
            def change(state):
                if kind == 'member':
                    # A coherent ordinary WRITE workspace is valid canonical
                    # metadata, but cannot prove this assignment's READ scope.
                    state.workspaces = [w.model_copy(update={
                        'read_only_primary_repository_id': None,
                        'writable_repository_ids': tuple(m.resource_id for m in w.repository_members),
                        'repository_members': tuple(m.model_copy(update={'access_mode':'write'})
                                                    for m in w.repository_members)})
                        if w.id == self.workspace.id else w for w in state.workspaces]
                else:
                    state.leases = [l.model_copy(update={'mode':'write',
                        'resource_modes': {resource_id: 'write' for resource_id in l.resource_ids}})
                        if l.id == self.workspace.lease_id else l for l in state.leases]
                return state
            self.f.workspaces.store.update(change)
            with self.assertRaises(HTTPException) as caught:
                await self.replace()
            self.assertEqual(caught.exception.detail['code'], 'readonly_replacement_scope_unverified')
            self.f.workspaces.store.update(lambda _: deepcopy(original))
        self.assertEqual(len(self.provider.requests), 1)

    async def test_current_definition_drift_refuses_historical_scope(self):
        await self.old()
        profiles = self.f.execution_profiles
        active = profiles.record(organization_id=self.f.actor.organization_id,
            workspace_id=self.f.actor.workspace_id, project_id=self.f.project.id)
        payload = deepcopy(active.payload)
        payload['default_profile_id'] = self.profile_id
        draft = profiles.registry.create_draft(DefinitionDraftCreate(
            definition_id=active.definition_id, kind=active.kind,
            definition_schema_version=active.definition_schema_version,
            payload=payload, actor=self.f.actor.identity_id, reason='Private current pin drift'))
        profiles.registry.approve_publication(draft.record_id, actor='private-reviewer',
            reference='PRIVATE', reason='Controlled fixture')
        profiles.registry.publish(draft.record_id, DefinitionPublishRequest(actor=self.f.actor.identity_id,
            expected_active_revision=active.revision, reason='Private current pin drift'))
        with self.assertRaises(HTTPException) as caught:
            await self.replace()
        self.assertEqual(caught.exception.detail['code'], 'readonly_replacement_scope_unverified')
        self.assertEqual(len(self.provider.requests), 1)

    async def test_foreign_source_assignment_scope_refuses_before_normalization(self):
        await self.old()
        def change(state):
            state.assignments = [a.model_copy(update={'project_id':'foreign'}) if a.id==self.terminal.id else a
                                 for a in state.assignments]
            return state
        self.f.workers.store.update(change)
        with self.assertRaises(HTTPException) as caught:
            await self.replace()
        self.assertEqual(caught.exception.detail['code'], 'thread_replacement_predecessor_unsettled')
        self.assertEqual(len(self.provider.requests), 1)

    async def test_no_owned_read_proof_keeps_explicit_write_refusal(self):
        await self.old()
        self.recovery.predecessor_retirement = None
        with self.assertRaises(HTTPException) as caught:
            await self.replace()
        self.assertEqual(caught.exception.detail['code'], 'execution_preflight_blocked')
        self.assertIn('READ execution cannot request writable repositories', str(caught.exception.detail))
        self.assertEqual(len(self.provider.requests), 1)

    async def test_changed_repository_or_profile_does_not_inherit_read_preservation(self):
        await self.old(legacy=False)
        original = self.settings.get('old-read').model_copy(deep=True)
        for change in ({'repository_resource_id': self.secondary},
                       {'execution_profile_id': 'different-profile'}):
            with self.subTest(change=change):
                self.state.thread_settings.put('old-read', original.model_copy(deep=True))
                row = self.settings.remember('old-read', **change)
                self.assertEqual(row.writable_repository_resource_ids,
                                 (change.get('repository_resource_id', self.primary),))

    def test_orchestration_settings_remain_without_repository_authority(self):
        row = self.settings.remember('orchestration', sandbox='read-only',
            execution_profile_id='orchestration-only')
        self.assertIsNone(row.repository_resource_id)
        self.assertEqual(row.writable_repository_resource_ids, ())
        self.assertEqual(row.read_only_repository_resource_ids, ())

    def test_sandbox_only_settings_retain_ordinary_mutable_semantics(self):
        row = self.settings.remember('ordinary', sandbox='read-only',
            repository_resource_id=self.primary, read_only_repository_resource_ids=(self.secondary,))
        self.assertEqual(row.writable_repository_resource_ids, (self.primary,))
        self.assertEqual(row.read_only_repository_resource_ids, (self.secondary,))
