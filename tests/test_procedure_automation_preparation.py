"""#1982: real bootstrap/custody/interpreter; private native/routing read edges."""
import asyncio
import copy
import hashlib
import json
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace as NS

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.automation_definitions import AutomationDefinition
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest, DefinitionReference, DefinitionScope
from codex_web.execution_workers import AssignmentClaimRequest, AssignmentStartRequest, ExecutionRuntimeBinding
from codex_web.execution_workspaces import ExecutionWorkspaceRelease
from codex_web.models import WorkItemHandoff, WorkItemState
from codex_web.procedure_control_plane import CanonicalOperationRegistry
from codex_web.services.automation_definitions import install_automation_definitions, AutomationEventTriggerService
from codex_web.services.automation_runs import AutomationRunService, AutomationTriggerAdmissionBridge
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.keyed_background_tasks import KeyedTaskCoordinator
from codex_web.services.procedure_automation import ProcedureAutomationService, digest
from codex_web.services.procedure_execution import ProcedureExecutionError
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.skills import SkillCreate, SkillPublish
from codex_web.storage.automation_runs import AutomationRunStore
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from tests import test_native_procedure_credential_custody as custody_fixtures
from tests import test_procedure_automation as bridge_fixtures
from tests import test_procedure_control_plane as operation_fixtures


class FreshSupervisorPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = custody_fixtures.NativeProcedureCredentialCustodyTests()
        await self.c.asyncSetUp()
        self.addAsyncCleanup(self.c.asyncTearDown)
        self.f = f = self.c.f
        # The borrowed native fixture has a synthetic Project DTO. Register
        # that exact existing identity in the canonical private Project owner
        # before publishing any Project-scoped Skill (no scope fallback).
        from pathlib import Path
        from codex_web.services.projects import ProjectService
        from codex_web.storage.projects import ProjectRepository
        from codex_web.workspaces import WorkspaceMapper
        repository = ProjectRepository(Path(f.temp.name) / 'event-projects.json',
            store=f.sqlite, workspace_mapper=WorkspaceMapper(root=Path(f.temp.name)))
        repository.save([f.project])
        self.projects = ProjectService(repository)
        self.assertEqual(self.projects.get(f.project.id, f.actor.tenant).id, f.project.id)
        f.skills.projects = self.projects
        f.discovery.projects = self.projects
        self.engine = self.c.owner
        self.native = self.c.admission
        # Publish a genuinely read-only execution profile; native credentials
        # still originate exclusively from the normal bootstrap builder.
        active = f.profiles.record(organization_id=f.actor.organization_id,
            workspace_id=f.actor.workspace_id, project_id=f.project.id)
        payload = copy.deepcopy(active.payload)
        profile = copy.deepcopy(next(p for p in payload['profiles'] if p['id'] == 'repository-write'))
        profile.update(id='event-supervisor-readonly', repository_access='read-only',
            control_plane_operations=['procedure.read', 'procedure.start', 'procedure.advance'])
        payload['profiles'].append(profile)
        draft = f.definition_registry.create_draft(DefinitionDraftCreate(definition_id=active.definition_id,
            kind=active.kind, definition_schema_version=active.definition_schema_version, payload=payload,
            actor=f.actor.identity_id, reason='Private explicit readonly supervisor'))
        f.definition_registry.approve_publication(draft.record_id, actor='test-approver',
            reference='TEST', reason='Controlled private profile fixture')
        f.definition_registry.publish(draft.record_id, DefinitionPublishRequest(actor=f.actor.identity_id,
            reason='Private fixture publication', expected_active_revision=active.revision))
        # Use the existing real registered-owner fixture, replacing only target
        # Resource type. It has no provider/native/model execution edge.
        self.operation = operation_fixtures.ControlledOwner()
        registry = CanonicalOperationRegistry()
        _, contract = bridge_fixtures.procedure_content('read')
        registry.register(replace(contract, resource_types=('repository',)), self.operation)
        self.engine.control_plane = registry
        f.skills.procedure_control_plane = registry
        content, _ = bridge_fixtures.procedure_content('read')
        # Borrowed Skill/discovery/security owners share f.now=100; worker,
        # lease and engine deadlines intentionally remain real wall-clock.
        # Keep the procedure's genuine freshness window in that same domain.
        content.update(reviewed_at=f.skills.clock()-1, review_due_at=f.skills.clock()+100,
            expires_at=f.skills.clock()+200)
        self.assertLess(content['reviewed_at'], f.skills.clock())
        self.assertLess(f.skills.clock(), content['review_due_at'])
        content['environments'] = ['development']
        content['resource_selectors'][0]['resource_types'] = ['repository']
        created = f.skills.create(SkillCreate(project_id=f.project.id, skill_id='native-provider-required',
            name='Project-scoped event procedure', instructions='Read through the canonical owner.',
            operational_procedure=content), actor=f.actor)
        published = f.skills.publish(created['skillId'], created['recordId'], SkillPublish(), actor=f.actor,
            project_id=f.project.id)
        self.reference = DefinitionReference.model_validate(published['definitionReference'])
        f.workspaces.release(f.workspace.id, ExecutionWorkspaceRelease(reason='Replace old fixture workspace'), actor=f.actor)
        self.profile = AgentProfileExecutionBinding(profile_id='readonly-supervisor', profile_revision=1,
            profile_record_id='private-profile-record', role_id='supervisor', skill_refs=(),
            execution_profile_id=profile['id'], sandbox_requirement='read-only')
        bound = self.c.builder.prepare_bootstrap(bootstrap_id='event-supervisor-bootstrap',
            execution_id='event-supervisor-execution', project_id=f.project.id, sandbox='read-only',
            approval_policy='on-request', explicit_repository_id=f.repo.id,
            execution_profile_id=profile['id'], agent_profile=self.profile,
            runtime_binding=ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex',
                capability_revision=7, authentication_mode='trusted_local_session'))
        claim = f.workers.claim(self.c.worker.id, AssignmentClaimRequest(lease_seconds=300),
            actor=self.c.worker_actor, assignment_id=bound.assignment_id)
        self.assignment = f.workers.start(self.c.worker.id, bound.assignment_id,
            AssignmentStartRequest(fence=claim.fence, lease_token=claim.lease.lease_token), actor=self.c.worker_actor)
        self.c.assignment = self.assignment
        # Actual interpreter output derives from its input. Install canonical
        # governance BEFORE creating any input so lineage is genuinely governed,
        # matching ProcedureExecutionTests rather than backfilling IDs.
        from codex_web.services.data_governance import DataGovernanceService
        from codex_web.storage.data_governance import DataGovernanceStore
        f.artifacts.governance = DataGovernanceService(DataGovernanceStore(f.sqlite))
        f.artifacts.resources = f.resources
        self.c.input_artifact()  # Install real local Artifact content owner.
        self.bindings = ThreadBootstrapBindingService(ThreadBootstrapBindingStore(f.sqlite))
        self.bindings.bind(bootstrap_id='event-supervisor-bootstrap', thread_id='supervisor-thread',
            assignment_id=self.assignment.id, execution_id=self.assignment.execution_id,
            execution_workspace_id=self.assignment.execution_workspace_id, actor=f.actor)
        self.native.bootstrap_bindings = self.bindings
        self.native.native_session_attestor = self.c.fixture.attestor
        self.runs = AutomationRunService(AutomationRunStore(f.sqlite), install_automation_definitions(f.definition_registry))
        self.bus = CanonicalEventBus(CanonicalEventStore(f.sqlite))
        self.events = CanonicalEventIngestionService(self.bus)
        # The borrowed admission-only engine intentionally had no Attention
        # dependency. Real interpreter failure disposition must retain its
        # actual typed cause rather than failing while reporting that cause.
        from codex_web.services.attention import AttentionService
        from codex_web.storage.attention import AttentionStore
        self.engine.attention = AttentionService(AttentionStore(f.sqlite), self.events, identity=f.identity)
        self.triggers = AutomationEventTriggerService(f.definition_registry, self.bus)
        self.controller = AutonomyController(AutonomyStateStore(f.sqlite))
        self.controller.resume(actor_id=f.actor.identity_id)
        self.coordinator = KeyedTaskCoordinator(max_concurrency=1, per_scope_concurrency=1)
        self.addAsyncCleanup(self.coordinator.stop)
        now = time.time()
        self.work = WorkItemState(ref='fixture!1', organization_id=f.actor.organization_id,
            workspace_id=f.actor.workspace_id, project_id=f.project.id, resource_ids=[f.repo.id],
            current_owner='sender', next_owner='recipient', created_at=now, updated_at=now,
            last_meaningful_update_at=now,
            handoff=WorkItemHandoff(from_agent='sender', to_agent='recipient', requested_at=10.0))
        # Artifact admission and bridge routing must read the SAME canonical
        # private Work host retained by the borrowed ActionIntent fixture.
        # Do not disable the Artifact Work guard or invent an unrelated reader.
        f.artifacts.work_item_host._save_work_item_state(self.work)
        self.assertIs(f.artifacts.work_item_host, f.host)
        self.assertEqual(f.host._work_item_state(self.work.ref), self.work)
        target = self.c.legacy_assignment
        bootstrap = lambda thread, actor: self.bindings.get_by_thread(thread, actor)
        # Canonical routing/Profile/native transport READ observations are the
        # sole controlled edges; all assignment/custody/Artifact/checkpoint writes
        # and the interpreter itself remain genuine canonical owners.
        self.routing = NS(snapshot=lambda _:NS(owner=lambda role:role, coordination_owners=('supervisor',)),
            existing_binding=lambda _, role:NS(thread_id='supervisor-thread' if role=='supervisor' else 'recipient-thread'),
            bootstraps=NS(get_by_thread=bootstrap),
            executor=lambda *a, **k:(NS(profile_id=self.profile.profile_id),f.actor),
            recovery_recipient=lambda *a, **k:(NS(thread_id='recipient-thread'),target))
        async def runner(thread):
            a = self.current() if thread=='supervisor-thread' else target
            return {'runner':{'assignmentId':a.id, 'fence':a.fence, 'live':True,
                'processRunning':True, 'pendingExecutorWork':0}, 'activity':{'state':'idle', 'turnId':None}}
        self.service = ProcedureAutomationService(runs=self.runs, engine=self.engine, identity=f.identity,
            routing=self.routing, threads=NS(runner=runner), continuity=NS(thread_is_active=lambda _:False),
            get_work=f.host._work_item_state, events=self.bus.store, event_triggers=self.triggers,
            controller=self.controller, coordinator=self.coordinator, automation_execution=NS())
        # Attribute assignment is baseline-compatible: OLD must reach the actual
        # missing-admission failure, never a new constructor/method error.
        self.service.native_preparation = getattr(self.native, 'prepare_automation', None)
        payload = bridge_fixtures.automation_payload(self.reference.model_dump(mode='json'))
        payload['trigger']['event_filter']['project_id'] = f.project.id
        draft = self.runs.definitions.create_draft('fresh-supervisor', AutomationDefinition.model_validate(payload),
            actor_id=f.actor.identity_id, scope_type=DefinitionScope.PROJECT, scope_id=f.project.id)
        self.runs.definitions.publish_draft(draft.record_id, actor_id=f.actor.identity_id)
        self.bridge = AutomationTriggerAdmissionBridge(self.runs, self.triggers, self.bus)
        self.bridge.procedure_handler = self.service.handle_match
        self.addCleanup(self.bridge.install())

    def current(self):
        return self.f.workers.store.assignment(self.assignment.id)

    async def activate(self, key='actual-material-event'):
        await self.events.ingest(event_type='work.transition', source='autonomy-watchdog:fixture',
            idempotency_key=key, payload={'project_id':self.f.project.id,'work_item_ref':self.work.ref},
            tenant_id=self.f.actor.organization_id, workspace_id=self.f.actor.workspace_id)
        while self.coordinator._tasks:
            await asyncio.gather(*tuple(self.coordinator._tasks.values()), return_exceptions=True)
        rows = self.runs.store.list(organization_id=self.f.actor.organization_id,
            workspace_id=self.f.actor.workspace_id, automation_id='fresh-supervisor')
        self.assertEqual(len(rows), 1)
        return rows[0]

    async def test_real_fresh_readonly_bootstrap_material_event_seals_and_completes_without_input(self):
        self.assertEqual(self.current().agent_profile.skill_refs, ())
        self.assertEqual(self.current().sandbox, 'read-only')
        self.assertIsNone(self.current().native_credential_custody.admitted_at)
        self.assertIsNone(self.current().procedure_checkpoint)
        run = await self.activate()
        # Diagnose the genuine OLD boundary explicitly, rather than treating an
        # unrelated caught fixture error as the intended regression.
        if run.status != 'succeeded':
            self.assertEqual(run.result_code, 'procedure_credential_initial_admission_required',
                self.current().procedure_checkpoint)
        self.assertIsNotNone(run.procedure_custody, run.result_code)
        artifact = self.engine.evidence.get_artifact(run.procedure_custody.input_snapshot_id, actor=self.f.actor)
        self.assertEqual(artifact.work_item_ref, self.work.ref)
        self.assertEqual(artifact.lifecycle, 'active')
        self.assertTrue(artifact.governance_record_id)
        raw, _ = self.engine._content(artifact.id, self.f.actor, self.current())
        self.assertEqual(digest(json.loads(raw)), run.procedure_custody.input_snapshot_sha256)
        self.assertEqual(run.status, 'succeeded', run.result_code)
        self.assertIsNotNone(self.current().native_credential_custody.admitted_at)
        self.assertIn(self.reference, self.current().native_credential_custody.procedures)
        self.assertEqual(self.current().procedure_checkpoint.id, run.procedure_custody.checkpoint_id)
        self.assertEqual(self.operation.calls, 1)
        self.c.fixture.attestor.assert_called()
        again = await self.activate('duplicate-material-event')
        self.assertEqual(again.id, run.id)
        self.assertEqual(self.operation.calls, 1)

    async def test_pending_missing_owner_refuses_without_checkpoint(self):
        self.service.native_preparation = None
        run = await self.activate()
        self.assertEqual(run.result_code, 'procedure_automation_preparation_owner_unavailable')
        self.assertIsNone(self.current().procedure_checkpoint)
        self.assertIsNone(self.current().native_credential_custody.admitted_at)

    async def test_missing_physical_attestor_never_seals(self):
        self.native.native_session_attestor = None
        run = await self.activate()
        self.assertEqual(run.result_code, 'procedure_automation_preparation_owner_unavailable')
        self.assertIsNone(self.current().procedure_checkpoint)
        self.assertIsNone(self.current().native_credential_custody.admitted_at)

    async def test_pause_during_preparation_prevents_start_but_preserves_seal(self):
        prepare = self.service.native_preparation
        async def paused(**kwargs):
            result = await prepare(**kwargs)
            self.controller.pause(actor_id=self.f.actor.identity_id)
            return result
        self.service.native_preparation = paused
        run = await self.activate()
        self.assertEqual(run.result_code, 'procedure_automation_controls_blocked')
        self.assertIsNotNone(self.current().native_credential_custody.admitted_at)
        self.assertIsNone(self.current().procedure_checkpoint)

    async def test_changed_original_run_during_preparation_prevents_start(self):
        prepare = self.service.native_preparation
        async def changed(**kwargs):
            result = await prepare(**kwargs)
            def update(state):
                state.runs = [row.model_copy(update={'status':'cancelled'}) for row in state.runs]
                return state
            self.runs.store.update(update)
            return result
        self.service.native_preparation = changed
        await self.activate()
        self.assertIsNone(self.current().procedure_checkpoint)

    async def test_input_scope_and_checksum_refuse_before_sealing(self):
        prepare = self.service.native_preparation
        async def changed(**kwargs):
            return await prepare(**{**kwargs,'request_checksum':'0'*64})
        self.service.native_preparation = changed
        run = await self.activate()
        self.assertEqual(run.result_code,'procedure_automation_preparation_input_changed')
        self.assertIsNone(self.current().native_credential_custody.admitted_at)

    async def test_wrong_thread_and_fence_never_seal(self):
        prepare = self.service.native_preparation
        async def changed(**kwargs):
            return await prepare(**{**kwargs,'expected_fence':kwargs['expected_fence']+1})
        self.service.native_preparation = changed
        run = await self.activate()
        self.assertEqual(run.result_code,'procedure_automation_preparation_assignment_changed')
        self.assertIsNone(self.current().native_credential_custody.admitted_at)

    async def test_formatted_artifact_uses_canonical_input_digest(self):
        snapshot = self.service._snapshot
        def formatted(run, selection):
            artifact = snapshot(run, selection)
            raw = json.dumps(selection.values, indent=2, ensure_ascii=True).encode()
            from codex_web.artifact_evidence import ArtifactCreate, sha256_digest
            item = self.engine.evidence.create_artifact(ArtifactCreate(project_id=self.f.project.id,
                execution_id=self.assignment.execution_id, execution_workspace_id=self.assignment.execution_workspace_id,
                resource_ids=self.assignment.resource_ids, artifact_type='report', name='Formatted canonical original',
                source='automation-run:'+run.id, digest=sha256_digest(raw)),actor=self.f.actor)
            self.assertNotEqual(hashlib.sha256(raw).hexdigest(), digest(selection.values))
            return self.engine.evidence.attach_artifact_content(item.id,(raw,),actor=self.f.actor)
        self.service._snapshot = formatted
        run = await self.activate()
        self.assertEqual(run.status,'succeeded',run.result_code)

    async def test_full_current_discovery_set_is_sealed_not_only_automation_target(self):
        from codex_web.skills import SkillDefinition
        record = self.f.skills.definitions.get_record(self.reference.record_id)
        procedure = SkillDefinition.model_validate(record.payload).operational_procedure
        other = self.f.skills.create(SkillCreate(project_id=self.f.project.id,
            skill_id='second-current-read', name='Second applicable canonical read',
            instructions='Same isolated declared scope, separate governed procedure.',
            operational_procedure=procedure.model_copy(update={'conflict_group':'second-read'})), actor=self.f.actor)
        published = self.f.skills.publish(other['skillId'],other['recordId'],SkillPublish(),
            actor=self.f.actor,project_id=self.f.project.id)
        other_ref = DefinitionReference.model_validate(published['definitionReference'])
        run = await self.activate()
        self.assertEqual(run.status,'succeeded',run.result_code)
        self.assertEqual(set(self.current().native_credential_custody.procedures),{self.reference,other_ref})

    async def test_wrong_original_artifact_source_is_not_admitted(self):
        prepare = self.service.native_preparation
        async def changed(**kwargs):
            return await prepare(**{**kwargs,'automation_run_id':'foreign-original'})
        self.service.native_preparation = changed
        run = await self.activate()
        self.assertEqual(run.result_code,'procedure_automation_preparation_input_scope')
        self.assertIsNone(self.current().native_credential_custody.admitted_at)

    async def test_unselected_definition_is_not_a_credential_scope(self):
        prepare = self.service.native_preparation
        async def changed(**kwargs):
            wrong = self.reference.model_copy(update={'checksum':'0'*64})
            return await prepare(**{**kwargs,'definition':wrong})
        self.service.native_preparation = changed
        run = await self.activate()
        self.assertEqual(run.result_code,'procedure_automation_preparation_target_unselected')
        self.assertIsNone(self.current().native_credential_custody.admitted_at)

    async def test_revoked_assignment_lease_during_preparation_cannot_seal(self):
        prepare = self.service.native_preparation
        async def changed(**kwargs):
            self.f.workers.store.update_assignment(self.c.worker.id,self.assignment.id,
                lambda state:state.model_copy(update={'assignments':[state.assignments[0].model_copy(
                    update={'lease':state.assignments[0].lease.model_copy(update={'expires_at':time.time()-1})})]}))
            return await prepare(**kwargs)
        self.service.native_preparation = changed
        run = await self.activate()
        self.assertEqual(run.result_code,'procedure_assignment_fence_expired_or_revoked')
        self.assertIsNone(self.current().native_credential_custody.admitted_at)

    async def test_sealed_empty_scope_is_never_reopened(self):
        self.engine.admit_initial_native_credentials(self.current(),(),self.f.actor,
            lambda current:self.native.native_session_attestor(current,self.engine))
        before = self.current().native_credential_custody
        run = await self.activate()
        self.assertEqual(run.result_code,'procedure_credential_procedure_not_admitted')
        self.assertEqual(self.current().native_credential_custody,before)
        self.assertIsNone(self.current().procedure_checkpoint)

    async def test_changed_current_skill_definition_cannot_seal(self):
        prepare = self.service.native_preparation
        async def changed(**kwargs):
            self.f.definition_registry.quarantine(self.reference.record_id,
                actor=self.f.actor.identity_id, reason='Actual current Skill trust revoked')
            return await prepare(**kwargs)
        self.service.native_preparation = changed
        run = await self.activate()
        self.assertNotEqual(run.status,'succeeded')
        self.assertIsNone(self.current().native_credential_custody.admitted_at)
        self.assertIsNone(self.current().procedure_checkpoint)

    async def test_changed_worker_capability_cannot_seal(self):
        prepare = self.service.native_preparation
        async def changed(**kwargs):
            self.f.workers.store.update_worker(self.c.worker.id,
                lambda state:state.model_copy(update={'workers':[state.workers[0].model_copy(update={'capabilities':('git',)})]}))
            return await prepare(**kwargs)
        self.service.native_preparation = changed
        run = await self.activate()
        self.assertEqual(run.result_code,'procedure_assignment_fence_expired_or_revoked')
        self.assertIsNone(self.current().native_credential_custody.admitted_at)

    async def test_wrong_canonical_thread_binding_cannot_seal(self):
        prepare = self.service.native_preparation
        async def changed(**kwargs):
            return await prepare(**{**kwargs,'thread_id':'recipient-thread'})
        self.service.native_preparation = changed
        run = await self.activate()
        self.assertNotEqual(run.status,'succeeded')
        self.assertIsNone(self.current().native_credential_custody.admitted_at)
        self.assertIsNone(self.current().procedure_checkpoint)
