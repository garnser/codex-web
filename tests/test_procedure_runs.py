"""Real published readonly MR procedure, retained proofs and same assignment.

Provider HTTP is private MockTransport; no native process/model or live API.
"""
import asyncio
import copy
import json
import unittest
from unittest.mock import Mock
from types import SimpleNamespace

from codex_web.services.procedure_execution import ProcedureExecutionError
from codex_web.services.assignment_reasoning_broker import AssignmentReasoningBroker, OPERATIONS
from codex_web.procedure_execution import ProcedureCheckpoint
from tests.test_premerge_readiness_observation import PremergeReadinessProcedureTests


class ProcedureRunTests(PremergeReadinessProcedureTests):
    async def advance(self):
        current = self.workers.store.assignment(self.assignment.id)
        result = await self.runner.advance(current.id, actor=self.actor,
            run_id=self.runner.run_id(current.procedure_checkpoint))
        return result.procedure_checkpoint

    async def complete(self):
        observed = await self.advance()
        self.assertEqual(observed.steps['observe'].status, 'succeeded', observed.failure_code)
        completed = await self.advance()
        self.assertEqual(completed.status, 'completed', completed.failure_code)
        return self.workers.store.assignment(self.assignment.id)

    def changed_input(self, head='c'):
        self.mr['sha'] = head * 40
        self.mr['head_pipeline']['sha'] = head * 40
        return self.artifact(json.dumps({'pull_request_number':33,
            'expected_head_sha':head * 40}).encode(), 'changed-exact-mr-input')

    def start_new(self, snapshot):
        return self.runner.start(self.assignment.id, definition=self.pin,
            input_snapshot_id=snapshot.id, actor=self.actor)

    async def test_secret_reference_healthy_assignment_accepts_second_changed_input(self):
        # Also executable against the unmodified old owner: its first failure
        # must be the second start's active-pin conflict, not a new-field access.
        for _ in range(2):
            first=await self.runner.advance(self.assignment.id,actor=self.actor)
        self.assertEqual(first.procedure_checkpoint.status,'completed')
        changed=self.changed_input()
        second=self.start_new(changed)
        self.assertEqual(second.id,first.id)
        self.assertEqual(second.procedure_checkpoint.input_snapshot_id,changed.id)
        self.assertEqual(second.lease,first.lease)

    async def test_secret_reference_changed_mr_second_run_preserves_first_and_resource_accounting(self):
        before = await self.complete()
        original = before.procedure_checkpoint.model_dump(mode='json')
        first = before.procedure_checkpoint.steps['observe']
        original_intent = self.intents.store.load(intent_id=first.action_intent_id).model_dump(mode='json')
        original_evidence = {eid:self.artifacts.get_evidence(eid,actor=self.actor).model_dump(mode='json')
            for eid in first.evidence.values()}
        workspace_before = self.workspaces.store.load().model_dump(mode='json')
        snapshot = self.changed_input()
        started = self.start_new(snapshot)
        self.assertNotEqual(started.procedure_checkpoint.id,before.procedure_checkpoint.id)
        self.assertEqual(started.procedure_history[0].model_dump(mode='json'),original)
        for field in ('id','execution_id','assigned_worker_id','status','fence','lease','limits','deadline_at',
                      'started_at','runtime_binding','execution_workspace_id','resource_ids','agent_profile'):
            self.assertEqual(getattr(started,field),getattr(before,field),field)
        second = await self.complete()
        new_intent = self.intents.get(second.procedure_checkpoint.steps['observe'].action_intent_id,self.actor)
        self.assertNotEqual(new_intent.id,first.action_intent_id)
        self.assertIn(second.procedure_checkpoint.id,new_intent.idempotency_key)
        self.assertEqual(second.procedure_history[0].model_dump(mode='json'),original)
        self.assertEqual(self.intents.store.load(intent_id=first.action_intent_id).model_dump(mode='json'),original_intent)
        for eid,value in original_evidence.items():
            self.assertEqual(self.artifacts.get_evidence(eid,actor=self.actor).model_dump(mode='json'),value)
        self.assertEqual(self.workspaces.store.load().model_dump(mode='json'),workspace_before)
        self.assertEqual(len(self.observed_calls),28)
        self.runner.models.invoke.assert_not_called()

    async def test_secret_reference_copied_input_and_historical_input_cannot_replay(self):
        first = await self.complete()
        copied = self.artifact(b'{"expected_head_sha":"' + b'a'*40 + b'", "pull_request_number":33}', 'same-values-new-id')
        unchanged = self.start_new(copied)
        self.assertEqual(unchanged.procedure_checkpoint,first.procedure_checkpoint)
        self.assertEqual(unchanged.procedure_history,())
        self.start_new(self.changed_input())
        await self.complete()
        with self.assertRaisesRegex(ProcedureExecutionError,'procedure_input_already_completed'):
            self.start_new(copied)
        self.assertEqual(len(self.observed_calls),28)

    async def test_secret_reference_stale_advance_and_historical_lookup_never_change_old_run(self):
        first = await self.complete()
        self.start_new(self.changed_input())
        current = self.workers.store.assignment(self.assignment.id)
        for run_id in (None,first.procedure_checkpoint.id,'procedure-run-'+'0'*32):
            with self.subTest(run_id=run_id), self.assertRaisesRegex(ProcedureExecutionError,'procedure_run_not_current'):
                await self.runner.advance(current.id,actor=self.actor,run_id=run_id)
        view = self.runner.inspect(current.id,self.actor,run_id=first.procedure_checkpoint.id)
        self.assertEqual(view['procedure_checkpoint'],first.procedure_checkpoint.model_dump(mode='json'))
        self.assertEqual(view['current_run_id'],current.procedure_checkpoint.id)
        self.assertEqual(len(view['procedure_runs']),2)
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from codex_web.api.procedure_execution import build_procedure_execution_router
        app=FastAPI()
        @app.middleware('http')
        async def authenticated(request:Request,call_next):
            request.state.identity_actor=self.actor
            return await call_next(request)
        app.include_router(build_procedure_execution_router(self.runner))
        base=f'/api/execution-workers/assignments/{current.id}/procedure'
        with TestClient(app) as client:
            self.assertEqual(client.get(base,params={'run_id':first.procedure_checkpoint.id}).json(),view)
            for body in ({},{'run_id':first.procedure_checkpoint.id}):
                response=client.post(base+'/advance',json=body)
                self.assertEqual(response.status_code,409,response.text)
                self.assertEqual(response.json()['detail']['code'],'procedure_run_not_current')
        self.assertEqual(len(self.observed_calls),14)
        stale = first.model_copy(deep=True)
        with self.assertRaisesRegex(ProcedureExecutionError,'procedure_checkpoint_conflict'):
            self.runner._save(stale,stale.procedure_checkpoint)
        self.assertEqual(self.workers.store.assignment(current.id),current)

    async def test_secret_reference_unfinished_and_uncertain_old_effect_refuse_rollover(self):
        snapshot = self.changed_input()
        with self.assertRaisesRegex(ProcedureExecutionError,'procedure_active_pin_conflict'):
            self.start_new(snapshot)
        self.mr['sha']='a'*40;self.mr['head_pipeline']['sha']='a'*40
        completed=await self.complete()
        intent_id=completed.procedure_checkpoint.steps['observe'].action_intent_id
        def make_unknown(state):
            state.intents[0].status='requires_reconciliation'
            return state
        self.intents.store.update(make_unknown,intent_ids=(intent_id,))
        with self.assertRaisesRegex(ProcedureExecutionError,'procedure_run_not_settled'):
            self.start_new(snapshot)
        current=self.workers.store.assignment(completed.id)
        self.assertEqual(current.procedure_checkpoint,completed.procedure_checkpoint)
        self.assertEqual(current.procedure_history,())

    async def test_secret_reference_revoked_definition_and_expired_assignment_refuse_new_run(self):
        completed=await self.complete();snapshot=self.changed_input()
        self.definition_registry.quarantine(self.pin.record_id,actor=self.actor.identity_id,reason='private current trust revocation')
        with self.assertRaises(ProcedureExecutionError):self.start_new(snapshot)
        self.assertEqual(self.workers.store.assignment(completed.id).procedure_history,())

    async def test_secret_reference_legacy_checkpoint_retains_keys_and_historical_identity(self):
        current=self.workers.store.assignment(self.assignment.id)
        raw=current.procedure_checkpoint.model_dump(mode='json')
        raw.pop('id');raw.pop('canonical_input_checksum');raw['schema_version']='1.0'
        legacy=ProcedureCheckpoint.model_validate(raw)
        self.assertEqual(legacy.model_dump(mode='json'),raw)
        def replace(state):
            state.assignments[0].procedure_checkpoint=legacy
            return state
        self.workers.store.update_assignment(self.worker.id,current.id,replace)
        # Explicit domain migration preserves the old nested proof bytes and
        # requires a newer reader; the physical record store does not migrate.
        old_state=self.workers.store.load().model_dump(mode='json')
        old_state['schema_version']='1.12'
        for item in old_state['assignments']:item.pop('procedure_history',None)
        migrated=self.workers.store._decode(old_state)
        self.assertEqual(migrated.schema_version,'1.13')
        self.assertEqual(migrated.assignments[0].procedure_checkpoint.model_dump(mode='json'),raw)
        self.assertEqual(migrated.assignments[0].procedure_history,())
        from codex_web.compatibility import ContractSpec, ContractCompatibilityError
        with self.assertRaises(ContractCompatibilityError):
            ContractSpec('execution-worker-state','1.12',('1.12',)).require(migrated.schema_version)
        first=await self.complete()
        intent=self.intents.get(first.procedure_checkpoint.steps['observe'].action_intent_id,self.actor)
        self.assertEqual(intent.idempotency_key,f'procedure:{current.id}:{self.pin.checksum}:observe')
        self.start_new(self.changed_input())
        view=self.runner.inspect(current.id,self.actor,run_id='legacy')
        self.assertEqual(view['procedure_checkpoint'],first.procedure_checkpoint.model_dump(mode='json'))
        self.assertEqual(view['procedure_runs'][0]['id'],'legacy')

    async def test_secret_reference_racing_changed_starts_commit_one_archive_without_reset(self):
        completed=await self.complete()
        one=self.changed_input('c');two=self.changed_input('d')
        results=await asyncio.gather(*(asyncio.to_thread(self.start_new,snapshot) for snapshot in (one,two)),return_exceptions=True)
        self.assertEqual(sum(not isinstance(x,Exception) for x in results),1,results)
        current=self.workers.store.assignment(completed.id)
        self.assertEqual(len(current.procedure_history),1)
        self.assertEqual(current.procedure_history[0],completed.procedure_checkpoint)
        self.assertEqual(current.lease,completed.lease)

    async def test_secret_reference_assignment_broker_admits_changed_run_and_returns_exact_history(self):
        completed=await self.complete();snapshot=self.changed_input()
        # Actual broker ownership adapter; only its external Profile/transport
        # collaborators are private fixtures. The runner/identity/authority,
        # assignment/workspace/Intent/provider/proof owners remain canonical.
        operation_ids=[x.id for x in OPERATIONS]
        broker=SimpleNamespace(execution_workers=self.workers,
            execution_profiles=SimpleNamespace(resolve=lambda *a,**k:(SimpleNamespace(control_plane_operations=operation_ids),completed.execution_profile_definition)),
            _actor=Mock(),_requester_actor=Mock(return_value=self.actor),_authorize=Mock())
        adapter=AssignmentReasoningBroker(broker,procedures=self.runner,executives=None)
        start=next(x for x in OPERATIONS if x.id=='procedure.start')
        result=await adapter.dispatch(completed,self.worker_actor,start,completed.id,
            {'definition':self.pin.model_dump(mode='json'),'input_snapshot_id':snapshot.id},{})
        self.assertNotEqual(result['current_run_id'],completed.procedure_checkpoint.id)
        read=next(x for x in OPERATIONS if x.id=='procedure.read')
        from codex_web.services.control_plane_broker import ControlPlaneBrokerService
        resolved=ControlPlaneBrokerService._resolve_operation("GET",
            f"/api/execution-workers/assignments/{completed.id}/procedure?run_id={completed.procedure_checkpoint.id}")
        history=await adapter.dispatch(completed,self.worker_actor,read,completed.id,{},resolved.query)
        self.assertEqual(history['procedure_checkpoint'],completed.procedure_checkpoint.model_dump(mode='json'))
        self.assertEqual(broker._authorize.call_count,2)
        from fastapi import HTTPException
        for values in ([], [completed.procedure_checkpoint.id, completed.procedure_checkpoint.id]):
            with self.subTest(query_cardinality=len(values)):
                with self.assertRaises(HTTPException) as refused:
                    await adapter.dispatch(completed,self.worker_actor,read,completed.id,{}, {'run_id':values})
                self.assertEqual(refused.exception.status_code,403)
        self.assertEqual(broker._authorize.call_count,2)


    async def test_secret_reference_retained_outcome_knowledge_is_bound_to_original_run(self):
        from tests.test_procedure_outcomes import _ProcedureOutcomeFixture
        first=await self.complete()
        service=_ProcedureOutcomeFixture.outcomes(self)
        original=service.convert(first.id,actor=self.actor,run_id=first.procedure_checkpoint.id)
        self.assertEqual(original['status'],'draft',original)
        self.start_new(self.changed_input())
        await self.complete()
        historical=service.convert(first.id,actor=self.actor,run_id=first.procedure_checkpoint.id)
        self.assertEqual(historical['knowledge_id'],original['knowledge_id'])
        self.assertEqual(historical['draft']['recordId'],original['draft']['recordId'])
        self.assertEqual(historical['report']['run_id'],first.procedure_checkpoint.id)
        current=service.extract(first.id,actor=self.actor)[2]
        self.assertNotEqual(current['run_id'],original['report']['run_id'])
        record=self.definition_registry.get_record(original['draft']['recordId'])
        service.assert_source(record,actor=self.actor)

    async def test_secret_reference_expired_assignment_and_stale_fence_refuse_rollover(self):
        from unittest.mock import patch
        completed=await self.complete();snapshot=self.changed_input()
        with patch.object(self.runner,'clock',return_value=completed.lease.expires_at):
            with self.assertRaisesRegex(ProcedureExecutionError,'procedure_assignment_fence_expired_or_revoked'):
                self.start_new(snapshot)
        candidate=completed.procedure_checkpoint.model_copy(deep=True)
        candidate.id='procedure-run-'+'f'*32
        candidate.status='ready'
        stale=completed.model_copy(update={'fence':completed.fence+1})
        with self.assertRaisesRegex(ProcedureExecutionError,'procedure_checkpoint_conflict'):
            self.runner._rollover(stale,candidate)
        self.assertEqual(self.workers.store.assignment(completed.id).procedure_history,())


def load_tests(loader, tests, pattern):
    # Reuse one canonical fixture without rerunning its inherited unrelated cases.
    return unittest.TestSuite(ProcedureRunTests(name) for name in ProcedureRunTests.__dict__ if name.startswith('test_'))
