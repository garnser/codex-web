"""Private canonical MR/workspace/intent records and real private Git.

External GitLab/push and the shared engine callback are synthetic. No application
composition, native provider, live repository or network is used by this fixture.
"""
from dataclasses import replace
from types import SimpleNamespace
import subprocess
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
    CanonicalOperationError, CanonicalProcedureContext, CanonicalOperationRegistry,
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
        checkpoint = SimpleNamespace(steps={'recover': SimpleNamespace(status='running')})
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
