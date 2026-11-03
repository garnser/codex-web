"""#2001 real readonly bootstrap, canonical checkpoint, cancellation and Run CAS.

Only native/Profile/routing READ edges inherited from the qualified preparation
fixture are controlled. No provider, native input, model or live API is invoked.
"""
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from codex_web.api.automation_outcomes import build_automation_outcomes_router
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.execution_workers import AssignmentCancelRequest
from codex_web.models import ExecutionPreflightAttempt
from codex_web.services.automation_outcomes import AutomationOutcomeReconciliationService
from codex_web.services.identity import AuthenticationError
from codex_web.services.owner_routing import OwnerRoutingService
from codex_web.services.procedure_automation import ProcedureAutomationError
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from tests import test_procedure_automation_preparation as fixtures


class RetiredProcedureRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.b = fixtures.FreshSupervisorPreparationTests()
        self.b.addCleanup = self.addCleanup
        self.b.addAsyncCleanup = self.addAsyncCleanup
        await self.b.asyncSetUp()
        self.b.operation.disposition = 'waiting'
        self.run = await self.b.activate()
        self.f = self.b.f
        self.assertEqual(self.run.status, 'running')
        self.assertEqual(self.b.current().procedure_checkpoint.status, 'waiting')
        self.preflight = ExecutionPreflightStore(self.f.sqlite)
        self.preflight.prepare_work_item_index()
        routing = SimpleNamespace(preflight=self.preflight)
        self.b.routing.require_resolved_originals = lambda work: OwnerRoutingService.require_resolved_originals(routing, work)
        self.outcomes = AutomationOutcomeReconciliationService(self.b.runs, self.f.workers)
        self.outcomes.procedure_reconciler = self.b.service.reconcile
        self.before = self.b.current().model_copy(deep=True)
        self.retire()
        self.original = self.b.current().model_copy(deep=True)
        self.operation_calls = self.b.operation.calls

    def retire(self):
        current = self.b.current()
        self.f.workers.cancel_bootstrap(current.id, AssignmentCancelRequest(
            expected_fence=current.fence, reason='Private controlled normal upgrade retirement'), actor=self.f.actor)
        self.assertEqual(self.b.current().status, 'cancelled')
        self.assertEqual(self.b.current().fence, current.fence + 1)
        self.assertIsNone(self.b.current().lease)

    def dispose(self, actor=None):
        # OLD source reaches its actual original-custody refusal. Never call a
        # missing method or unsupported constructor/keyword as the reproduction.
        options = {'cancel_retired_procedure': True} if 'cancel_retired_procedure' in inspect.signature(self.outcomes.reconcile).parameters else {}
        return self.outcomes.reconcile(self.run.id, actor=actor or self.f.actor, **options)

    def assert_originals(self):
        self.assertEqual(self.b.current(), self.original)
        self.assertEqual(self.b.current().procedure_checkpoint, self.before.procedure_checkpoint)
        self.assertEqual(self.b.operation.calls, self.operation_calls)
        current = self.b.runs.store.get(self.run.id, organization_id=self.run.organization_id,
            workspace_id=self.run.workspace_id)
        for field in ('procedure_custody', 'trigger', 'dedupe_key', 'execution_ids', 'evidence_ids',
                      'definition_ref', 'work_item_ref', 'attempt', 'started_at'):
            self.assertEqual(getattr(current, field), getattr(self.run, field), field)

    def change_assignment(self, change):
        current = self.b.current()
        def update(state):
            change(state.assignments[0])
            return state
        self.f.workers.store.update_assignment(current.assigned_worker_id, current.id, update)

    async def test_cancelled_original_can_end_scheduling_without_completing_checkpoint(self):
        result = None
        refusal = None
        try:
            result = self.dispose()
        except ProcedureAutomationError as exc:
            refusal = str(exc)
        self.assertIsNotNone(result, f'Original retired custody stranded Run: {refusal}')
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(result.result_code, 'procedure_retired_custody_cancelled')
        self.assertIn('not completed', result.result_reason)
        self.assert_originals()
        # Repeating the request cannot replay an operation or produce a retry.
        self.assertEqual(self.dispose(), result)
        self.assertEqual(self.b.operation.calls, self.operation_calls)

    def prune_published_original_event(self):
        # Exercise normal bounded retention, not raw deletion or a missing-event
        # mock. The Run was genuinely admitted before its published event ages out.
        store = self.b.service.events
        original = store.event(self.run.trigger.event_id)
        self.assertIsNotNone(original)
        if store.outbox(original.event_id) is not None:
            store.mark_outbox_published(original.event_id, backend_id='private-fixture',
                delivery_id='private-original-delivery', now=original.occurred_at + 1)
        store.max_events = 1
        later = CanonicalEventEnvelope(event_id='later-retained-event',
            event_type='test.retention', source='private-fixture',
            occurred_at=original.occurred_at + 2, payload={},
            tenant_id=original.tenant_id, workspace_id=original.workspace_id)
        store.record_if_new(later, idempotency_key='later-retained-event')
        self.assertIsNone(store.event(original.event_id))
        self.assertEqual(store.event(later.event_id), later)

    async def test_normally_pruned_event_does_not_strand_settled_retired_run(self):
        self.prune_published_original_event()
        result = None
        refusal = None
        try:
            result = self.dispose()
        except ProcedureAutomationError as exc:
            refusal = str(exc)
        self.assertIsNotNone(result, f'Retired scheduling depends on pruned event: {refusal}')
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(result.result_code, 'procedure_retired_custody_cancelled')
        self.assert_originals()
        self.assertEqual(self.dispose(), result)

    async def test_active_execution_still_refuses_normally_pruned_original_event(self):
        self.prune_published_original_event()
        self.change_assignment(lambda a: a.__dict__.update(self.before.model_copy(deep=True).__dict__))
        live = self.b.current().model_copy(deep=True)
        with self.assertRaisesRegex(ProcedureAutomationError, 'procedure_automation_event_changed'):
            self.b.service._definition(self.run)
        self.assertEqual(self.b.current(), live)
        self.assertEqual(self.b.operation.calls, self.operation_calls)
        self.assertEqual(self.b.service._get(self.run), self.run)

    async def test_pruned_event_does_not_bypass_current_definition_validation(self):
        self.prune_published_original_event()
        resolve = self.b.runs.definitions.resolve
        def changed(*args, **kwargs):
            definition, reference = resolve(*args, **kwargs)
            return definition, reference.model_copy(update={'revision':reference.revision + 1})
        with patch.object(self.b.runs.definitions, 'resolve', side_effect=changed):
            with self.assertRaisesRegex(ProcedureAutomationError, 'procedure_automation_definition_changed'):
                self.dispose()
        self.assert_originals()

    async def test_existing_reconcile_without_explicit_disposition_still_refuses_changed_fence(self):
        with self.assertRaisesRegex(ProcedureAutomationError, 'original_custody_changed'):
            self.outcomes.reconcile(self.run.id, actor=self.f.actor)
        self.assert_originals()

    async def test_live_custody_cannot_be_cancelled_by_scheduling_disposition(self):
        # Exact original live snapshot, arranged only in the private fixture.
        self.change_assignment(lambda a: a.__dict__.update(self.before.model_copy(deep=True).__dict__))
        with self.assertRaisesRegex(ValueError, 'cancelled_original_required'):
            self.dispose()
        self.assertEqual(self.b.runs.store.get(self.run.id, organization_id=self.run.organization_id,
            workspace_id=self.run.workspace_id).status, 'running')

    async def test_arbitrary_fence_or_changed_worker_is_not_original_retirement(self):
        for field, value in (('fence', self.original.fence + 1), ('assigned_worker_id', 'foreign-worker')):
            with self.subTest(field=field):
                self.change_assignment(lambda a: setattr(a, field, value))
                with self.assertRaisesRegex(ValueError, 'cancelled_original_required'):
                    self.dispose()
                self.change_assignment(lambda a: setattr(a, field, getattr(self.original, field)))
        self.assert_originals()

    async def test_unknown_or_running_step_is_not_erased(self):
        for status in ('running', 'reconciliation_required'):
            with self.subTest(status=status):
                self.change_assignment(lambda a: setattr(next(iter(a.procedure_checkpoint.steps.values())), 'status', status))
                with self.assertRaisesRegex(ValueError, 'effect_unresolved'):
                    self.dispose()
        self.change_assignment(lambda a: setattr(a, 'procedure_checkpoint', self.original.procedure_checkpoint.model_copy(deep=True)))
        self.assert_originals()

    async def test_checkpoint_unknown_is_not_erased(self):
        self.change_assignment(lambda a: setattr(a.procedure_checkpoint, 'status', 'reconciliation_required'))
        with self.assertRaisesRegex(ValueError, 'checkpoint_unsettled_or_changed'):
            self.dispose()

    async def test_started_model_invocation_remains_with_original_owner(self):
        self.change_assignment(lambda a: setattr(next(iter(a.procedure_checkpoint.steps.values())), 'invocation_ids', ('original-model-call',)))
        with self.assertRaisesRegex(ValueError, 'effect_unresolved'):
            self.dispose()

    async def test_exact_native_unknown_work_index_blocks_disposition(self):
        self.preflight.put(ExecutionPreflightAttempt(id='unknown-original', correlation_id='unknown-original',
            execution_id='original-unresolved-execution', organization_id=self.run.organization_id,
            workspace_id=self.run.workspace_id, project_id=self.run.project_id,
            thread_id='recipient-thread', message='Private retained input',
            queued_turn_snapshot={'work_item_ref':self.run.work_item_ref}, status='reconciliation_required',
            created_at=1, updated_at=1))
        with self.assertRaisesRegex(ValueError, 'native_outcome_unresolved'):
            self.dispose()
        self.assert_originals()

    async def test_missing_complete_native_inventory_is_not_absence_of_effects(self):
        self.f.sqlite.record_apply(self.preflight.NAMESPACE,
            upserts={'metadata/work-item-index-1.0': {'schema_version':'1.0', 'complete':False}})
        with self.assertRaisesRegex(ValueError, 'retained_index_unavailable'):
            self.dispose()

    async def test_invalidated_canonical_waiting_evidence_is_not_settled(self):
        current = self.b.current()
        step = next(iter(current.procedure_checkpoint.steps.values()))
        evidence_id = step.evidence['canonical_disposition']
        get = self.b.engine.evidence.get_evidence
        def invalidated(key, **options):
            proof = get(key, **options)
            return proof.model_copy(update={'result':'fail'}) if key == evidence_id else proof
        with patch.object(self.b.engine.evidence, 'get_evidence', side_effect=invalidated):
            with self.assertRaisesRegex(ValueError, 'diagnostic_changed'):
                self.dispose()
        self.assert_originals()

    async def test_foreign_provider_or_workspace_cannot_relabel_waiting_diagnostic(self):
        get = self.b.engine.evidence.get_evidence
        for update in ({'provider':'foreign-owner'}, {'execution_workspace_id':'foreign-workspace'}):
            with self.subTest(update=update):
                def replaced(key, **options):
                    return get(key, **options).model_copy(update=update)
                with patch.object(self.b.engine.evidence, 'get_evidence', side_effect=replaced):
                    with self.assertRaisesRegex(ValueError, 'diagnostic_changed'):
                        self.dispose()
        self.assert_originals()

    async def test_credential_free_mfa_claim_is_not_authenticated_origin(self):
        actor = self.f.actor.model_copy(update={'assurance':'mfa'})
        with self.assertRaises(AuthenticationError):
            self.dispose(actor)
        self.assert_originals()

    async def test_foreign_tenant_cannot_select_run(self):
        actor = self.f.actor.model_copy(update={'organization_id':'other'})
        with self.assertRaises(KeyError):
            self.dispose(actor)
        self.assert_originals()

    async def test_revoked_procedure_refuses_disposition(self):
        with patch.object(self.b.engine.skills, 'validate_required_procedure', side_effect=ValueError('procedure revoked')):
            with self.assertRaisesRegex(ValueError, 'procedure revoked'):
                self.dispose()
        self.assert_originals()

    async def test_assignment_changed_after_validation_refuses_writer_cas(self):
        original = self.b.runs.store.cancel_retired_procedure
        def racing(run, assignment, **options):
            self.change_assignment(lambda a: setattr(a.procedure_checkpoint, 'revision', a.procedure_checkpoint.revision + 1))
            return original(run, assignment, **options)
        with patch.object(self.b.runs.store, 'cancel_retired_procedure', side_effect=racing):
            with self.assertRaisesRegex(ValueError, 'original_changed'):
                self.dispose()
        self.assertEqual(self.b.runs.store.get(self.run.id, organization_id=self.run.organization_id,
            workspace_id=self.run.workspace_id).status, 'running')

    async def test_existing_operator_api_accepts_explicit_disposition_and_rejects_combined_modes(self):
        app = FastAPI()
        @app.middleware('http')
        async def auth(request, call_next):
            request.state.identity_actor = self.f.actor
            return await call_next(request)
        app.include_router(build_automation_outcomes_router(self.outcomes))
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://private-fixture') as client:
            path = f'/api/automation-runs/{self.run.id}/reconcile'
            refused = await client.post(path, json={'cancel_retired_procedure':True, 'cancel_unknown_home':True})
            self.assertEqual(refused.status_code, 409)
            response = await client.post(path, json={'cancel_retired_procedure':True})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['run']['status'], 'cancelled')
        self.assert_originals()


