"""Absent supplemental approval is no Role; full mediated guards still apply."""
from collections import Counter
import json
import unittest
from unittest.mock import patch

from codex_web.storage.approval_requests import ApprovalRequestNotFoundError
from tests import test_procedure_result_projection_composition as projection_fixture


class EmptyApprovalSupplementalRolesTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = projection_fixture.NativeResultProcedureMetadataTests.asyncSetUp
    async def test_empty_current_approval_has_no_definition_identity_resource_reads(self):
        f = self.f
        counts = Counter()
        original_get = f.sqlite.get
        def get(namespace, *args, **kwargs):
            if namespace in ('definition_registry', 'identity_state', 'resource_catalog'):
                counts[namespace] += 1
            return original_get(namespace, *args, **kwargs)
        with patch.object(f.sqlite, 'get', side_effect=get):
            roles = f.runner.authority_approval_roles(self.fixture.intent.execution_id,
                self.fixture.intent.request, f.admin)
        self.assertEqual(roles, ())
        self.assertEqual(dict(counts), {}, json.dumps(dict(counts), sort_keys=True))

    async def test_quarantined_skill_still_refused_by_full_admission_before_io(self):
        from codex_web.definitions import DefinitionLifecycle
        from codex_web.procedure_discovery import procedure_action_intent
        from codex_web.services.skills import SkillResolutionFailure
        f, intent = self.f, self.fixture.intent
        f.registry.store.update(lambda rows: [row.model_copy(update={
            'lifecycle': DefinitionLifecycle.QUARANTINED})
            if row.record_id == f.pin.record_id else row for row in rows])
        self.assertEqual(f.runner.authority_approval_roles(intent.execution_id, intent.request, f.admin), ())
        binding = f.intents.execution.registry.binding(intent.binding_id, f.admin)
        provider = f.intents.execution.registry.provider(binding.provider_type, binding.provider_instance, actor=f.admin)
        definition = f.intents.execution._definition(provider, intent.action_id)
        calls = list(f.observed_calls)
        evaluations = []
        original_evaluate = f.authority.evaluate
        def evaluate(request, *, actor, **kwargs):
            decision = original_evaluate(request, actor=actor, **kwargs)
            evaluations.append((request.capability, tuple(request.approval_role_ids), decision.outcome.value))
            return decision
        with patch.object(f.authority, 'evaluate', side_effect=evaluate):
            # The observer is below this helper's captured direct-hook gate.
            self.assertTrue(f.runner._can_skip_empty_approval_roles())
            with procedure_action_intent(intent.id), self.assertRaises(SkillResolutionFailure) as caught:
                await f.intents.procedure_provider_admission(binding, provider, definition, intent.request,
                    f.worker_actor, 'verify')
        self.assertEqual(caught.exception.code, 'procedure_quarantined')
        self.assertTrue(evaluations)
        self.assertTrue(all(roles == () and outcome == 'allow' for _, roles, outcome in evaluations))

        # Restore the Skill, then expire the actual canonical Role catalog.
        # The ordinary evaluator itself must deny before any provider I/O.
        f.registry.store.update(lambda rows: [row.model_copy(update={
            'lifecycle': DefinitionLifecycle.PUBLISHED})
            if row.record_id == f.pin.record_id else row for row in rows])
        catalog = f.authority.catalog_record(actor=f.admin, project_id=intent.project_id)
        f.registry.store.update(lambda rows: [row.model_copy(update={
            'effective_until': f.runner.clock() - 1})
            if row.record_id == catalog.record_id else row for row in rows])
        evaluations.clear()
        from codex_web.services.action_providers import ActionRequirementError
        with patch.object(f.authority, 'evaluate', side_effect=evaluate):
            self.assertTrue(f.runner._can_skip_empty_approval_roles())
            with procedure_action_intent(intent.id), self.assertRaisesRegex(
                    ActionRequirementError, 'procedure_authority_denied'):
                await f.intents.procedure_provider_admission(binding, provider, definition, intent.request,
                    f.worker_actor, 'verify')
        self.assertTrue(evaluations)
        self.assertTrue(any(outcome == 'deny' for _, _, outcome in evaluations))
        self.assertEqual(f.observed_calls, calls)
        self.assertEqual(f.intents.store.load(intent_id=intent.id).receipts, [])
        f.runner.models.invoke.assert_not_called()

    async def test_custom_callbacks_keep_original_signature_once(self):
        f, intent = self.f, self.fixture.intent
        calls = []
        def custom_approved(assignment, step, actor):
            calls.append(step.id)
            return ('private-test-supplemental-role',)
        with patch.object(f.runner, '_approved_roles', side_effect=custom_approved):
            self.assertEqual(f.runner.authority_approval_roles(intent.execution_id,
                intent.request, f.admin), ('private-test-supplemental-role',))
        self.assertEqual(calls, [self.step.id])
        marker = RuntimeError('Synthetic direct procedure callback refusal')
        def custom_procedure(assignment, actor):
            raise marker
        with patch.object(f.runner, '_procedure', side_effect=custom_procedure):
            with self.assertRaises(RuntimeError) as caught:
                f.runner.authority_approval_roles(intent.execution_id, intent.request, f.admin)
        self.assertIs(caught.exception, marker)

    async def test_actual_approval_and_recovery_states_keep_full_procedure_path(self):
        f, intent = self.f, self.fixture.intent
        for change in ({'approval_id': 'private-missing-approval'},
                       {'rollback_origin_receipt_id': 'private-original-rollback-receipt'}):
            def update(state):
                current = state.assignments[0].procedure_checkpoint.steps[self.step.id]
                for key, value in change.items():
                    setattr(current, key, value)
                return state
            f.workers.store.update_assignment(f.worker.id, f.assignment.id, update)
            counts = Counter()
            original_get = f.sqlite.get
            def get(namespace, *args, **kwargs):
                if namespace == 'definition_registry':
                    counts[namespace] += 1
                return original_get(namespace, *args, **kwargs)
            with patch.object(f.sqlite, 'get', side_effect=get):
                self.assertTrue(f.runner._can_skip_empty_approval_roles())
                if 'approval_id' in change:
                    with self.assertRaises(ApprovalRequestNotFoundError):
                        f.runner.authority_approval_roles(intent.execution_id, intent.request, f.admin)
                else:
                    self.assertEqual(f.runner.authority_approval_roles(
                        intent.execution_id, intent.request, f.admin), ())
            self.assertGreater(counts['definition_registry'], 0)
            def restore(state):
                current = state.assignments[0].procedure_checkpoint.steps[self.step.id]
                for key in change:
                    setattr(current, key, None)
                return state
            f.workers.store.update_assignment(f.worker.id, f.assignment.id, restore)
            counts.clear()
            with patch.object(f.sqlite, 'get', side_effect=get):
                self.assertEqual(f.runner.authority_approval_roles(intent.execution_id, intent.request, f.admin), ())
            self.assertEqual(dict(counts), {})

