"""Real governed input/action/proof completion on an unscoped bootstrap.

Only provider HTTP and workspace transport are synthetic. Assignment, workspace,
recovery authorization, action creation/verification and Evidence are real owners.
"""
import json
import time
import unittest
from unittest.mock import patch

from codex_web.execution_subjects import ExecutionSubject
from codex_web.execution_workers import (
    AssignmentCompleteRequest, AssignmentClaimRequest, AssignmentStartRequest,
    ExecutionAssignmentCreate,
)
from codex_web.execution_workspaces import ExecutionWorkspaceAcquire, ExecutionWorkspaceRelease
from codex_web.resources import RepositoryExecutionScope
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSession
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.services.procedure_execution import ProcedureExecutionError
from tests import test_mr_guarded_readonly_budget as guarded


class ProcedurePublicationRecoveryAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await guarded.GuardedReadinessCompositionTests.asyncSetUp(self)
        f = self.f
        f.configure(180)
        await guarded.publish_guarded_skill(f)
        original = f.workers.store.assignment(f.assignment.id)
        previous_session = f.broker.validator.__self__
        await f.broker.stop()
        f.workers.complete(f.worker.id, original.id, AssignmentCompleteRequest(
            fence=original.fence, lease_token=original.lease.lease_token,
            succeeded=False, failure_code='private_fixture_scope_replaced'), actor=f.worker_actor)
        f.workspaces.release(original.execution_workspace_id,
            ExecutionWorkspaceRelease(reason='Private new bootstrap fixture'), actor=f.admin, preserve_files=True)
        subject = ExecutionSubject(kind='thread_bootstrap', ref='private-recovery-procedure')
        workspace = f.workspaces.acquire(ExecutionWorkspaceAcquire(subject=subject,
            execution_id='private-recovery-procedure-execution', project_id=original.project_id,
            resource_ids=original.resource_ids, repository_resource_id=f.target.id), actor=f.admin)
        fields = ExecutionAssignmentCreate.model_fields
        data = {k: v for k, v in original.model_dump(mode='json').items() if k in fields}
        data.update(subject=subject.model_dump(mode='json'), work_item_ref=None,
            execution_id=workspace.execution_id, execution_workspace_id=workspace.id,
            repository_scope=RepositoryExecutionScope(organization_id='local', workspace_id='default',
                project_id=original.project_id, writable_repository_ids=(f.target.id,),
                source='single_repository', source_ref=f.target.id).model_dump(mode='json'))
        created = f.workers.create_assignment(ExecutionAssignmentCreate.model_validate(data), actor=f.admin)
        claimed = f.workers.claim(f.worker.id, AssignmentClaimRequest(),
            actor=f.worker_actor, assignment_id=created.id)
        f.assignment = f.workers.start(f.worker.id, claimed.id, AssignmentStartRequest(
            fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=f.worker_actor)
        f.current_assignment = f.assignment
        self.workspace = f.workspaces.authorize_publication_recovery(workspace.id,
            work_item_ref=f.ref, repository_resource_id=f.target.id,
            assignment_id=f.assignment.id, assignment_fence=f.assignment.fence,
            source_execution_id=f.assignment.execution_id, source_subject=f.assignment.subject,
            assignment_deadline_at=f.assignment.deadline_at, actor=f.admin)
        session = AssignmentBoundAgentProcessSession(previous_session.local_worker, None,
            f.assignment.id, runtime_factory=lambda *a, **k: self.fail('No native process'),
            credential_provider=previous_session.credential_provider)
        self.addCleanup(session._supervision_executor.shutdown, wait=True)
        self.addCleanup(session._diagnostic_executor.shutdown, wait=True)
        session.fence = f.assignment.fence
        session.delegation = previous_session.credential_provider.issue(f.assignment,
            worker_id=f.worker.id, fence=f.assignment.fence, actor=f.worker_actor)
        f.broker = AssignmentBoundControlPlaneBroker(f.service, assignment=f.assignment,
            worker_id=f.worker.id, worker_actor=f.worker_actor, fence=f.assignment.fence,
            validator=session._validate_current_authority)
        await f.broker.start()
        # Work validation remains the exact canonical ModelMap owner.
        f.evidence.work_item_host = f.intents.work_item_host

    async def asyncTearDown(self):
        await guarded.GuardedReadinessCompositionTests.asyncTearDown(self)

    async def start(self):
        published = await self.f.publish_v2()
        started = await self.f.start_snapshot(published['procedure_input']['id'])
        return published, started

    def change_recovery(self, **updates):
        def change(state):
            for i, workspace in enumerate(state.workspaces):
                if workspace.id == self.workspace.id:
                    state.workspaces[i] = workspace.model_copy(update={
                        'publication_recovery': workspace.publication_recovery.model_copy(update=updates)})
            return state
        self.f.workspaces.store.update(change)

    async def test_null_origin_full_creation_verification_proofs_and_completion(self):
        f = self.f
        published, started = await self.start()
        input_id = published['procedure_input']['id']
        original = f.evidence.get_artifact(input_id, actor=f.admin).model_dump(mode='json')
        self.assertIsNone(started.work_item_ref)
        self.assertIsNone(original['work_item_ref'])
        advanced = await f.advance_run(started.procedure_checkpoint.id)
        step = advanced.procedure_checkpoint.steps['observe']
        # Old source reaches the real ActionIntent creation and refuses exactly
        # procedure_action_creation_request_changed, rather than missing an API.
        self.assertEqual(step.status, 'succeeded', advanced.procedure_checkpoint.failure_code)
        intent = f.intents.get(step.action_intent_id, f.admin)
        self.assertEqual(intent.work_item_ref, f.ref)
        self.assertEqual(intent.publication_recovery_scope.recovery, self.workspace.publication_recovery)
        history = f.intents.store.load(intent_id=intent.id)
        self.assertEqual(len(history.receipts), 1)
        self.assertTrue(history.verifications[-1].verified)
        self.assertEqual(set(step.evidence), {'receipt', 'verification'})
        for proof_id in step.evidence.values():
            proof = f.evidence.get_evidence(proof_id, actor=f.admin)
            self.assertEqual((proof.work_item_ref, proof.execution_id, proof.execution_workspace_id),
                (f.ref, started.execution_id, started.execution_workspace_id))
        output = f.evidence.get_artifact(step.output_snapshot_id, actor=f.admin)
        self.assertEqual(output.work_item_ref, f.ref)
        calls = len(f.observed_calls)
        completed = await f.advance_run(started.procedure_checkpoint.id)
        self.assertEqual(completed.procedure_checkpoint.status, 'completed')
        self.assertIsNone(completed.work_item_ref)
        self.assertEqual(f.evidence.get_artifact(input_id, actor=f.admin).model_dump(mode='json'), original)
        self.assertEqual(len(f.observed_calls), calls)
        self.assertEqual(json.loads(f.content(step.output_snapshot_id))['readiness'], 'blocked')
        f.runner.models.invoke.assert_not_called()

    async def test_recovery_change_after_provider_io_retains_receipt_without_new_proof(self):
        f = self.f
        _, started = await self.start()
        original = f.runner._provider_result_owned
        async def changed(assignment, step, intent, receipt, history, actor):
            self.assertEqual(intent.status, 'succeeded')
            self.change_recovery(work_item_ref='group/app#foreign')
            return await original(assignment, step, intent, receipt, history, actor)
        with patch.object(f.runner, '_provider_result_owned', side_effect=changed):
            refused = await f.advance_run(started.procedure_checkpoint.id)
        step = refused.procedure_checkpoint.steps['observe']
        self.assertEqual(refused.procedure_checkpoint.status, 'recovery_required')
        self.assertEqual(refused.procedure_checkpoint.failure_code, 'procedure_action_recovery_scope_changed')
        self.assertEqual(step.evidence, {})
        history = f.intents.store.load(intent_id=step.action_intent_id)
        self.assertEqual(len(history.receipts), 1)
        self.assertTrue(history.verifications[-1].verified)
        self.assertIsNone(refused.work_item_ref)

    async def _reject_before_effect(self, kind):
        f = self.f
        _, started = await self.start()
        if kind == 'expiry':
            self.change_recovery(expires_at=time.time() - 1)
        elif kind == 'repository':
            def change_repository(state):
                for i, workspace in enumerate(state.workspaces):
                    if workspace.id == self.workspace.id:
                        foreign = 'foreign-repository'
                        changed = workspace.model_copy(update={
                            'repository_resource_id': foreign,
                            'writable_repository_ids': (foreign,),
                            'repository_members': tuple(member.model_copy(update={
                                'resource_id': foreign}) for member in workspace.repository_members),
                            'repository_checkpoints': {},
                            'publication_recovery': workspace.publication_recovery.model_copy(update={
                                'repository_resource_id': foreign})})
                        state.workspaces[i] = type(workspace).model_validate(changed.model_dump())
                return state
            f.workspaces.store.update(change_repository)
        elif kind == 'fence':
            self.change_recovery(assignment_fence=started.fence + 1)
        elif kind == 'owner':
            def change_owner(state):
                for i, workspace in enumerate(state.workspaces):
                    if workspace.id == self.workspace.id:
                        changed = workspace.model_copy(update={
                            'owner_identity_id': 'foreign-owner',
                            'publication_recovery': workspace.publication_recovery.model_copy(update={
                                'authorized_by': 'foreign-owner'})})
                        state.workspaces[i] = type(workspace).model_validate(changed.model_dump())
                return state
            f.workspaces.store.update(change_owner)
        else:
            def expire(state):
                for i, lease in enumerate(state.leases):
                    if lease.id == self.workspace.lease_id:
                        state.leases[i] = lease.model_copy(update={'released_at': time.time()})
                return state
            f.workspaces.store.update(expire)
        calls = len(f.observed_calls)
        if kind == 'lease':
            # Current-input admission refuses before the state-machine catch.
            with self.assertRaises(ProcedureExecutionError) as caught:
                await f.runner.advance(f.assignment.id, actor=f.admin,
                    run_id=f.runner.run_id(started.procedure_checkpoint))
            self.assertEqual(caught.exception.code, 'procedure_resource_lease_expired_or_revoked')
            refused = f.runner.get(f.assignment.id, f.admin)
            self.assertEqual(refused.procedure_checkpoint.status, started.procedure_checkpoint.status)
        else:
            refused = await f.advance_run(started.procedure_checkpoint.id)
            self.assertEqual(refused.procedure_checkpoint.status, 'recovery_required')
        self.assertIsNone(refused.procedure_checkpoint.steps['observe'].action_intent_id)
        self.assertEqual(len(f.observed_calls), calls)
        self.assertIsNone(refused.work_item_ref)

    async def test_expired_recovery_refuses_before_provider(self):
        await self._reject_before_effect('expiry')

    async def test_foreign_repository_refuses_before_provider(self):
        await self._reject_before_effect('repository')

    async def test_changed_recovery_fence_refuses_before_provider(self):
        await self._reject_before_effect('fence')

    async def test_released_workspace_lease_refuses_before_provider(self):
        await self._reject_before_effect('lease')

    async def test_changed_workspace_owner_refuses_before_provider(self):
        await self._reject_before_effect('owner')
