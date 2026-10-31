"""Actual guarded readiness owners; synthetic provider edge, no native/model I/O."""
import json
import unittest

from codex_web.definitions import DefinitionReference
from codex_web.skills import SkillUpdate, SkillPublish
from codex_web.services.code_host_action_contract import CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_GUARDED_ACTION_ID as GUARDED
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from tests import test_mr_observation_policy as policy_fixture


async def publish_guarded_skill(f, *, timeout=120, parent_timeout=None):
    """An unused generic assignment opts into the exact newly published Skill."""
    await f.v2_skill()
    record = f.registry.get_record(f.pin.record_id)
    procedure = json.loads(json.dumps(record.payload['operational_procedure']))
    procedure['operation_triggers'] = [GUARDED]
    procedure['steps'][0]['action']['action_id'] = GUARDED
    procedure['steps'][0]['timeout_seconds'] = timeout
    procedure['max_duration_seconds'] = timeout if parent_timeout is None else parent_timeout
    draft = f.skills.update(record.definition_id, SkillUpdate(operational_procedure=procedure,
        reason='Private explicit guarded read-only operation opt-in'), actor=f.admin)
    published = f.skills.publish(draft['skillId'], draft['recordId'], SkillPublish(), actor=f.admin)
    f.pin = DefinitionReference.model_validate(published['definitionReference'])
    def bind(state):
        state.assignments[0].skill_refs = (f.pin,)
        return state
    f.workers.store.update_assignment(f.worker.id, f.assignment.id, bind)
    validator = f.broker.validator
    await f.broker.stop()
    current = f.workers.store.assignment(f.assignment.id)
    f.broker = AssignmentBoundControlPlaneBroker(f.service, assignment=current,
        worker_id=f.worker.id, worker_actor=f.worker_actor, fence=current.fence, validator=validator)
    await f.broker.start()
    f.intents.execution.observation_deadline_resolver = f.runner.observation_deadline_for_action


class GuardedReadinessCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = policy_fixture.MRObservationPolicyTests(
            'test_v2_real_publish_start_readonly_receipt_evidence_after_47_seconds')
        await self.f.asyncSetUp()
        self.f.broker_response_timeout = 150

    async def asyncTearDown(self):
        await self.f.asyncTearDown()
        self.f.doCleanups()

    async def test_actual_guarded_publish_start_verify_projection_and_completion(self):
        f = self.f
        f.configure(120)
        await publish_guarded_skill(f)
        result = await f.publish_v2()
        started = await f.start_snapshot(result['procedure_input']['id'])
        advanced = await f.advance_run(started.procedure_checkpoint.id)
        state = advanced.procedure_checkpoint.steps['observe']
        self.assertEqual(state.status, 'succeeded', advanced.procedure_checkpoint.failure_code)
        intent = f.intents.get(state.action_intent_id, f.admin)
        self.assertEqual((intent.action_id, intent.timeout_seconds), (GUARDED, 120))
        self.assertEqual(intent.action_definition.timeout_seconds, 300)
        from codex_web.skill_procedures import OperationalProcedure
        procedure = OperationalProcedure.model_validate(
            f.registry.get_record(f.pin.record_id).payload['operational_procedure'])
        self.assertEqual(procedure.steps[0].timeout_seconds, 120)
        self.assertEqual(f.runner._guarded_readonly_deadline(
            advanced, procedure, procedure.steps[0]),
            min(advanced.procedure_checkpoint.deadline_at,
                state.started_at + 120, advanced.deadline_at or float('inf')))
        history = f.intents.store.load(intent_id=intent.id)
        self.assertEqual(len(history.receipts), 1)
        self.assertIsNotNone(history.receipts[0].result)
        self.assertTrue(history.verifications[-1].verified)
        calls = len(f.observed_calls)
        completed = await f.advance_run(started.procedure_checkpoint.id)
        self.assertEqual(completed.procedure_checkpoint.status, 'completed')
        self.assertEqual(len(f.observed_calls), calls)
        self.assertEqual(json.loads(f.content(state.output_snapshot_id))['readiness'], 'blocked')
        f.runner.models.invoke.assert_not_called()


    async def test_fresh_published_capacity_uses_240_step_with_300_parent(self):
        f = self.f
        f.configure(300)
        await publish_guarded_skill(f, timeout=240, parent_timeout=300)
        result = await f.publish_v2()
        started = await f.start_snapshot(result['procedure_input']['id'])
        advanced = await f.advance_run(started.procedure_checkpoint.id)
        state = advanced.procedure_checkpoint.steps['observe']
        self.assertEqual(state.status, 'succeeded', advanced.procedure_checkpoint.failure_code)
        intent = f.intents.get(state.action_intent_id, f.admin)
        self.assertEqual((intent.timeout_seconds, intent.action_definition.timeout_seconds), (240, 300))
        self.assertAlmostEqual(started.procedure_checkpoint.deadline_at - started.procedure_checkpoint.started_at, 300, delta=0.001)
        history = f.intents.store.load(intent_id=intent.id)
        self.assertEqual(len(history.receipts), 1)
        self.assertTrue(history.verifications[-1].verified)
        calls = len(f.observed_calls)
        completed = await f.advance_run(started.procedure_checkpoint.id)
        self.assertEqual(completed.procedure_checkpoint.status, 'completed')
        self.assertEqual(len(f.observed_calls), calls)
        self.assertEqual(json.loads(f.content(state.output_snapshot_id))['readiness'], 'blocked')
        f.runner.models.invoke.assert_not_called()


    async def test_precreated_120_intent_snapshot_is_not_rewritten_by_larger_catalog(self):
        import asyncio
        from unittest.mock import patch

        f = self.f
        f.configure(300)
        await publish_guarded_skill(f)  # The original published step is still120.
        result = await f.publish_v2()
        started = await f.start_snapshot(result['procedure_input']['id'])
        provider = f.intents.execution.registry.provider(
            'gitlab', 'gitlab.example', actor=f.admin)
        actions = provider.actions
        execute = f.intents.execute_claimed
        claimed = asyncio.Event()
        resume = asyncio.Event()

        def previous_catalog():
            return tuple(row.model_copy(update={'timeout_seconds':120})
                if row.action_id == GUARDED else row for row in actions())

        async def pause_claimed(*args, **kwargs):
            claimed.set()
            await resume.wait()
            return await execute(*args, **kwargs)

        # Pause at the real existing claimed-intent boundary before any HTTP.
        # Restore the current stock catalog before canonical execution resumes.
        with patch.object(f.intents, 'execute_claimed', pause_claimed):
            with patch.object(provider, 'actions', previous_catalog):
                pending = asyncio.create_task(f.advance_run(started.procedure_checkpoint.id))
                await asyncio.wait_for(claimed.wait(), 30)
            try:
                current = f.workers.store.assignment(f.assignment.id)
                intent_id = current.procedure_checkpoint.steps['observe'].action_intent_id
                intent = f.intents.get(intent_id, f.admin)
                original_snapshot = intent.action_definition.model_dump(mode='json')
                self.assertEqual((intent.timeout_seconds, intent.action_definition.timeout_seconds), (120, 120))
                current_definition = next(row for row in provider.actions() if row.action_id == GUARDED)
                self.assertEqual(current_definition.timeout_seconds, 300)
                deadline = f.runner.readonly_operation_deadline(intent, f.admin)
                self.assertLessEqual(deadline,
                    current.procedure_checkpoint.steps['observe'].started_at + 120)
                remaining = f.intents._guarded_readonly_remaining(intent, f.admin)
                self.assertGreater(remaining, 0)
                self.assertLessEqual(remaining, 120)
            finally:
                resume.set()
                advanced = await pending
        state = advanced.procedure_checkpoint.steps['observe']
        self.assertEqual(state.status, 'succeeded', advanced.procedure_checkpoint.failure_code)
        retained = f.intents.get(intent_id, f.admin)
        self.assertEqual(retained.timeout_seconds, 120)
        self.assertEqual(retained.action_definition.model_dump(mode='json'), original_snapshot)
        history = f.intents.store.load(intent_id=intent_id)
        self.assertEqual(len(history.receipts), 1)
        self.assertTrue(history.verifications[-1].verified)
        f.runner.models.invoke.assert_not_called()


class GuardedCheckpointWriterTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the canonical assignment CAS after a simulated lock wait."""

    async def test_positive_writer_deadline_is_checked_inside_cas(self):
        from unittest.mock import patch
        from codex_web.procedure_execution import ProcedureCheckpoint, ProcedureStepCheckpoint
        from codex_web.services.procedure_execution import ProcedureExecutionError

        f = policy_fixture.MRObservationPolicyTests(
            'test_v2_real_publish_start_readonly_receipt_evidence_after_47_seconds')
        await f.asyncSetUp()
        try:
            now = f.runner.clock()
            checkpoint = ProcedureCheckpoint(definition=f.pin, actor_identity_id=f.admin.identity_id,
                input_snapshot_id='synthetic-writer-boundary-only', input_snapshot_checksum='0' * 64,
                resource_context_checksum='synthetic-writer-boundary-only', started_at=now,
                deadline_at=now + 120, steps={'observe': ProcedureStepCheckpoint()})
            current = f.runner._save(f.workers.store.assignment(f.assignment.id), checkpoint)
            original_update = f.workers.store.update_assignment
            clock = [now]

            def delayed_update(worker_id, assignment_id, updater, **kwargs):
                def after_lock(state):
                    clock[0] = now + 2
                    return updater(state)
                return original_update(worker_id, assignment_id, after_lock, **kwargs)

            with patch.object(f.runner, 'clock', side_effect=lambda: clock[0]):
                for status in ('ready', 'completed'):
                    before = f.workers.store.assignment(f.assignment.id)
                    desired = before.procedure_checkpoint.model_copy(deep=True, update={'status': status})
                    clock[0] = now
                    with patch.object(f.workers.store, 'update_assignment', side_effect=delayed_update):
                        with self.assertRaisesRegex(ProcedureExecutionError, 'budget_exhausted'):
                            f.runner._save(before, desired, positive_deadline=now + 1)
                    self.assertEqual(f.workers.store.assignment(f.assignment.id).model_dump(mode='json'),
                        before.model_dump(mode='json'))
                # Expiry must not prevent a truthful error/reconciliation write.
                desired = current.procedure_checkpoint.model_copy(deep=True,
                    update={'status': 'reconciliation_required', 'failure_code': 'synthetic_expired'})
                saved = f.runner._save(current, desired)
                self.assertEqual(saved.procedure_checkpoint.status, 'reconciliation_required')
                self.assertEqual(saved.procedure_checkpoint.failure_code, 'synthetic_expired')
        finally:
            await f.asyncTearDown()
            f.doCleanups()


class LegacyStatusSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_status_write_failure_propagates_without_second_transition(self):
        from unittest.mock import patch
        from tests import test_action_intents as legacy_fixture
        from codex_web.action_intents import ActionIntentStatus

        f = legacy_fixture.ActionIntentTests('test_verified_execution_advances_work_item_only_after_success')
        await f.asyncSetUp()
        try:
            intent = f._create()
            original = f.service._set_status
            transitions = []

            def fail_success(intent_id, status, **kwargs):
                transitions.append(status)
                if status == ActionIntentStatus.SUCCEEDED:
                    raise OSError('synthetic canonical status persistence refusal')
                return original(intent_id, status, **kwargs)

            with patch.object(f.service, '_set_status', side_effect=fail_success):
                with self.assertRaisesRegex(OSError, 'synthetic canonical status'):
                    await f._execute(intent)
            self.assertNotIn(ActionIntentStatus.REQUIRES_RECONCILIATION, transitions)
            history = f.service.history(intent.id, f.actor)
            self.assertEqual(len(history['receipts']), 1)
            self.assertTrue(history['verifications'][-1]['verified'])
        finally:
            await f.asyncTearDown()


class GuardedProjectionBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_projection_uses_same_parent_deadline_and_joins_expired_writer(self):
        """Real cooperative owner, synthetic blocking work; no authority claim."""
        import threading
        import time
        from types import SimpleNamespace
        from codex_web.services.procedure_execution import ProcedureExecutionService
        from codex_web.storage.state_store import storage_operation_budget

        async def project(deadline, delay):
            settled = threading.Event()
            commits = []

            def writer(*_):
                try:
                    time.sleep(delay)
                    storage_operation_budget.get().remaining()
                    commits.append('synthetic-budget-qualified-write')
                    return 'projected'
                finally:
                    settled.set()

            owner = SimpleNamespace(readonly_operation_deadline=lambda *_: deadline,
                clock=time.time, _provider_result=writer)
            try:
                result = await ProcedureExecutionService._provider_result_owned(
                    owner, None, None, SimpleNamespace(id='synthetic-budget'), None, None, None)
                return result, settled.is_set(), commits
            except TimeoutError:
                self.assertTrue(settled.is_set(), 'A timeout cannot leave an unjoined writer')
                self.assertEqual(commits, [])
                raise

        # A legacy operation keeps its whole five-second cooperative bound.
        with self.assertRaises(TimeoutError):
            await project(None, 5.05)
        # New guarded projection may exceed five seconds, within its original D.
        result, settled, commits = await project(time.time() + 20, 5.05)
        self.assertEqual((result, settled, commits),
            ('projected', True, ['synthetic-budget-qualified-write']))
        # That same absolute D is a limit, not a newly granted phase duration.
        with self.assertRaises(TimeoutError):
            await project(time.time() + .01, .04)
