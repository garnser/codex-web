"""Private canonical MR/workspace/intent records and real private Git.

External GitLab/push and the shared engine callback are synthetic. No application
composition, native provider, live repository or network is used by this fixture.
"""
from dataclasses import replace
from types import SimpleNamespace
import subprocess
import json
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from codex_web.action_intents import (
    ActionDecisionSnapshot, ActionIntentClaimRequest, ActionIntentCreate,
    ActionIntentRetryRequest, ActionIntentStatus,
)
from codex_web.artifact_evidence import EvidenceRequirement
from codex_web.failures import FailureReason
from codex_web.models import WorkItemState
from codex_web.procedure_control_plane import (
    CanonicalOperationError, CanonicalProcedureContext, CanonicalOperationRegistry, CanonicalPreviousStep,
    current_original_intent, original_intent_scope,
)
from codex_web.resources import ResourceUpdate
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.procedure_publication_recovery import (
    INSPECT, RECOVER, VERIFY, PublicationRecoveryInput, PublicationRecoveryOperations,
    register_publication_operations, request_checksum,
)
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from tests import test_mr_scoped_publication_recovery as fixtures


class PublicationProcedureOperationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = fixtures.MRScopedPublicationRecoveryTests()
        await self.env.asyncSetUp()
        # The borrowed nested fixture registers its own private temp cleanup.
        self.addAsyncCleanup(self.env.f.asyncTearDown)
        self.f, self.a = self.env.f, self.env.assignment
        self.f.resources.update(self.f.repository.id,
            ResourceUpdate(operational_context={'environment': 'development'}), actor=self.f.actor)
        self.worker_actor = self.f.identity.bootstrap_service_actor(identity_id='action-worker',
            name='Private recovery worker', scope=self.f.actor.tenant,
            service_scopes=('action-intent:worker', 'execution-worker:run', 'secret:use'))
        self.workers = ExecutionWorkerService(ExecutionWorkerStore(self.f.sqlite),
            identity=self.f.identity, workspaces=self.f.provider.contract.workspaces)
        worker = self.workers.store.worker(self.a.assigned_worker_id)
        self.f.sqlite.record_apply(ExecutionWorkerStore.worker_namespace, upserts={worker.id:
            worker.model_copy(update={'service_identity_id': self.worker_actor.identity_id}).model_dump(mode='json')})
        self.workspaces = self.f.provider.contract.workspaces
        read_work = lambda ref: WorkItemState.model_validate(self.f.sqlite.record_get('work_item_states', ref))
        self.work_items = SimpleNamespace(state_machine=SimpleNamespace(_work_item_state=read_work))
        canonical_host = SimpleNamespace(_work_item_state=read_work,
            _load_work_item_states=lambda: {self.env.ref: read_work(self.env.ref)})
        authority = install_authority_roles(DefinitionRegistryService(DefinitionRegistryStore(self.f.sqlite)), self.f.resources)
        self.intents = ActionIntentService(ActionIntentStore(self.f.sqlite), self.f.execution,
            identity=self.f.identity, authority=authority, execution_workers=self.workers,
            work_item_host=canonical_host)
        self.git('switch', '-c', 'codex/private-wrong-local-branch')
        request = self.env.request.model_copy(update={'requested_by': self.f.actor.identity_id})
        original = self.intents.create(ActionIntentCreate(binding_id=self.f.binding.id, request=request,
            execution_id=self.a.execution_id, work_item_ref=self.env.ref,
            expected_evidence=((EvidenceRequirement(id='independent-required-receipt', evidence_type='provider_receipt'),)
                if 'missing_evidence' in self._testMethodName else ()),
            policy_decision=ActionDecisionSnapshot(outcome='allow', source='policy:assignment-control-plane',
                capabilities=('repository.branch.publish',))), actor=self.f.actor)
        self.intents.claim(ActionIntentClaimRequest(worker_id=self.a.assigned_worker_id),
            actor=self.worker_actor, intent_id=original.id)
        self.original = await self.intents.execute_claimed(original.id, self.a.assigned_worker_id, actor=self.worker_actor)
        self.assertEqual(self.original.status, 'failed', self.original.last_error)
        self.assertEqual(self.original.failure.source_native_code, 'ActionRequirementError')
        self.values = PublicationRecoveryInput(work_item_ref=self.env.ref,
            execution_workspace_id=self.a.execution_workspace_id,
            expected_assignment_id=self.a.id, expected_assignment_fence=self.a.fence,
            original_intent_id=original.id, expected_original_request_sha256=request_checksum(original.request),
            expected_original_receipt_id=self.original.last_receipt_id,
            expected_head_revision=request.parameters['head_revision'], expected_canonical_branch=self.f.branch)
        self.owner = PublicationRecoveryOperations(workers=self.workers, workspaces=self.workspaces,
            intents=self.intents, work_items=self.work_items)
        self.registry = CanonicalOperationRegistry()
        register_publication_operations(self.registry, self.owner)
        self.engine = SimpleNamespace(authority_approval_roles=lambda *args: (), result_admission=lambda *args: None)
        def retry_guard(intent):
            current = current_original_intent(self.engine, intent)
            if current is None:
                return None
            registration, context, digest = current
            def guard(row):
                if request_checksum(row.request) != digest:
                    raise CanonicalOperationError('fixture_original_digest_changed')
                return registration.adapter.guard_original_retry(context, row)
            return guard
        self.engine.canonical_original_retry_guard = retry_guard
        self.intents.procedure_execution = self.engine
        self.context = self.context_for(RECOVER)

    def git(self, *args):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null',
            '-c', 'core.fsmonitor=false', '-C', str(self.env.workspace.path), *args], text=True).strip()

    def context_for(self, operation):
        step = SimpleNamespace(id='recover', control_plane=SimpleNamespace(operation_id=operation))
        checkpoint = SimpleNamespace(id='private-procedure', steps={'recover': SimpleNamespace(status='running')})
        registration = self.registry.resolve(operation, '1.0')
        return CanonicalProcedureContext(assignment=self.a, checkpoint=checkpoint, step=step,
            requester_actor=self.f.actor, worker_actor=self.worker_actor,
            resource_ids=(self.f.repository.id,), validate_current=lambda: None,
            _original_scope=lambda ctx, original, digest: original_intent_scope(
                self.engine, registration, ctx, original, digest))

    def update_intent(self, **changes):
        def update(state):
            state.intents[0] = state.intents[0].model_copy(update=changes)
            return state
        self.intents.store.update(update, intent_ids=(self.original.id,))

    async def test_known_branch_refusal_waits_without_mutating_git_or_original(self):
        before = self.intents.get(self.original.id, self.f.actor).model_dump(mode='json')
        branch, head = self.git('branch', '--show-current'), self.git('rev-parse', 'HEAD')
        result = await self.owner.execute(self.context, self.values)
        self.assertEqual(result.disposition, 'waiting')
        self.assertEqual(result.code, 'publication_worker_owned_workspace_repair_required')
        self.assertEqual(self.git('branch', '--show-current'), branch)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.intents.get(self.original.id, self.f.actor).model_dump(mode='json'), before)
        self.assertIsNone(self.f.published)

    async def test_remediated_original_reuses_id_key_and_one_normal_attempt(self):
        self.git('switch', self.f.branch)
        head, refs = self.git('rev-parse', 'HEAD'), self.git('show-ref')
        # This original fixture requests provider verification only; it does
        # not create or claim independent QA or complete its Work Item.
        result = await self.owner.execute(self.context, self.values)
        current = self.intents.get(self.original.id, self.f.actor)
        self.assertEqual(current.id, self.original.id)
        self.assertEqual(current.idempotency_key, self.original.idempotency_key)
        self.assertEqual(current.attempt, self.original.attempt + 1)
        self.assertIsNotNone(self.f.published)
        self.assertEqual(self.f.published[3], self.values.expected_head_revision)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.git('show-ref'), refs)
        self.assertEqual(result.disposition, 'succeeded')
        self.assertTrue(result.outputs['verified'])
        self.assertIn(current.last_verification_id, result.original_references)
        self.assertIn(self.original.id, result.original_references)
        self.assertEqual(len(self.intents.list(self.f.actor)), 1)

    async def test_same_head_foreign_result_fields_cannot_borrow_original_verification(self):
        self.git('switch', self.f.branch)
        result = await self.owner.execute(self.context, self.values)
        self.assertTrue(result.outputs['verified'])
        current = self.intents.get(self.original.id, self.f.actor)
        receipt = next(r for r in self.intents.store.load(intent_id=current.id).receipts
            if r.id == current.last_receipt_id)
        original_result = receipt.result.model_copy(deep=True)
        for field, value in (('branch', 'foreign/branch'), ('execution_workspace_id', 'foreign-workspace'),
                             ('resource_id', 'foreign-resource'), ('external_id', 'f' * 40)):
            with self.subTest(field=field):
                forged = original_result.model_copy(update={'external_id': value}) if field == 'external_id' else (
                    original_result.model_copy(update={'output': {**original_result.output, field: value}}))
                # Adversarial canonical-record fixture keeps the exact receipt,
                # key, same head and original verification/timestamps intact;
                # the changed result field must independently prevent reuse.
                def replace_result(state):
                    for r in state.receipts:
                        if r.id == receipt.id:
                            r.result = forged
                    return state
                self.intents.store.update(replace_result)
                observed = await self.owner.execute(self.context_for(VERIFY), self.values)
                self.assertFalse(observed.outputs['verified'])
                self.assertEqual(observed.disposition, 'reconciliation_required')

    async def test_missing_evidence_cannot_promote_provider_delivery_to_verified(self):
        self.git('switch', self.f.branch)
        result = await self.owner.execute(self.context, self.values)
        current = self.intents.get(self.original.id, self.f.actor)
        self.assertIsNotNone(self.f.published)
        self.assertEqual(current.status, 'requires_reconciliation')
        self.assertEqual(result.disposition, 'reconciliation_required')
        self.assertFalse(result.outputs['verified'])

    async def test_unknown_outcome_only_reconciles_read_only(self):
        self.intents._set_status(self.original.id, ActionIntentStatus.UNCERTAIN,
            failure=self.intents._failure(self.original, FailureReason.UNKNOWN_OUTCOME))
        with (patch.object(self.intents, 'execute_claimed', side_effect=AssertionError('no replay')),
              patch.object(self.intents, 'claim', side_effect=AssertionError('no claim'))):
            result = await self.owner.execute(self.context, self.values)
        self.assertEqual(result.disposition, 'reconciliation_required')
        self.assertFalse(result.outputs['verified'])
        self.assertIsNone(self.f.published)

    async def test_historical_original_is_observed_without_retry(self):
        self.update_intent(execution_id='historical-origin')
        with patch.object(self.intents, 'claim', side_effect=AssertionError('historical retry')):
            result = await self.owner.execute(self.context, self.values)
        self.assertEqual(result.disposition, 'reconciliation_required')
        self.assertFalse(result.facts['same_origin'])
        self.assertIsNone(self.f.published)

    async def test_same_intent_writer_refuses_status_race_without_clearing_unknown(self):
        self.git('switch', self.f.branch)
        with self.owner._original(self.context, self.values):
            observed = self.intents.get(self.original.id, self.f.actor)
            self.intents._set_status(observed.id, ActionIntentStatus.UNCERTAIN,
                failure=self.intents._failure(observed, FailureReason.UNKNOWN_OUTCOME))
            before = self.intents.get(observed.id, self.f.actor).model_dump(mode='json')
            with self.assertRaisesRegex(CanonicalOperationError, 'observation_changed'):
                self.intents._schedule_retry(observed, ActionIntentRetryRequest(reason='actual racing canonical owner'))
            self.assertEqual(self.intents.get(observed.id, self.f.actor).model_dump(mode='json'), before)
        self.assertIsNone(self.f.published)

    async def test_ordinary_reconciliation_policy_remains_outside_recovery_scope(self):
        observed = self.intents.get(self.original.id, self.f.actor)
        pending = self.intents._schedule_retry(observed, ActionIntentRetryRequest(reason='existing owner'))
        self.assertEqual(pending.status, 'pending')
        self.assertEqual(pending.id, observed.id)

    async def test_exact_request_receipt_resource_and_actor_are_required(self):
        for changes in ({'expected_original_request_sha256': 'f' * 64},
                        {'expected_original_receipt_id': 'foreign-receipt'},
                        {'work_item_ref': 'foreign/repo!9'}, {'expected_assignment_fence': 2}):
            with self.subTest(changes=changes), self.assertRaises((CanonicalOperationError, KeyError)):
                await self.owner.execute(self.context, self.values.model_copy(update=changes))
        for context in (replace(self.context, resource_ids=('foreign-resource',)),
                        replace(self.context, worker_actor=self.f.actor)):
            with self.assertRaises(CanonicalOperationError):
                await self.owner.execute(context, self.values)
        self.assertIsNone(self.f.published)

    async def test_exhausted_budget_and_disabled_idempotency_refuse(self):
        self.git('switch', self.f.branch)
        for changes in ({'attempt': self.original.retry_policy.max_attempts}, {'provider_idempotency_supported': False}):
            # Keep the known original attempt/receipt paired for the independent
            # policy check; a changed attempt itself is not treated as no effect.
            self.update_intent(**changes)
            result = await self.owner.execute(self.context, self.values)
            self.assertNotEqual(result.disposition, 'succeeded')
            self.assertIsNone(self.f.published)
            self.update_intent(attempt=self.original.attempt, provider_idempotency_supported=True)

    async def test_read_only_inspect_and_verify_do_not_call_mutation_owner(self):
        with patch.object(self.intents, 'reconcile', side_effect=AssertionError('read must not requeue')):
            inspected = await self.owner.execute(self.context_for(INSPECT), self.values)
            verified = await self.owner.execute(self.context_for(VERIFY), self.values)
        self.assertEqual(inspected.disposition, 'succeeded')
        self.assertFalse(inspected.outputs['verified'])
        self.assertEqual(verified.disposition, 'reconciliation_required')

    async def test_repeated_read_only_operations_reconcile_without_original_owner_scope(self):
        with patch.object(self.intents, 'reconcile', side_effect=AssertionError('read must stay read')):
            for operation, expected in ((INSPECT, 'succeeded'), (VERIFY, 'reconciliation_required')):
                with self.subTest(operation=operation):
                    context = self.context_for(operation)
                    result = await self.owner.reconcile(context, self.values)
                    self.assertEqual(result.disposition, expected)
                    self.assertFalse(result.outputs['verified'])
        self.assertIsNone(self.f.published)

    async def test_original_provider_admission_requires_actual_registered_invocation(self):
        with self.assertRaisesRegex(CanonicalOperationError, 'admission_unavailable'):
            self.owner.admit_original(self.context, self.original, self.original.request, 'execute')
        with self.owner._original(self.context, self.values):
            self.assertTrue(self.owner.admit_original(self.context, self.original, self.original.request, 'execute'))
            changed = self.original.request.model_copy(update={'idempotency_key': 'new-key'})
            with self.assertRaises(CanonicalOperationError):
                self.owner.admit_original(self.context, self.original, changed, 'execute')

    def previous_waiting(self, result):
        from codex_web.artifact_evidence import EvidenceCreate
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        service = self.intents.artifact_evidence
        if service is None:
            service = self.intents.artifact_evidence = ArtifactEvidenceService(ArtifactEvidenceStore(self.f.sqlite),
                resources=self.f.resources, work_item_host=self.intents.work_item_host)
        proof = service.create_evidence(EvidenceCreate(project_id=self.a.project_id,
            work_item_ref=self.a.work_item_ref, execution_id=self.a.execution_id,
            execution_workspace_id=self.a.execution_workspace_id, resource_ids=self.a.resource_ids,
            evidence_type='policy_evaluation', result='info', provider='canonical-procedure-owner',
            source=f'procedure:{self.a.id}:{self.context.checkpoint.id}:{self.context.step.id}',
            metadata={'canonical_operation':RECOVER, 'canonical_result':json.dumps({
                'operation':RECOVER, 'facts':result.facts,'original_references':result.original_references,
                'disposition':result.disposition,'code':result.code})}), actor=self.f.actor)
        return CanonicalPreviousStep(status='waiting', failure_code=result.code,
            diagnostic_evidence_ids=(proof.id,), operation_code=result.code)

    async def test_resumed_known_waiting_requires_consumable_exact_diagnostic_and_real_repair(self):
        waiting = await self.owner.execute(self.context, self.values)
        previous = self.previous_waiting(waiting)
        context = replace(self.context, previous_step=previous)
        self.git('switch', self.f.branch)
        result = await self.owner.reconcile(context, self.values)
        self.assertEqual(result.disposition, 'succeeded', result)
        self.assertEqual(self.intents.get(self.original.id,self.f.actor).attempt,self.original.attempt+1)
        self.assertIsNotNone(self.f.published)
        before = self.intents.history(self.original.id,self.f.actor)
        self.assertEqual((await self.owner.reconcile(context,self.values)).disposition,'succeeded')
        self.assertEqual(self.intents.history(self.original.id,self.f.actor),before)

    async def test_resumed_unknown_or_consumed_marker_never_reexecutes_publication(self):
        waiting = await self.owner.execute(self.context,self.values)
        previous = self.previous_waiting(waiting)
        self.git('switch',self.f.branch)
        for marker in (None, replace(previous,status='running',diagnostic_evidence_ids=(),operation_code=None)):
            context = replace(self.context,previous_step=marker)
            result = await self.owner.reconcile(context,self.values)
            self.assertNotEqual(result.disposition,'succeeded')
            self.assertIsNone(self.f.published)
        self.update_intent(status=ActionIntentStatus.UNCERTAIN)
        result = await self.owner.reconcile(replace(self.context,previous_step=previous),self.values)
        self.assertEqual(result.disposition,'reconciliation_required')
        unresolved=self.intents.get(self.original.id,self.f.actor)
        self.assertEqual(unresolved.status,'requires_reconciliation')
        self.assertEqual((unresolved.id,unresolved.request,unresolved.attempt),
            (self.original.id,self.original.request,self.original.attempt))
        self.assertIsNone(self.f.published)

    async def test_foreign_waiting_diagnostic_cannot_borrow_known_failure(self):
        waiting = await self.owner.execute(self.context,self.values)
        previous = self.previous_waiting(waiting)
        service = self.intents.artifact_evidence
        ident = previous.diagnostic_evidence_ids[0]
        original = service.get_evidence(ident,actor=self.f.actor)
        self.git('switch',self.f.branch)
        for change in ({'source':'foreign-step'},{'producer_identity_id':'foreign'},
                {'execution_id':'foreign'},{'resource_ids':('foreign',)},{'work_item_ref':'foreign/repo!7'},
                {'metadata':{**original.metadata,'canonical_result':'{}'}}):
            service.store.update(lambda s:s.model_copy(update={'evidence':[
                original.model_copy(update=change) if p.id==ident else p for p in s.evidence]}))
            result = await self.owner.reconcile(replace(self.context,previous_step=previous),self.values)
            self.assertNotEqual(result.disposition,'succeeded')
            self.assertIsNone(self.f.published)

    def real_engine(self):
        """Actual shared1977 interpreter and canonical owners in this SAME store."""
        from unittest.mock import AsyncMock
        from codex_web.artifact_evidence import ArtifactCreate,sha256_digest
        from codex_web.definitions import DefinitionReference
        from codex_web.skills import SkillCreate,SkillPublish
        from codex_web.services.skills import SkillService
        from codex_web.services.skill_security import SkillSecurityService,SkillScannerRegistry
        from codex_web.storage.skill_security import SkillSecurityStore
        from codex_web.services.artifact_evidence import ArtifactEvidenceService
        from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
        from codex_web.services.artifact_content import ArtifactContentService,ArtifactContentRegistry
        from codex_web.services.local_artifact_content import LocalArtifactContentStore
        from codex_web.services.data_governance import DataGovernanceService
        from codex_web.storage.data_governance import DataGovernanceStore
        from codex_web.services.procedure_discovery import ProcedureDiscoveryService,ProcedureExecutionCapabilityService
        from codex_web.services.procedure_execution import ProcedureExecutionService
        from codex_web.services.canonical_events import CanonicalEventBus,CanonicalEventIngestionService
        from codex_web.storage.canonical_events import CanonicalEventStore
        from codex_web.services.attention import AttentionService
        from codex_web.storage.attention import AttentionStore
        from codex_web.services.approval_requests import ApprovalRequestService
        from codex_web.storage.approval_requests import ApprovalRequestStore
        from tests.test_skill_security import FakeScanner
        registry = self.intents.authority.registry
        service = ArtifactEvidenceService(ArtifactEvidenceStore(self.f.sqlite), resources=self.f.resources,
            work_item_host=self.intents.work_item_host)
        content = ArtifactContentRegistry()
        content.register(LocalArtifactContentStore(Path(self.env.workspace.path).parent/'procedure-content'))
        service.content = ArtifactContentService(content)
        service.governance = DataGovernanceService(DataGovernanceStore(self.f.sqlite))
        self.intents.artifact_evidence = service
        skills = SkillService(registry)
        skills.bind_procedure_contracts(actions=self.intents.execution.registry,resources=self.f.resources,
            scope_actor=lambda ref:self.f.actor,control_plane=self.registry)
        scanners=SkillScannerRegistry();scanners.register(FakeScanner())
        skills.bind_security(SkillSecurityService(SkillSecurityStore(self.f.sqlite),registry,scanners))
        body = json.loads((Path(__file__).resolve().parents[1]/'examples/procedures/workspace-publication-recovery.INACTIVE.json').read_text())['payload']
        active = body['operational_procedure']
        active['owner_identity_id']=self.f.actor.identity_id
        active.update(reviewed_at=time.time()-1,review_due_at=time.time()+3600,expires_at=time.time()+7200)
        for step in active['steps']:
            step['failure']['escalation_owner_identity_id']=self.f.actor.identity_id
        fields={key:value for key,value in body.items() if key in SkillCreate.model_fields}
        fields['operational_procedure']=active
        draft=skills.create(SkillCreate(skill_id='private-original-publication',**fields),actor=self.f.actor)
        published=skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=self.f.actor)
        pin=DefinitionReference.model_validate(published['definitionReference'])
        # Complete three-step contract exceeds the deployment default12k; use
        # the existing explicit finite guidance-capacity contract, never truncate.
        discovery=ProcedureDiscoveryService(skills,self.f.resources,max_context_characters=32000)
        discovery.execution_capabilities=ProcedureExecutionCapabilityService(self.workers,None,None)
        events=CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(self.f.sqlite)))
        self.real_runner=ProcedureExecutionService(workers=self.workers,skills=skills,discovery=discovery,
            intents=self.intents,evidence=service,identity=self.f.identity,authority=self.intents.authority,
            approvals=ApprovalRequestService(ApprovalRequestStore(self.f.sqlite),self.f.identity,events,
                authority_roles=self.intents.authority),attention=AttentionService(AttentionStore(self.f.sqlite),events,
                identity=self.f.identity),control_actor=self.f.actor,worker_actor=self.worker_actor,
            models=SimpleNamespace(invoke=AsyncMock(side_effect=AssertionError('zero model calls'))),control_plane=self.registry)
        self.intents.procedure_execution=self.real_runner
        self.intents.procedure_discovery=discovery
        self.intents.execution.procedure_admission=self.intents.procedure_provider_admission
        data=json.dumps(self.values.model_dump(exclude_none=True)).encode()
        snapshot=service.create_artifact(ArtifactCreate(project_id=self.a.project_id,work_item_ref=self.env.ref,
            execution_id=self.a.execution_id,resource_ids=self.a.resource_ids,artifact_type='report',
            name='Exact original recovery inputs',digest=sha256_digest(data)),actor=self.f.actor)
        snapshot=service.attach_artifact_content(snapshot.id,(data,),actor=self.f.actor)
        self.real_runner.start(self.a.id,definition=pin,input_snapshot_id=snapshot.id,actor=self.f.actor)
        return self.real_runner

    async def test_actual_interpreter_waiting_material_repair_uses_same_original_once(self):
        runner=self.real_engine()
        inspected=await runner.advance(self.a.id,actor=self.f.actor)
        self.assertEqual(inspected.procedure_checkpoint.steps['inspect'].status,'succeeded')
        waiting=await runner.advance(self.a.id,actor=self.f.actor)
        self.assertEqual(waiting.procedure_checkpoint.status,'waiting')
        self.assertIsNone(self.f.published)
        self.git('switch',self.f.branch)
        with patch.object(self.owner,'reconcile',wraps=self.owner.reconcile) as resumed:
            recovered=await runner.advance(self.a.id,actor=self.f.actor)
        resumed_context=resumed.call_args.args[0]
        diagnostic=[runner.evidence.get_evidence(ident,actor=self.f.actor).model_dump(mode='json')
            for ident in resumed_context.previous_step.diagnostic_evidence_ids]
        self.assertEqual(recovered.procedure_checkpoint.steps['reconcile-original'].status,'succeeded',
            {'checkpoint':recovered.procedure_checkpoint,'previous':resumed_context.previous_step,'diagnostic':diagnostic})
        self.assertIsNotNone(self.f.published)
        self.assertEqual(self.intents.get(self.original.id,self.f.actor).attempt,self.original.attempt+1)
        verified=await runner.advance(self.a.id,actor=self.f.actor)
        self.assertEqual(verified.procedure_checkpoint.steps['verify-original'].status,'succeeded')
        completed=await runner.advance(self.a.id,actor=self.f.actor)
        self.assertEqual(completed.procedure_checkpoint.status,'completed')
        runner.models.invoke.assert_not_called()

    async def test_actual_interpreter_unknown_after_waiting_preserves_no_effect(self):
        runner=self.real_engine()
        await runner.advance(self.a.id,actor=self.f.actor)
        waiting=await runner.advance(self.a.id,actor=self.f.actor)
        self.assertEqual(waiting.procedure_checkpoint.status,'waiting')
        self.update_intent(status=ActionIntentStatus.UNCERTAIN)
        self.git('switch',self.f.branch)
        current=await runner.advance(self.a.id,actor=self.f.actor)
        self.assertEqual(current.procedure_checkpoint.status,'reconciliation_required')
        unresolved=self.intents.get(self.original.id,self.f.actor)
        self.assertEqual(unresolved.status,'requires_reconciliation')
        self.assertEqual((unresolved.id,unresolved.request,unresolved.attempt),
            (self.original.id,self.original.request,self.original.attempt))
        self.assertIsNone(self.f.published)
        runner.models.invoke.assert_not_called()
