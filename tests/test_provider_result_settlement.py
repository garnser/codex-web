"""Actual initial publication and governed partial Evidence, controlled timeout edge.

Only the inherited private GitLab transport/push edge and a deterministic
post-commit projection interruption are controlled. No app/native/live calls.
"""
import asyncio
import copy
import unittest
from unittest.mock import patch, AsyncMock

from tests import test_initial_publication_procedure as initial
from codex_web.execution_workers import AssignmentCompleteRequest
from codex_web.services.post_turn_readiness import PostTurnReadinessService


class ProviderResultSettlementTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = initial.InitialPublicationProcedureTests.asyncSetUp
    prepare_durable = initial.InitialPublicationProcedureTests.prepare_durable
    original = initial.InitialPublicationProcedureTests.original

    async def interrupted(self):
        runner,started=await self.prepare_durable()
        self.partial=[]
        create=self.f.evidence.create_evidence
        raised=False
        def create_then_interrupt(payload, **kwargs):
            nonlocal raised
            proof=create(payload,**kwargs)
            if (not raised and payload.source.startswith('action-intent:')
                    and payload.evidence_type=='provider_receipt'):
                raised=True;self.partial.append(proof.id)
                raise TimeoutError('Controlled interruption AFTER canonical Evidence commit')
            return proof
        with patch.object(self.f.evidence,'create_evidence',side_effect=create_then_interrupt):
            stopped=await runner.advance(started.id,actor=self.f.actor,run_id=started.procedure_checkpoint.id)
        self.assertEqual(self.pushes,1)
        self.assertEqual(len(self.partial),1)
        self.assertEqual(stopped.procedure_checkpoint.status,'reconciliation_required',
            'Successful original became terminal instead of read-only result settlement')
        state=stopped.procedure_checkpoint.steps['publish']
        intent=self.f.intents.get(state.action_intent_id,self.f.actor)
        self.assertEqual(intent.status,'succeeded')
        self.assertEqual(intent.attempt,1)
        self.assertEqual(state.failure_code,'procedure_provider_result_settlement_required')
        self.assertTrue(state.evidence.get('provider_result_interruption'))
        return runner,stopped,intent

    async def resume_without_provider(self, runner, stopped):
        from contextlib import ExitStack
        with ExitStack() as stack:
            forbidden=[]
            for owner,name in [(self.f.intents,'create'),(self.f.intents,'claim'),
                    (self.f.intents,'execute_claimed'),(self.f.intents,'reconcile'),
                    (self.f.execution,'verify'),(self.f.provider,'execute'),
                    (self.f.provider,'verify'),(self.f.client,'branch'),
                    (self.f.client,'read_json_bounded')]:
                forbidden.append(stack.enter_context(patch.object(owner,name,
                    side_effect=AssertionError('No provider/original recovery path: '+name))))
            result=await runner.advance(stopped.id,actor=self.f.actor,
                run_id=stopped.procedure_checkpoint.id)
            for call in forbidden:
                call.assert_not_called()
            return result

    async def test_joined_projection_interruption_settles_same_original_and_partial_proof(self):
        runner,stopped,intent=await self.interrupted()
        before=self.f.intents.history(intent.id,self.f.actor)
        marker=stopped.procedure_checkpoint.steps['publish'].evidence['provider_result_interruption']
        completed=await self.resume_without_provider(runner,stopped)
        state=completed.procedure_checkpoint.steps['publish']
        self.assertEqual(state.status,'succeeded',completed.procedure_checkpoint.failure_code)
        self.assertEqual(completed.procedure_checkpoint.id,stopped.procedure_checkpoint.id)
        self.assertEqual(state.action_intent_id,intent.id)
        self.assertEqual(state.attempts,1)
        self.assertEqual(state.evidence['receipt'],self.partial[0])
        self.assertNotIn('provider_result_interruption',state.evidence)
        diagnostic=self.f.evidence.get_evidence(marker,actor=self.f.actor)
        self.assertEqual(diagnostic.result,'info')
        self.assertEqual(diagnostic.source,runner._step_origin(completed,'publish'))
        self.assertEqual(self.f.intents.history(intent.id,self.f.actor),before)
        self.assertEqual(self.pushes,1)

    async def test_projection_uses_remaining_declared_bound_not_five_seconds(self):
        runner,started=await self.prepare_durable()
        owned=PostTurnReadinessService._run_owned;seconds=[]
        async def observe(operation, **kwargs):
            if kwargs['name'].startswith('procedure-provider-result-'):
                seconds.append(kwargs['seconds'])
            return await owned(operation,**kwargs)
        with patch.object(PostTurnReadinessService,'_run_owned',side_effect=observe):
            completed=await runner.advance(started.id,actor=self.f.actor,run_id=started.procedure_checkpoint.id)
        self.assertEqual(completed.procedure_checkpoint.steps['publish'].status,'succeeded')
        self.assertEqual(len(seconds),1);self.assertGreater(seconds[0],5)
        self.assertLessEqual(seconds[0],30)
        self.assertEqual(self.pushes,1)

    async def test_legacy_untyped_terminal_failure_is_not_reopened(self):
        runner,stopped,intent=await self.interrupted()
        def legacy(state):
            item=state.assignments[0];pin=item.procedure_checkpoint
            pin.status='recovery_required';pin.failure_code='procedure_boundary_failure:TimeoutError'
            pin.steps['publish'].status='failed';pin.steps['publish'].failure_code=pin.failure_code
            pin.steps['publish'].evidence={}
            return state
        self.workers.store.update_assignment(stopped.assigned_worker_id,stopped.id,legacy)
        before=self.workers.store.assignment(stopped.id)
        result=await self.resume_without_provider(runner,before)
        self.assertEqual(result.procedure_checkpoint,before.procedure_checkpoint)
        self.assertEqual(self.pushes,1)

    async def test_missing_interruption_evidence_does_not_authorize_settlement(self):
        runner,stopped,intent=await self.interrupted()
        def clear(state):
            state.assignments[0].procedure_checkpoint.steps['publish'].evidence={}
            return state
        self.workers.store.update_assignment(stopped.assigned_worker_id,stopped.id,clear)
        result=await self.resume_without_provider(runner,stopped)
        self.assertNotEqual(result.procedure_checkpoint.steps['publish'].status,'succeeded')
        self.assertIn('interruption_missing',result.procedure_checkpoint.failure_code)
        self.assertEqual(self.pushes,1)

    async def test_invalidated_diagnostic_refuses_same_original(self):
        runner,stopped,intent=await self.interrupted()
        marker=stopped.procedure_checkpoint.steps['publish'].evidence['provider_result_interruption']
        self.f.evidence.invalidate_evidence(marker,'Private revoked diagnostic',actor=self.f.actor)
        result=await self.resume_without_provider(runner,stopped)
        self.assertNotEqual(result.procedure_checkpoint.steps['publish'].status,'succeeded')
        self.assertEqual(self.pushes,1)

    async def test_changed_positive_original_is_not_accepted_from_old_diagnostic(self):
        runner,stopped,intent=await self.interrupted()
        def change(state):
            state.intents=[x.model_copy(update={'last_verification_id':None}) if x.id==intent.id else x
                for x in state.intents]
            return state
        self.f.intents.store.update(change,intent_ids=(intent.id,))
        result=await self.resume_without_provider(runner,stopped)
        self.assertNotEqual(result.procedure_checkpoint.steps['publish'].status,'succeeded')
        self.assertEqual(self.pushes,1)

    async def test_unknown_original_never_reexecutes_or_infers_settlement(self):
        runner,stopped,intent=await self.interrupted()
        def change(state):
            state.intents=[x.model_copy(update={'status':'uncertain'}) if x.id==intent.id else x for x in state.intents]
            return state
        self.f.intents.store.update(change,intent_ids=(intent.id,))
        result=await self.resume_without_provider(runner,stopped)
        self.assertEqual(result.procedure_checkpoint.status,'reconciliation_required')
        self.assertEqual(self.pushes,1)

    async def test_expired_original_run_does_not_receive_new_settlement_budget(self):
        runner,stopped,intent=await self.interrupted()
        runner.clock=lambda:stopped.procedure_checkpoint.deadline_at+1
        result=await self.resume_without_provider(runner,stopped)
        self.assertNotEqual(result.procedure_checkpoint.steps['publish'].status,'succeeded')
        self.assertEqual(self.pushes,1)

    async def test_canonical_terminal_assignment_cannot_settle_old_checkpoint(self):
        runner,stopped,intent=await self.interrupted()
        current=self.workers.store.assignment(stopped.id)
        self.workers.complete(current.assigned_worker_id,current.id,AssignmentCompleteRequest(
            lease_token=current.lease.lease_token,fence=current.fence,succeeded=False,
            failure_code='private_fixture_retired'),actor=self.f.worker)
        result=await self.resume_without_provider(runner,stopped)
        self.assertNotEqual(result.procedure_checkpoint.steps['publish'].status,'succeeded')
        self.assertEqual(self.pushes,1)

    async def test_foreign_diagnostic_metadata_cannot_relabel_same_original(self):
        runner,stopped,intent=await self.interrupted()
        from codex_web.artifact_evidence import EvidenceCreate
        import json
        marker=stopped.procedure_checkpoint.steps['publish'].evidence['provider_result_interruption']
        old=self.f.evidence.get_evidence(marker,actor=self.f.actor)
        data=json.loads(old.metadata['provider_result_interruption']);data['request_checksum']='f'*64
        new=self.f.evidence.create_evidence(EvidenceCreate(project_id=old.project_id,
            work_item_ref=old.work_item_ref,execution_id=old.execution_id,
            execution_workspace_id=old.execution_workspace_id,resource_ids=old.resource_ids,
            evidence_type='policy_evaluation',result='info',provider=old.provider,source=old.source,
            observed_at=old.observed_at,summary='Private foreign provenance negative',
            metadata={'provider_result_interruption':json.dumps(data)}),actor=self.f.actor)
        def replace(state):
            state.assignments[0].procedure_checkpoint.steps['publish'].evidence={
                'provider_result_interruption':new.id}
            return state
        self.workers.store.update_assignment(stopped.assigned_worker_id,stopped.id,replace)
        result=await self.resume_without_provider(runner,stopped)
        self.assertNotEqual(result.procedure_checkpoint.steps['publish'].status,'succeeded')
        self.assertEqual(self.pushes,1)

    async def test_current_definition_revocation_still_blocks_settlement(self):
        runner,stopped,intent=await self.interrupted()
        runner.skills.definitions.quarantine(stopped.procedure_checkpoint.definition.record_id,
            actor='security',reason='Private current Definition revocation')
        result=await self.resume_without_provider(runner,stopped)
        self.assertNotEqual(result.procedure_checkpoint.steps['publish'].status,'succeeded')
        self.assertEqual(self.pushes,1)

    async def test_changed_coherent_fence_cannot_reuse_old_interruption(self):
        runner,stopped,intent=await self.interrupted()
        def change(state):
            item=state.assignments[0]
            state.assignments[0]=item.model_copy(update={'fence':item.fence+1,
                'lease':item.lease.model_copy(update={'fence':item.fence+1})})
            return state
        self.workers.store.update_assignment(stopped.assigned_worker_id,stopped.id,change)
        result=await self.resume_without_provider(runner,stopped)
        self.assertNotEqual(result.procedure_checkpoint.steps['publish'].status,'succeeded')
        self.assertEqual(self.pushes,1)

    async def test_current_binding_change_refuses_old_original_projection(self):
        runner,stopped,intent=await self.interrupted()
        from codex_web.action_providers import ActionProviderBindingCreate
        old=self.f.binding
        self.f.registry.set_enabled(old.id,False,actor=self.f.actor)
        fresh=self.f.registry.bind(ActionProviderBindingCreate(provider_type=old.provider_type,
            provider_instance=old.provider_instance,project_id=old.project_id,
            resource_ids=old.resource_ids,credential_ref=old.credential_ref,
            security_policy=old.security_policy),actor=self.f.actor,resources=self.f.resources)
        self.assertNotEqual(fresh.id,old.id)
        result=await self.resume_without_provider(runner,stopped)
        self.assertNotEqual(result.procedure_checkpoint.steps['publish'].status,'succeeded')
        self.assertEqual(result.procedure_checkpoint.failure_code,'procedure_action_request_changed')
        self.assertEqual(self.pushes,1)
