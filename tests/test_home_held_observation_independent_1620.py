"""Independent public-hook regressions for request-local held-state observations."""
import math,unittest
from unittest.mock import patch
from tests import test_home_unowned_upgrade as fixture
from codex_web.storage.state_store import storage_operation_budget,check_operation_budget
from codex_web.runtime.execution import ExecutionPrestartRefusal

class HeldObservationIndependentTests(fixture.UnownedHomeUpgradeTests):
    def checkpoint(self):
        return self.planned.checkpoint_current(self.plan.id,worker_runtime=self.runtime,operator_actor=self.operator)

    async def test_whole_hook_finishes_one_original_query_under_same_shared_budget(self):
        self.lose();before=self.preserved();rows=self.f.sqlite.home_intent_recovery_rows
        create=self.artifacts.create_artifact;calls=[];created=[]
        def charge(seconds):
            budget=storage_operation_budget.get();self.assertIsNotNone(budget)
            budget.deadline-=seconds;check_operation_budget()
        def query(*a,**kw):
            calls.append(1);charge(1.111);return rows(*a,**kw)
        def atomic(*a,**kw):
            value=create(*a,**kw);created.append(value.id);charge(1.8);return value
        # Deterministic budget sensitivity, not a claim of measured production cost.
        with patch.object(self.f.sqlite,'home_intent_recovery_rows',side_effect=query),patch.object(self.artifacts,'create_artifact',side_effect=atomic):
            self.assertTrue(self.drain().maintenance_mode)
        self.assertEqual(len(calls),1);self.assertEqual(len(created),1)
        self.assertEqual(self.preserved(),before);self.assertEqual(self.effects,0)

    async def test_unavailable_nonfinite_or_nonrevision_values_retain_full_scans(self):
        self.lose();rows=self.f.sqlite.home_intent_recovery_rows
        for value in [None,float('nan'),float('inf'),-float('inf'),True,'unchanged']:
            with self.subTest(value=value),patch.object(self.f.sqlite,'namespace_revision',return_value=value),patch.object(self.f.sqlite,'home_intent_recovery_rows',wraps=rows) as reader:
                self.checkpoint();self.assertEqual(reader.call_count,3)
        with patch.object(self.f.sqlite,'namespace_revision',None),patch.object(self.f.sqlite,'home_intent_recovery_rows',wraps=rows) as reader:
            self.checkpoint();self.assertEqual(reader.call_count,3)

    async def test_changed_revision_retains_three_complete_queries(self):
        self.lose();rows=self.f.sqlite.home_intent_recovery_rows;revision=[1000.0]
        def changed(*a):revision[0]+=1;return revision[0]
        with patch.object(self.f.sqlite,'namespace_revision',side_effect=changed),patch.object(self.f.sqlite,'home_intent_recovery_rows',wraps=rows) as reader:
            self.checkpoint();self.assertEqual(reader.call_count,3)

    async def test_canonical_mutation_during_first_scan_invalidates_observation(self):
        intent=self.create();self.intents.cancel(intent.id,'Private initial terminal action',actor=self.actor)
        self.lose();rows=self.f.sqlite.home_intent_recovery_rows;calls=[]
        def query(*a,**kw):
            result=rows(*a,**kw);calls.append(1)
            if len(calls)==1:
                def mutate(state):
                    state.intents=[x.model_copy(update={'status':type(x.status)('uncertain')}) if x.id==intent.id else x for x in state.intents]
                    return state
                self.intents.store.update(mutate,intent_ids=(intent.id,))
            return result
        with patch.object(self.f.sqlite,'home_intent_recovery_rows',side_effect=query):
            with self.assertRaises(ExecutionPrestartRefusal):self.checkpoint()
        self.assertGreaterEqual(len(calls),2)
        self.assertIsNone(self.planned._dormant_artifact(self.plan.id,self.actor))
        self.assertEqual(self.intents.get(intent.id,self.actor).status,'uncertain');self.assertEqual(self.effects,0)

    async def test_canonical_change_after_artifact_create_forces_final_original_scan(self):
        intent=self.create();self.intents.cancel(intent.id,'Private initial terminal action',actor=self.actor)
        self.lose();rows=self.f.sqlite.home_intent_recovery_rows;create=self.artifacts.create_artifact;created=[]
        def atomic(*a,**kw):
            artifact=create(*a,**kw);created.append(artifact.id)
            def mutate(state):
                state.intents=[x.model_copy(update={'status':type(x.status)('uncertain')}) if x.id==intent.id else x for x in state.intents]
                return state
            self.intents.store.update(mutate,intent_ids=(intent.id,))
            return artifact
        with patch.object(self.f.sqlite,'home_intent_recovery_rows',wraps=rows) as reader,patch.object(self.artifacts,'create_artifact',side_effect=atomic):
            with self.assertRaises(ExecutionPrestartRefusal):self.drain()
            self.assertEqual(reader.call_count,2)
        self.assertEqual(len(created),1);self.assertEqual(self.effects,0)
        current=self.upgrades.get(self.plan.id,actor=self.operator)
        self.assertFalse(current.maintenance_mode);self.assertEqual(current.admission_epoch,0)
        self.assertEqual(self.planned._dormant_artifact(self.plan.id,self.actor).id,created[0])

    async def test_changed_live_process_guard_is_fresh_even_when_actions_unchanged(self):
        self.lose();rows=self.f.sqlite.home_intent_recovery_rows;calls=[]
        def query(*a,**kw):
            result=rows(*a,**kw);calls.append(1)
            self.observation={'processRunning':True,'pendingExecutorWork':0}
            # Use the existing callback seam rather than changing storage/action revision.
            self.planned.runner_observer=lambda _: {'assignmentId':self.current().id,'fence':self.current().fence,'assignmentStatus':'lost','live':False,'leaseValid':False,**self.observation}
            return result
        with patch.object(self.f.sqlite,'home_intent_recovery_rows',side_effect=query):
            with self.assertRaises(ExecutionPrestartRefusal):self.checkpoint()
        self.assertEqual(len(calls),1);self.assertEqual(self.effects,0)
        self.assertIsNone(self.planned._dormant_artifact(self.plan.id,self.actor))

    async def test_current_session_attribution_is_fresh_when_action_revision_is_unchanged(self):
        self.lose();rows=self.f.sqlite.home_intent_recovery_rows;calls=[]
        original=self.sessions.store.find_unique_by_native_id('operator-thread',provider_id='openai',runtime_id='codex')
        def query(*a,**kw):
            result=rows(*a,**kw);calls.append(1)
            self.sessions.store.upsert(original.model_copy(update={'assignment_id':'foreign-assignment'}))
            return result
        with patch.object(self.f.sqlite,'home_intent_recovery_rows',side_effect=query):
            with self.assertRaises(ExecutionPrestartRefusal):self.checkpoint()
        self.assertEqual(len(calls),1);self.assertEqual(self.effects,0)
        self.assertIsNone(self.planned._dormant_artifact(self.plan.id,self.actor))

    async def test_retained_cancelled_unknown_receipt_remains_unknown_without_replay(self):
        self.after_spawn=lambda:(_ for _ in ()).throw(RuntimeError('Private provider observation lost'))
        intent=self.create();await self.execute(intent)
        self.intents.cancel(intent.id,'Private retention only; no outcome correction',actor=self.actor)
        original=self.intents.get(intent.id,self.actor).model_dump(mode='json')
        self.assertEqual(original['status'],'cancelled');self.lose()
        before=self.preserved();artifact=self.checkpoint()
        raw=artifact.metadata['dormant_preservation']
        self.assertIn('unknown_receipt',raw);self.assertIn('UNKNOWN',raw)
        self.assertEqual(self.intents.get(intent.id,self.actor).model_dump(mode='json'),original)
        self.assertEqual(self.preserved(),before);self.assertEqual(self.effects,1)

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(loader.loadTestsFromName(n,HeldObservationIndependentTests)
        for n in HeldObservationIndependentTests.__dict__ if n.startswith('test_'))
