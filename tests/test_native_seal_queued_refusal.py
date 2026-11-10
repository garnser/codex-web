"""Real queued disposition composed with real pending-custody seal admission."""
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from codex_web.models import QueuedNativeStartReceipt
from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.skills import SkillCreate, SkillPublish
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from tests.test_empty_native_preparation_sealing import EmptyNativePreparationSealingTests
from tests.test_turn_execution import _Host


class NativeSealQueuedRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seal = EmptyNativePreparationSealingTests()
        await self.seal.asyncSetUp()
        self.addAsyncCleanup(self.seal.env.asyncTearDown)
        self.f = self.seal.f
        self.host = _Host()
        self.host._project = lambda _id: self.f.project
        self.runtime = TurnExecutionService(self.host)
        self.preflight = ExecutionPreflightService(ExecutionPreflightStore(self.f.sqlite))
        self.runtime.queued_preflight = self.preflight
        self.runtime.schedule_queue_drain = Mock()
        self.native_write = AsyncMock(return_value={'turn': {'id': 'actual-native-turn'}})

        async def admitted_start(thread_id, **options):
            self.assertEqual(thread_id, 'native-provider-thread')
            self.assertEqual(options['execution_id'], self.f.native_assignment.execution_id)
            # Actual discovery, physical-scope proof and guarded credential CAS.
            # Only the final native writer is a spy; no synthetic refusal detail.
            await self.seal.env.admit()
            return await self.native_write(thread_id, **options)

        self.runtime.start_thread_turn_now = admitted_start
        self.queued = self.runtime.enqueue_turn(thread_id='native-provider-thread',
            project_id=self.f.project.id, message='Review migration architecture; retain this original',
            execution_id=self.f.native_assignment.execution_id, source='web')

    def publish_candidate(self):
        created = self.f.skills.create(SkillCreate(skill_id='new-before-empty-seal',
            name='New provider candidate', instructions='Use the current canonical procedure',
            operational_procedure=self.seal.env.content), actor=self.f.actor)
        self.f.skills.publish(created['skillId'], created['recordId'], SkillPublish(), actor=self.f.actor)

    def retained(self):
        return self.preflight.store.get('preflight-queued-' + self.queued.id)

    async def during_seal(self, change):
        original = self.seal.env.owner.admit_initial_native_credentials

        def before_cas(assignment, selected, actor, validate):
            return change(lambda: original(assignment, selected, actor, validate))

        with patch.object(self.seal.env.owner, 'admit_initial_native_credentials', side_effect=before_cas):
            await self.runtime.drain_thread_queue(self.queued.thread_id)
        self.native_write.assert_not_awaited()
        self.seal.assert_unsealed()
        self.runtime.schedule_queue_drain.assert_not_called()
        return self.retained()

    async def test_catalog_change_during_real_seal_is_retained_as_known_preinput_refusal(self):
        original = self.seal.env.owner.admit_initial_native_credentials
        calls = []

        def before_cas(assignment, selected, actor, validate):
            calls.append(assignment.id)
            self.publish_candidate()
            return original(assignment, selected, actor, validate)

        with patch.object(self.seal.env.owner, 'admit_initial_native_credentials', side_effect=before_cas):
            await self.runtime.drain_thread_queue(self.queued.thread_id)
        retained = self.preflight.store.get('preflight-queued-' + self.queued.id)
        self.assertEqual(calls, [self.f.native_assignment.id])
        self.assertEqual(retained.status, 'blocked')
        self.assertEqual(retained.blockers[0].code, 'procedure_native_preparation_changed')
        self.assertEqual(retained.execution_id, self.queued.execution_id)
        self.assertEqual(retained.queued_turn_snapshot['id'], self.queued.id)
        self.assertEqual(retained.queued_turn_snapshot['message'], self.queued.message)
        self.assertIsNotNone(retained.queued_admission_claim_id)
        self.assertIsNone(retained.started_at)
        self.assertIsNone(retained.queued_start_receipt)
        self.native_write.assert_not_awaited()
        self.seal.assert_unsealed()
        notices = [event for event in self.host.events if event['type'] == 'queued_turn_retained_for_recovery']
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0]['known_unstarted'])
        self.runtime.schedule_queue_drain.assert_not_called()

    async def test_removed_candidates_are_a_closed_no_exclusions_refusal(self):
        def remove_candidates(continue_cas):
            self.f.definition_registry.store.update(lambda rows: [row for row in rows
                if row.kind != 'agent.skill'])
            return continue_cas()

        retained = await self.during_seal(remove_candidates)
        self.assertEqual(retained.status, 'blocked')
        self.assertEqual(retained.blockers[0].code, 'procedure_native_preparation_changed')
        self.assertIn('no supported candidate exclusions', retained.last_error)

    async def test_hidden_candidate_validator_failure_stays_unknown(self):
        def fail_validation(continue_cas):
            with patch.object(self.seal.env.owner, 'validate_native_preparation',
                    side_effect=RuntimeError('validator unavailable')):
                return continue_cas()

        retained = await self.during_seal(fail_validation)
        self.assertEqual(retained.status, 'reconciliation_required')
        self.assertEqual(retained.blockers[0].code, 'native_start_outcome_unknown')
        self.assertFalse(self.preflight.public(retained, actor=self.f.actor)['can_retry'])

    async def test_attestor_failure_stays_unknown(self):
        def fail_attestor(continue_cas):
            with patch.object(self.seal.env.attestor, 'side_effect', RuntimeError('attestor unavailable')):
                return continue_cas()

        retained = await self.during_seal(fail_attestor)
        self.assertEqual(retained.status, 'reconciliation_required')
        self.assertEqual(retained.blockers[0].code, 'native_start_outcome_unknown')

    async def test_hidden_discovery_storage_failure_stays_unknown(self):
        original_read = self.f.skills.definitions.list_records
        reached = []

        def read_records(*args, **options):
            if options.get('kind') == 'agent.skill':
                reached.append(True)
                raise OSError('definition metadata unavailable')
            return original_read(*args, **options)

        def fail_definition_read(continue_cas):
            with patch.object(self.f.skills.definitions, 'list_records',
                    side_effect=read_records):
                return continue_cas()

        retained = await self.during_seal(fail_definition_read)
        self.assertTrue(reached)
        self.assertEqual(retained.status, 'reconciliation_required')
        self.assertEqual(retained.blockers[0].code, 'native_start_outcome_unknown')
        self.assertFalse(self.preflight.public(retained, actor=self.f.actor)['can_retry'])

    async def test_mixed_supported_and_failed_candidate_validation_stays_unknown(self):
        original_validate = self.seal.env.owner.validate_native_preparation
        observed = []

        def validate_candidate(assignment, selected, actor, **options):
            observed.append(selected[0].record_id)
            if selected[0].record_id == self.seal.env.reference.record_id:
                raise RuntimeError('one candidate metadata reader unavailable')
            return original_validate(assignment, selected, actor, **options)

        def fail_one(continue_cas):
            with patch.object(self.seal.env.owner, 'validate_native_preparation',
                    side_effect=validate_candidate):
                return continue_cas()

        retained = await self.during_seal(fail_one)
        self.assertGreaterEqual(len(set(observed)), 2)
        self.assertEqual(retained.status, 'reconciliation_required')
        self.assertFalse(self.preflight.public(retained, actor=self.f.actor)['can_retry'])

    async def test_failed_discovery_after_full_candidate_inventory_stays_unknown(self):
        # Fail the final proof check after the real discovery has enumerated
        # and excluded both candidates. The real discover catcher keeps those
        # exact refs while returning failed; no outcome/candidate DTO is forged.
        from codex_web.services.native_procedure_preparation import NativeProviderPreparation

        def fail_final_proof(continue_cas):
            with patch.object(NativeProviderPreparation, 'permits',
                    side_effect=OSError('final metadata proof unavailable')):
                return continue_cas()

        retained = await self.during_seal(fail_final_proof)
        self.assertEqual(retained.status, 'reconciliation_required')
        self.assertFalse(self.preflight.public(retained, actor=self.f.actor)['can_retry'])

    async def test_plain_zero_candidate_seal_reader_failure_stays_unknown(self):
        self.f.definition_registry.store.update(lambda rows: [row for row in rows
            if row.kind != 'agent.skill'])
        original_read = self.f.skills.definitions.list_records
        reached = []

        def read_records(*args, **options):
            if options.get('kind') == 'agent.skill':
                reached.append(True)
                raise OSError('plain metadata discovery unavailable')
            return original_read(*args, **options)

        def fail_plain_read(continue_cas):
            with patch.object(self.f.skills.definitions, 'list_records',
                    side_effect=read_records):
                return continue_cas()

        retained = await self.during_seal(fail_plain_read)
        self.assertTrue(reached)
        self.assertEqual(retained.status, 'reconciliation_required')
        self.assertFalse(self.preflight.public(retained, actor=self.f.actor)['can_retry'])

    async def test_untyped_http_detail_is_not_noninput_proof(self):
        def spoof_detail(_continue_cas):
            raise HTTPException(409, detail={'code': 'procedure_native_preparation_changed',
                'message': 'untrusted refusal text'})

        retained = await self.during_seal(spoof_detail)
        self.assertEqual(retained.status, 'reconciliation_required')
        self.assertFalse(self.preflight.public(retained, actor=self.f.actor)['can_retry'])

    async def test_positive_original_receipt_wins_over_later_seal_refusal(self):
        def record_then_change(continue_cas):
            current = self.retained()
            self.preflight.record_queued_native_start(current.id, QueuedNativeStartReceipt(
                admission_claim_id=current.queued_admission_claim_id,
                requested_execution_id=current.execution_id, execution_id=current.execution_id,
                thread_id=self.queued.thread_id, provider_native_session_id=self.queued.thread_id,
                provider_native_turn_id='original-observed-native-turn', provider_id='openai', runtime_id='codex',
                observed_at=current.updated_at))
            self.publish_candidate()
            return continue_cas()

        retained = await self.during_seal(record_then_change)
        self.assertEqual(retained.status, 'started')
        self.assertEqual(retained.queued_start_receipt.provider_native_turn_id, 'original-observed-native-turn')
        self.assertEqual(retained.blockers, ())

    async def test_changed_claim_is_not_overwritten_by_old_refusal(self):
        def change_claim(continue_cas):
            current = self.retained()
            self.preflight.store.update(current.id, lambda row: row.model_copy(update={
                'queued_admission_claim_id': 'different-original-owner'}))
            self.publish_candidate()
            return continue_cas()

        retained = await self.during_seal(change_claim)
        self.assertEqual(retained.status, 'reconciliation_required')
        self.assertEqual(retained.queued_admission_claim_id, 'different-original-owner')
        self.assertEqual(retained.blockers, ())

    async def test_unchanged_scope_completes_real_seal_before_native_writer(self):
        await self.runtime.drain_thread_queue(self.queued.thread_id)
        self.native_write.assert_awaited_once()
        self.assertIsNotNone(self.seal.current().native_credential_custody.admitted_at)
        self.assertEqual(self.seal.current().native_credential_custody.procedures, ())
        self.assertEqual(self.f.ops.calls, [])

    async def test_historical_unknown_is_not_reinterpreted_or_replayed(self):
        attempt = self.preflight.retain_queued(self.queued,
            organization_id=self.f.project.organization_id, workspace_id=self.f.project.workspace_id)
        _, claim = self.preflight.claim_queued_admission(attempt.id)
        self.preflight.begin_queued_start(attempt.id, claim_id=claim)
        self.preflight.queued_disposition(attempt.id, status='reconciliation_required', claim_id=claim,
            detail={'code': 'native_start_outcome_unknown',
                'message': 'physical-scope candidates changed or are unsupported'})
        before = self.retained().model_dump(mode='json')
        await self.runtime.drain_thread_queue(self.queued.thread_id)
        self.assertEqual(self.retained().model_dump(mode='json'), before)
        self.native_write.assert_not_awaited()
        self.seal.assert_unsealed()


def load_tests(loader, _tests, _pattern):
    return loader.loadTestsFromTestCase(NativeSealQueuedRefusalTests)
