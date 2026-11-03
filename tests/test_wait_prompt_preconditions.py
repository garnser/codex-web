"""Canonical Work/progress wait preconditions and dispatch guidance; no app/native IO."""
import unittest

from fastapi import HTTPException
from codex_web.models import WorkItemHandoff, WorkItemProgressUpdate
from codex_web.services.work_item_dispatch_prompt import WorkItemDispatchPromptPolicy
from tests import test_work_item_unchanged_wait as wait_fixture


class WaitPromptPreconditionTests(unittest.IsolatedAsyncioTestCase):
    setUp = wait_fixture.UnchangedWaitTests.setUp
    state = wait_fixture.UnchangedWaitTests.state
    request = wait_fixture.UnchangedWaitTests.request
    progress = wait_fixture.UnchangedWaitTests.progress

    def prompt(self):
        return WorkItemDispatchPromptPolicy(coerce_owner=lambda value: (value or '').strip().lower(),
            coordination_channel='coordination').render(self.state())

    def lane(self, **changes):
        state = self.state().model_copy(update={'current_owner':'sally', 'next_owner':'sally', **changes})
        self.repo.put(state.ref, state)
        return state

    async def test_active_no_blocker_prompt_does_not_advertise_refused_wait(self):
        self.lane(current_stage='implementation_active', blocker=None,
            next_action='Await the actual dependency receipt')
        before = self.state()
        with self.assertRaises(HTTPException) as refused:
            await self.progress(self.request())
        self.assertEqual(refused.exception.detail['code'], 'work_item_wait_checkpoint_changed')
        self.assertEqual(self.state(), before)
        self.assertNotIn('wait_checkpoint={', self.prompt(),
            'Prompt must not advertise the wait request the canonical owner just refused')
        self.assertIn('first record truthful structured progress', self.prompt())
        self.assertIn('Then re-read current Work', self.prompt())

    async def test_eligible_blocked_prompt_matches_real_wait_without_self_wake(self):
        before = self.lane()
        self.assertIn('wait_checkpoint={', self.prompt())
        self.assertIn('exact current hash', self.prompt())
        await self.progress(self.request())
        self.assertIsNotNone(self.state().wait_checkpoint)
        self.assertEqual(self.state().last_meaningful_update_at, before.last_meaningful_update_at)
        self.callbacks.schedule_actionable_owner_dispatch.assert_not_called()
        self.callbacks.schedule_actionable_owner_continuity_check.assert_not_called()

    async def test_real_blocked_progress_precedes_new_hash_wait_receipt(self):
        from codex_web.work_item_wait import wait_context
        self.lane(current_stage='implementation_active', blocker=None)
        before = wait_context(self.state())
        self.assertFalse(before['state_eligible'])
        await self.progress(WorkItemProgressUpdate(actor='sally', current_owner='sally', next_owner='sally',
            current_stage='failed_with_action_owner', blocker='Actual governed dependency receipt unavailable',
            next_action='Wait for the dependency owner to publish its receipt',
            note='Concrete prerequisite inspection identified this blocker'))
        self.callbacks.schedule_actionable_owner_dispatch.assert_called_once()
        fresh = wait_context(self.state())
        self.assertTrue(fresh['state_eligible'])
        self.assertNotEqual(fresh['input_sha256'], before['input_sha256'])
        self.callbacks.schedule_actionable_owner_dispatch.reset_mock()
        self.callbacks.schedule_actionable_owner_continuity_check.reset_mock()
        await self.progress(self.request())
        self.callbacks.schedule_actionable_owner_dispatch.assert_not_called()
        self.callbacks.schedule_actionable_owner_continuity_check.assert_not_called()

    async def test_pending_handoff_retains_ack_and_never_offers_wait_request(self):
        self.lane(handoff=WorkItemHandoff(from_agent='sally', to_agent='carl', requested_at=300),
            next_owner='carl')
        before = self.state()
        message = self.prompt()
        self.assertTrue(message.startswith('carl: structured handoff pending'))
        self.assertIn('/ack', message)
        self.assertIn('acknowledgement path first', message)
        self.assertNotIn('wait_checkpoint={', message)
        with self.assertRaises(HTTPException):
            await self.progress(self.request())
        self.assertEqual(self.state(), before)

    async def test_material_note_only_progress_still_wakes_active_owner(self):
        self.lane(current_stage='implementation_active', blocker=None)
        await self.progress(WorkItemProgressUpdate(actor='sally', note='New actual dependency Artifact is now available'))
        self.callbacks.schedule_actionable_owner_dispatch.assert_called_once()
        self.callbacks.schedule_actionable_owner_continuity_check.assert_called_once()
        self.assertIsNone(self.state().wait_checkpoint)
        self.assertIn('including a note-only update', self.prompt())

    async def test_ci_wait_does_not_invent_failed_lane_or_blocker(self):
        self.lane(current_stage='validation_running', blocker=None,
            next_action='Observe the pending exact-head pipeline on its terminal provider event')
        before = self.state()
        message = self.prompt()
        self.assertIn('Running, pending, created or queued exact-head CI is not a defect', message)
        self.assertIn('Retain the responsible owner and validation stage', message)
        self.assertIn('Never invent a blocker', message)
        self.assertNotIn('wait_checkpoint={', message)
        self.assertEqual(self.state(), before)

    async def test_state_projection_and_active_witness_use_same_preconditions(self):
        from codex_web.work_item_wait import wait_context, current_unchanged_wait, UnchangedWaitCheckpoint, wait_input_sha256
        cases=[({'blocker':None},'missing_blocker'), ({'next_action':None},'missing_next_action'),
            ({'current_stage':'implementation_active'},'active_lane'), ({'current_stage':'closed'},'closed'),
            ({'handoff':WorkItemHandoff(from_agent='sally',to_agent='carl',requested_at=1)},'pending_handoff')]
        original=self.lane()
        for changes,reason in cases:
            with self.subTest(reason=reason):
                state=original.model_copy(update=changes)
                state.wait_checkpoint=UnchangedWaitCheckpoint(expected_input_sha256=wait_input_sha256(state),
                    actor='sally',acknowledged_at=100)
                self.assertFalse(wait_context(state)['state_eligible'])
                self.assertEqual(wait_context(state)['state_ineligibility_reason'],reason)
                self.assertFalse(current_unchanged_wait(state))
