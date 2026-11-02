"""Real private Work state/store transitions; no application or provider transport."""
from pathlib import Path
import tempfile
import unittest

from fastapi import HTTPException

from codex_web.models import WorkItemAckCreate, WorkItemHandoff, WorkItemProgressUpdate, WorkItemState
from codex_web.services.work_item_dependencies import WorkItemRuntimeDependencies
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.sqlite_state import SQLiteStateStore


class PendingHandoffOwnerPromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SQLiteStateStore(self.root / 'private.sqlite')
        self.repo = ModelMapRepository(self.store, namespace='work_item_states',
            legacy_path=self.root / 'items.json', model=WorkItemState)
        deps = WorkItemRuntimeDependencies(data_dir=self.root, events_file=self.root / 'events.jsonl',
            load_states=self.repo.load, save_states=self.repo.save, load_projects=lambda: [],
            resource_ids_for_project=lambda *a, **k: [], leading_owner_cue_in_action=lambda value: None,
            default_validation_owner='quinn', default_release_owner='release manager',
            non_implementation_owners=frozenset({'quinn', 'orchestrator', 'release manager'}),
            get_state=self.repo.get, save_state=lambda state: self.repo.put(state.ref, state))
        self.machine = WorkItemStateMachine(store=self.store, dependencies=deps)
        self.ref = 'group/platform!57'
        self.machine._save_work_item_state(WorkItemState(ref=self.ref, project_id='project',
            current_owner='james', implementation_owner='james', validation_owner='quinn',
            current_stage='ready_for_validation', next_owner='quinn', artifact_state='branch',
            status_label='status::awaiting confirmation',
            handoff=WorkItemHandoff(from_agent='james', to_agent='quinn', requested_at=180., status='pending'),
            created_at=1., updated_at=1., last_meaningful_update_at=1.))

    def snapshot(self):
        path = self.root / 'events.jsonl'
        return self.repo.get(self.ref).model_dump(mode='json'), path.read_bytes() if path.exists() else None

    def progress(self, **kwargs):
        return self.machine._structured_progress(self.ref, WorkItemProgressUpdate(**kwargs))

    def test_recipient_promotion_without_ack_is_atomically_rejected(self):
        before = self.snapshot()
        with self.assertRaises(HTTPException) as caught:
            self.progress(actor='james', current_owner='quinn', expected_handoff_requested_at=180.,
                note='Validation finished; handoff requested, not acknowledged')
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail['code'], 'pending_handoff_ack_required')
        self.assertEqual(self.snapshot(), before)

    def test_repeated_lane_fields_cannot_promote_recipient(self):
        before = self.snapshot()
        with self.assertRaises(HTTPException) as caught:
            self.progress(actor='james', current_owner='quinn', current_stage='ready_for_validation',
                next_owner='quinn', status_label='status::awaiting confirmation',
                expected_handoff_requested_at=180.)
        self.assertEqual(caught.exception.detail['code'], 'pending_handoff_ack_required')
        self.assertEqual(self.snapshot(), before)

    def test_sender_progress_preserves_exact_pending_intake(self):
        old = self.repo.get(self.ref).handoff
        state = self.progress(actor='james', current_owner='james', note='Evidence recorded')
        self.assertEqual(state.handoff, old)
        self.assertEqual(state.current_owner, 'james')
        self.assertEqual(state.next_owner, 'quinn')
        self.assertFalse(self.machine._work_item_split_brain_findings(state))

    def test_stale_or_missing_nonce_cannot_promote_or_supersede(self):
        before = self.snapshot()
        for nonce in (None, 179.):
            with self.subTest(nonce=nonce), self.assertRaises(HTTPException) as caught:
                self.progress(actor='james', current_owner='quinn', expected_handoff_requested_at=nonce)
            self.assertEqual(caught.exception.detail['code'], 'pending_handoff_conflict')
            self.assertEqual(self.snapshot(), before)

    def test_actual_recipient_ack_still_transfers_ownership(self):
        state = self.machine._structured_ack(self.ref, WorkItemAckCreate(actor='quinn', accepted=True,
            expected_handoff_requested_at=180.))
        self.assertEqual(state.current_owner, 'quinn')
        self.assertEqual(state.handoff.status, 'accepted')
        self.assertIsNotNone(state.handoff.acknowledged_at)
        self.assertFalse(self.machine._work_item_split_brain_findings(state))

    def test_deliberate_stage_supersession_still_archives_pending_intake(self):
        state = self.progress(actor='james', current_owner='quinn',
            current_stage='failed_with_action_owner', expected_handoff_requested_at=180.)
        self.assertIsNone(state.handoff)
        self.assertEqual(state.current_owner, 'quinn')
        self.assertEqual(state.handoff_history[-1].status, 'superseded')
        self.assertIsNone(state.handoff_history[-1].acknowledged_at)

    def test_deliberate_other_owner_supersession_still_archives_pending_intake(self):
        state = self.progress(actor='orchestrator', current_owner='operator',
            expected_handoff_requested_at=180.)
        self.assertIsNone(state.handoff)
        self.assertEqual(state.current_owner, 'operator')
        self.assertEqual(state.handoff_history[-1].reason_code, 'new_canonical_progress')

    def test_exact_nonce_sender_repair_preserves_original_pending_receipt(self):
        state = self.repo.get(self.ref)
        state.current_owner = 'quinn'  # Historical inconsistent state, not a fabricated ACK.
        self.machine._save_work_item_state(state)
        original = state.handoff.model_copy(deep=True)
        repaired = self.progress(actor='local-admin', current_owner='james',
            expected_handoff_requested_at=180., note='Restore sender; recipient ACK remains outstanding')
        self.assertEqual(repaired.current_owner, 'james')
        self.assertEqual(repaired.handoff, original)
        self.assertFalse(self.machine._work_item_split_brain_findings(repaired))
