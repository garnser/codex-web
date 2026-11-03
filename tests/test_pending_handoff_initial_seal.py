"""Pending originals survive only the recorded initial credential-reference seal.

Private canonical Work/Profile/preflight owners and real recipient validation;
credential sealing is a typed observation, not secret/provider execution.
"""
import time
import unittest

from codex_web.execution_workers import NativeCredentialCustody, NetworkPolicy
from codex_web.owner_routing import OwnerRoutingError
from tests import test_owner_routing_recipients as fixtures


class PendingHandoffInitialSealTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = fixtures.CompatibleRecipientTests()
        self.env.setUp()
        self.addCleanup(self.env.doCleanups)
        self.service = self.env.service
        self.env.worker.service_identity_id = 'private-worker-identity'
        assignment = self.env.assignments['assignment-platform-thread']
        self.pending = assignment.model_copy(deep=True, update={
            'secret_refs': ('private-runtime-reference',),
            'native_credential_custody': NativeCredentialCustody(
                runtime_secret_ref='private-runtime-reference')})
        self.env.assignments[assignment.id] = self.pending
        self.pin = self.env.select()
        self.assertEqual(self.pin.handoff_requested_at, 1)
        self.original_pin = self.pin.model_dump(mode='json')
        self.original_work = self.env.f.env.states.get(self.env.state.ref).model_dump(mode='json')
        self.seal = self.pending.native_credential_custody.model_copy(update={
            'admitted_at': time.time(), 'fence': self.pending.fence,
            'requester_identity_id': self.pending.created_by,
            'worker_identity_id': self.env.worker.service_identity_id,
            'procedure_secret_refs': ('private-procedure-reference',)})
        self.sealed = self.pending.model_copy(deep=True, update={
            'secret_refs': ('private-runtime-reference', 'private-procedure-reference'),
            'native_credential_custody': self.seal})
        self.env.assignments[assignment.id] = self.sealed

    def context(self):
        return self.service.context_for_pin(self.pin, self.env.f.binding)

    def assert_original_preserved(self):
        self.assertEqual(self.pin.model_dump(mode='json'), self.original_pin)
        self.assertEqual(self.env.f.env.states.get(self.env.state.ref).model_dump(mode='json'),
                         self.original_work)

    def test_pending_original_validates_after_only_exact_recorded_initial_seal(self):
        self.assertNotEqual(self.service._authority_checksum(self.sealed),
                            self.pin.recipient.authority_checksum)
        reconstructed = self.sealed.model_copy(update={'secret_refs': self.pending.secret_refs})
        self.assertEqual(self.service._authority_checksum(reconstructed),
                         self.pin.recipient.authority_checksum)
        error, result = None, None
        try:
            result = self.context()
        except OwnerRoutingError as exc:
            error = exc
        self.assertIsNone(error, str(error))
        self.assertEqual(result, (self.env.f.profile, self.env.f.actor))
        self.env.validate(self.pin)
        self.assert_original_preserved()
        self.env.fixture.dispatch.assert_not_called()

    async def test_original_pin_reaches_real_queued_start_capture_before_delivery_and_ack(self):
        # Borrow the existing real capture/receipt/delivery/ACK regression with
        # this retained PRE-seal pin, never a newly selected post-seal witness.
        self.env.select = lambda: self.pin
        await self.env.test_retained_native_capture_observer_projects_selected_lane_then_allows_ack()
        self.assertEqual(self.pin.model_dump(mode='json'), self.original_pin)

    def test_unrecorded_extra_or_missing_secret_reference_still_refuses(self):
        for refs in ((*self.sealed.secret_refs, 'private-unrecorded-reference'),
                     ('private-procedure-reference',)):
            with self.subTest(refs=refs):
                self.env.assignments[self.sealed.id] = self.sealed.model_copy(update={'secret_refs': refs})
                with self.assertRaisesRegex(OwnerRoutingError, 'custody_changed'):
                    self.context()
        self.assert_original_preserved()

    def test_unadmitted_or_foreign_seal_identity_or_fence_still_refuses(self):
        for change in ({'admitted_at': None}, {'worker_identity_id': 'foreign-worker'},
                       {'requester_identity_id': 'foreign-requester'}, {'fence': 2}):
            with self.subTest(change=change):
                self.env.assignments[self.sealed.id] = self.sealed.model_copy(update={
                    'native_credential_custody': self.seal.model_copy(update=change)})
                with self.assertRaisesRegex(OwnerRoutingError, 'custody_changed'):
                    self.context()
        self.assert_original_preserved()

    def test_non_secret_authority_or_assignment_drift_still_refuses(self):
        for change in (
            {'network': NetworkPolicy(enabled=True, allowed_hosts=('example.invalid',))},
            {'fence': self.sealed.fence + 1},
            {'execution_workspace_id': 'foreign-workspace'},
            {'repository_scope': self.sealed.repository_scope.model_copy(update={
                'read_only_repository_ids': ('foreign-resource',)})},
            {'agent_profile': self.sealed.agent_profile.model_copy(update={'profile_revision': 99})},
        ):
            with self.subTest(fields=tuple(change)):
                self.env.assignments[self.sealed.id] = self.sealed.model_copy(deep=True, update=change)
                with self.assertRaises(OwnerRoutingError):
                    self.env.validate(self.pin)
        self.assert_original_preserved()

    def test_changed_work_owner_or_nonce_still_refuses(self):
        for change in ({'implementation_owner': 'foreign-owner'},
                       {'handoff': self.env.state.handoff.model_copy(update={'requested_at': 2})}):
            with self.subTest(fields=tuple(change)):
                changed = self.env.state.model_copy(deep=True, update=change)
                self.env.f.env.states.put(changed.ref, changed)
                with self.assertRaises(OwnerRoutingError):
                    self.env.validate(self.pin)
        self.env.f.env.states.put(self.env.state.ref, self.env.state)
        self.assert_original_preserved()

    def test_unknown_original_still_blocks_without_replay_or_delivery(self):
        unknown = self.env.f.uncertain()
        self.env.f.preflight.put(unknown)
        with self.assertRaisesRegex(OwnerRoutingError, 'native_outcome_unresolved'):
            self.env.validate(self.pin)
        self.assertEqual(self.env.f.preflight.get(unknown.id), unknown)
        self.env.fixture.dispatch.assert_not_called()
        self.assert_original_preserved()

    def test_current_routing_definition_drift_still_refuses(self):
        self.env.f.env.env.publish()
        with self.assertRaisesRegex(OwnerRoutingError, 'definition_changed'):
            self.env.validate(self.pin)
        self.assert_original_preserved()