class HistoricalDispatchDispositionTests(unittest.IsolatedAsyncioTestCase):
    """Actual registered mutation/engine waiting diagnostic and retained START edge.

    Native response bytes are a controlled typed fixture, not live qualification.
    All Work history, admission/index, bootstrap, checkpoint and Run writes use
    their real existing canonical private owners.
    """
    async def asyncSetUp(self):
        from dataclasses import replace
        from unittest.mock import AsyncMock
        from codex_web.agent_profiles import AgentProfileExecutionBinding
        from codex_web.execution_workers import AssignmentClaimRequest, AssignmentStartRequest, ExecutionRuntimeBinding
        from codex_web.procedure_control_plane import CanonicalOperationContract, CanonicalOperationRegistry
        from codex_web.services.procedure_custody_recovery import CustodyInput, CustodyOperation, output_contract
        from codex_web.services.execution_preflight import ExecutionPreflightService
        self.b = fixtures.FreshSupervisorPreparationTests()
        self.b.addCleanup = self.addCleanup
        self.b.addAsyncCleanup = self.addAsyncCleanup
        operation = 'work.handoff.dispatch-existing.v1'
        self.dispatch = CustodyOperation(operation=operation, workers=None, routing=None, threads=None,
            continuity=None, get_work=None, preflight=None)
        verify = CustodyOperation(operation='work.handoff.inspect.v1', workers=None, routing=None, threads=None,
            continuity=None, get_work=None, preflight=None)
        registry = CanonicalOperationRegistry()
        contract = CanonicalOperationContract(operation_id=operation, contract_version='1.0',
            resource_types=('repository',), required_capability='work_item.steer', authority_level='execute',
            input_model=CustodyInput, output_contracts=output_contract(operation), effect='mutation', timeout_seconds=10)
        registry.register(replace(contract, operation_id=verify.operation, effect='read',
            required_capability='work_item.read', authority_level='read', output_contracts=output_contract(verify.operation)), verify)
        original_content = fixtures.bridge_fixtures.procedure_content
        def procedure_content(_):
            content, _contract = original_content('mutation')
            for step, name, capability, level in zip(content['steps'], (operation, verify.operation),
                    ('work_item.steer', 'work_item.read'), ('execute', 'read')):
                step['control_plane']['operation_id'] = name
                step['permissions'][0].update(capability=capability, level=level)
                step['outputs'] = [c.model_dump(mode='json') for c in output_contract(name)]
            return content, contract
        with patch.object(fixtures.bridge_fixtures, 'procedure_content', side_effect=procedure_content), patch.object(
                fixtures.operation_fixtures, 'ControlledOwner', return_value=self.dispatch), patch.object(
                fixtures, 'CanonicalOperationRegistry', return_value=registry):
            await self.b.asyncSetUp()
        self.f = self.b.f
        # The inherited Git materialization edge is a fake backend. Declare its
        # exact existing workspace-id path planner, matching provision_git and
        # provision_git_readonly, so real mixed READ/WRITE lease guards apply.
        backend = self.f.workspaces.backend
        backend.git_workspace_path = lambda workspace_id: backend.root / workspace_id
        profile = AgentProfileExecutionBinding(profile_id='private-quinn', profile_revision=7,
            profile_record_id='private-quinn-record', role_id='recipient', execution_profile_id='repository-write')
        bound = self.b.c.builder.prepare_bootstrap(bootstrap_id='historical-qa-bootstrap',
            execution_id='historical-qa-execution', project_id=self.f.project.id, sandbox='workspace-write',
            approval_policy='on-request', explicit_repository_id=self.f.repo.id,
            execution_profile_id='repository-write', agent_profile=profile,
            runtime_binding=ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex',
                capability_revision=7, authentication_mode='trusted_local_session'))
        claimed = self.f.workers.claim(self.b.c.worker.id, AssignmentClaimRequest(lease_seconds=300),
            actor=self.b.c.worker_actor, assignment_id=bound.assignment_id)
        self.recipient = self.f.workers.start(self.b.c.worker.id, bound.assignment_id, AssignmentStartRequest(
            fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=self.b.c.worker_actor)
        self.preflight = ExecutionPreflightStore(self.f.sqlite)
        self.preflight.prepare_work_item_index()
        self.preflights = ExecutionPreflightService(self.preflight)
        route = SimpleNamespace(preflight=self.preflight)
        self.b.routing.require_resolved_originals = lambda state: OwnerRoutingService.require_resolved_originals(route, state)
        # Current native READ observations and the external dispatch transport
        # are controlled; the actual adapter/engine reservation/diagnostic runs.
        self.b.routing.recovery_recipient = lambda *a, **k:(SimpleNamespace(thread_id='recipient-thread'), self.recipient)
        self.transport = AsyncMock()
        for owner in (self.dispatch, verify):
            owner.workers = self.f.workers
            owner.routing = self.b.routing
            owner.get_work = self.f.host._work_item_state
            owner.preflight = self.preflights
            owner.continuity = SimpleNamespace(dispatch_structured_handoff=self.transport)
        async def inspect_native(context, value):
            context.validate_current()
            return self.f.host._work_item_state(value.work_item_ref), self.recipient, 'positively_idle_live', {}
        self.dispatch._inspect = inspect_native
        self.run = await self.b.activate()
        self.assertEqual(self.run.status, 'running')
        self.assertEqual(self.b.current().procedure_checkpoint.status, 'waiting')
        self.transport.assert_awaited_once()
        self.original_checkpoint = self.b.current().procedure_checkpoint.model_copy(deep=True)
        self.assertIn('custody_actual_start_and_ack_required', self.original_checkpoint.failure_code)
        self.values = self.b.service._original_values(self.run, self.f.actor)
        self.assertEqual(self.values['target_assignment_id'], self.recipient.id)
        self.install_original_start_and_ack()
        self.f.workers.cancel_bootstrap(self.b.current().id, AssignmentCancelRequest(
            expected_fence=self.b.current().fence, reason='Private normal supervisor retirement'), actor=self.f.actor)
        self.retired = self.b.current().model_copy(deep=True)
        self.outcomes = AutomationOutcomeReconciliationService(self.b.runs, self.f.workers)
        self.outcomes.procedure_reconciler = self.b.service.reconcile

    def install_original_start_and_ack(self):
        import time
        from codex_web.models import (QueuedTurn, QueuedNativeStartCorrelation, QueuedNativeStartEvidence,
            QueuedNativeStartReceipt, WorkItemHandoff, WorkItemHandoffDelivery)
        now = time.time() - 4
        self.queued = QueuedTurn(id='original-dispatch', thread_id='recipient-thread', project_id=self.run.project_id,
            execution_id='original-native-request', message='Private typed original delivery',
            work_item_ref=self.run.work_item_ref, handoff_requested_at=10,
            execution_profile_id='repository-write', repository_resource_id=self.f.repo.id,
            writable_repository_resource_ids=(self.f.repo.id,), agent_profile_id='private-quinn',
            agent_profile_revision=7, created_at=now)
        attempt = self.preflights.retain_queued(self.queued, organization_id=self.run.organization_id,
            workspace_id=self.run.workspace_id)
        _, claim = self.preflights.claim_queued_admission(attempt.id)
        self.assertTrue(self.preflights.begin_queued_start(attempt.id, claim_id=claim))
        correlation = QueuedNativeStartCorrelation(snapshot_sha256=self.preflights.queued_snapshot_digest(
            self.queued.model_dump(mode='json')), admission_claim_id=claim, requested_execution_id=self.queued.execution_id,
            organization_id=self.run.organization_id, workspace_id=self.run.workspace_id, project_id=self.run.project_id,
            actor_identity_id=self.recipient.created_by, agent_profile_id='private-quinn', agent_profile_revision=7,
            execution_profile_id='repository-write', repository_resource_id=self.f.repo.id,
            writable_repository_resource_ids=(self.f.repo.id,), assignment_resource_ids=self.recipient.resource_ids,
            assignment_id=self.recipient.id, execution_id=self.recipient.execution_id,
            worker_id=self.recipient.assigned_worker_id, fence=self.recipient.fence, thread_id='recipient-thread',
            provider_native_session_id='private-native-session', runtime_generation='private-generation',
            rpc_request_id='private-rpc', prepared_at=now)
        evidence = QueuedNativeStartEvidence(runtime_generation=correlation.runtime_generation,
            rpc_request_id=correlation.rpc_request_id, provider_native_session_id=correlation.provider_native_session_id,
            provider_native_turn_id='private-native-turn', observed_at=now+1)
        receipt = QueuedNativeStartReceipt(admission_claim_id=claim, requested_execution_id=self.queued.execution_id,
            execution_id=self.recipient.execution_id, thread_id='recipient-thread',
            provider_native_session_id=correlation.provider_native_session_id, provider_native_turn_id='private-native-turn',
            provider_id='openai', runtime_id='codex', observed_at=now+1)
        self.attempt = self.preflight.update(attempt.id, lambda current: current.model_copy(update={
            'status':'started', 'started_at':now+1, 'queued_start_correlation':correlation,
            'queued_start_evidence':evidence, 'queued_start_receipt':receipt}))
        state = self.f.host._work_item_state(self.run.work_item_ref)
        state.handoff.delivery = WorkItemHandoffDelivery(status='delivered', started_at=now+1,
            thread_id='recipient-thread', turn_id='private-native-turn', execution_id=self.recipient.execution_id,
            scope_verified=True, execution_profile_id='repository-write', repository_resource_id=self.f.repo.id,
            writable_repository_resource_ids=(self.f.repo.id,))
        state.current_stage = 'ready_for_validation'
        state.artifact_state = 'merge_request'
        state.implementation_owner = 'sender'
        state.validation_owner = 'recipient'
        self.f.host._save_work_item_state(state)
        # Real ACK/history owner writes the accepted nonce; no operator journal
        # or standalone event supplies that authority. The typed START above is
        # an explicitly controlled native response edge, not a live START claim.
        from pathlib import Path
        from codex_web.models import WorkItemAckCreate
        from codex_web.services.work_item_state import WorkItemStateMachine
        from codex_web.services.work_item_dependencies import WorkItemRuntimeDependencies
        root = Path(self.f.temp.name)
        dependencies = WorkItemRuntimeDependencies(data_dir=root, events_file=root/'history.jsonl',
            load_states=self.f.host._load_work_item_states, save_states=self.f.host._save_work_item_states,
            load_projects=lambda:[self.f.project], resource_ids_for_project=lambda *a,**k:[],
            leading_owner_cue_in_action=lambda _:None, default_validation_owner='recipient',
            default_release_owner='release manager', non_implementation_owners=frozenset({'recipient','release manager'}),
            get_state=self.f.host._work_item_state, save_state=self.f.host._save_work_item_state)
        machine = WorkItemStateMachine(store=self.f.sqlite, dependencies=dependencies)
        state = machine._structured_ack(state.ref, WorkItemAckCreate(actor='recipient',
            expected_handoff_requested_at=10, artifact_state='merge_request'))
        self.assertEqual(state.handoff.status, 'accepted')
        machine._archive_active_handoff(state, now=now+5, status='accepted',
            reason_code='new_canonical_handoff')
        self.assertNotEqual(state.handoff_history[-1], state.handoff_history[-2])
        self.assertEqual(state.handoff_history[-2].reason_code, None)
        self.assertEqual(state.handoff_history[-1].reason_code, 'new_canonical_handoff')
        self.assertEqual(state.handoff_history[-1].model_dump(exclude={'reason_code'}),
            state.handoff_history[-2].model_dump(exclude={'reason_code'}))
        state.handoff = WorkItemHandoff(from_agent='recipient', to_agent='sender', requested_at=now+6)
        self.f.host._save_work_item_state(state)

    def dispose(self):
        options = {'cancel_retired_procedure':True} if 'cancel_retired_procedure' in inspect.signature(self.outcomes.reconcile).parameters else {}
        return self.outcomes.reconcile(self.run.id, actor=self.f.actor, **options)

    async def test_original_mutation_dispatch_with_duplicate_historical_ack_settles_scheduling_only(self):
        self.f.workers.cancel_bootstrap(self.recipient.id, AssignmentCancelRequest(
            expected_fence=self.recipient.fence, reason='Private actual recipient idle retirement'), actor=self.f.actor)
        actual = self.f.workers.store.assignment(self.recipient.id)
        self.assertEqual(actual.fence, self.recipient.fence + 1)
        self.assertIsNone(actual.lease)
        history_before = self.f.host._work_item_state(self.run.work_item_ref).model_copy(deep=True)
        result = None
        refusal = None
        try:
            result = self.dispose()
        except ValueError as exc:
            refusal = str(exc)
        self.assertIsNotNone(result, f'Original settled dispatch stranded Run: {refusal}')
        self.assertEqual(self.f.host._work_item_state(self.run.work_item_ref), history_before)
        self.assertEqual(result.status, 'cancelled')
        self.assertEqual(self.b.current(), self.retired)
        self.assertEqual(self.b.current().procedure_checkpoint, self.original_checkpoint)
        self.transport.assert_awaited_once()
        self.assertEqual(result.procedure_custody, self.run.procedure_custody)

    async def test_conflicting_historical_ack_is_not_settlement(self):
        state = self.f.host._work_item_state(self.run.work_item_ref)
        state.handoff_history[-1].acknowledged_at += 1
        self.f.host._save_work_item_state(state)
        with self.assertRaisesRegex(ValueError, 'historical_ack_unavailable_or_conflicting'):
            self.dispose()

    async def test_non_archival_handoff_facts_remain_conflicting(self):
        state = self.f.host._work_item_state(self.run.work_item_ref)
        original = state.model_copy(deep=True)
        for field, value in (('from_agent', 'other-sender'),
                ('reason', 'Different business reason'), ('expected_action', 'Different requested action'),
                ('artifact_state', 'branch' if original.handoff_history[-1].artifact_state != 'branch' else 'merge_request'),
                ('stage', 'implementation_active' if original.handoff_history[-1].stage != 'implementation_active' else 'ready_for_validation')):
            with self.subTest(field=field):
                changed = original.model_copy(deep=True)
                setattr(changed.handoff_history[-1], field, value)
                self.f.host._save_work_item_state(changed)
                with self.assertRaisesRegex(ValueError, 'historical_ack_unavailable_or_conflicting'):
                    self.dispose()
                self.assertEqual(self.b.service._get(self.run).status, 'running')
                self.assertEqual(self.b.current(), self.retired)
        self.f.host._save_work_item_state(original)
        self.transport.assert_awaited_once()

    async def test_delivery_identity_scope_and_start_facts_remain_conflicting(self):
        from codex_web.models import WorkItemHandoffDelivery
        state = self.f.host._work_item_state(self.run.work_item_ref)
        original = state.model_copy(deep=True)
        delivery = original.handoff_history[-1].delivery
        changes = ({'started_at':delivery.started_at + 0.5},
            {'thread_id':'other-thread'}, {'turn_id':'other-turn'}, {'execution_id':'other-execution'},
            {'scope_verified':False},
            {'repository_resource_id':'other-repository',
             'writable_repository_resource_ids':['other-repository']},
            {'read_only_repository_resource_ids':['other-read-only-repository']})
        for update in changes:
            with self.subTest(fields=tuple(update)):
                changed = original.model_copy(deep=True)
                changed.handoff_history[-1].delivery = WorkItemHandoffDelivery.model_validate(
                    {**delivery.model_dump(mode='json'), **update})
                self.f.host._save_work_item_state(changed)
                with self.assertRaisesRegex(ValueError, 'historical_ack_unavailable_or_conflicting'):
                    self.dispose()
                self.assertEqual(self.b.service._get(self.run).status, 'running')
                self.assertEqual(self.b.current(), self.retired)
        self.f.host._save_work_item_state(original)
        self.transport.assert_awaited_once()

    async def test_missing_canonical_history_cannot_be_replaced_by_old_operator_journal(self):
        state = self.f.host._work_item_state(self.run.work_item_ref)
        state.handoff_history = []
        self.f.host._save_work_item_state(state)
        with self.assertRaisesRegex(ValueError, 'historical_ack_unavailable_or_conflicting'):
            self.dispose()

    async def test_other_nonce_original_start_is_not_borrowed(self):
        self.preflight.update(self.attempt.id, lambda current: current.model_copy(update={
            'queued_turn_snapshot':{**current.queued_turn_snapshot, 'handoff_requested_at':11}}))
        with self.assertRaisesRegex(ValueError, 'historical_recipient_changed'):
            self.dispose()

    async def test_current_native_unknown_still_blocks_known_old_dispatch(self):
        self.preflight.put(ExecutionPreflightAttempt(id='current-unknown', correlation_id='current-unknown',
            execution_id='current-native-request', organization_id=self.run.organization_id,
            workspace_id=self.run.workspace_id, project_id=self.run.project_id, thread_id='current-recipient',
            message='Private retained unknown', status='reconciliation_required', created_at=1, updated_at=1,
            queued_turn_snapshot={'work_item_ref':self.run.work_item_ref}))
        with self.assertRaisesRegex(ValueError, 'native_outcome_unresolved'):
            self.dispose()

    async def test_mismatched_original_target_cannot_borrow_same_work_historical_start(self):
        from codex_web.services.procedure_custody_recovery import CustodyInput
        value = CustodyInput.model_validate({**self.values, 'target_assignment_id':'foreign-recipient'})
        with self.assertRaisesRegex(ValueError, 'historical_recipient_changed'):
            self.dispatch.settled_retired_dispatch(self.retired, value)

    async def test_missing_start_receipt_cannot_settle_mutation_waiting(self):
        self.preflight.update(self.attempt.id, lambda current: current.model_copy(update={
            'queued_start_receipt':None}))
        with self.assertRaisesRegex(ValueError, 'custody_exact_start_proof_unavailable'):
            self.dispose()

    async def test_current_recipient_profile_or_fence_drift_refuses_history_reuse(self):
        current = self.f.workers.store.assignment(self.recipient.id)
        def drift(state):
            state.assignments[0].agent_profile = state.assignments[0].agent_profile.model_copy(update={
                'profile_revision':8})
            return state
        self.f.workers.store.update_assignment(current.assigned_worker_id, current.id, drift)
        with self.assertRaisesRegex(ValueError, 'historical_recipient_changed'):
            self.dispose()

    async def test_uncertain_mutation_checkpoint_cannot_be_cancelled_from_ack_alone(self):
        current = self.b.current()
        def unknown(state):
            state.assignments[0].procedure_checkpoint.steps['canonical'].status = 'reconciliation_required'
            return state
        self.f.workers.store.update_assignment(current.assigned_worker_id, current.id, unknown)
        with self.assertRaisesRegex(ValueError, 'effect_unresolved'):
            self.dispose()
