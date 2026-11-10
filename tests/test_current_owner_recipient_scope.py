"""Current-owner work uses the canonical recipient preparer without a handoff."""
import asyncio
import time
import unittest
from pathlib import Path

from pydantic import ValidationError

from codex_web.execution_subjects import ExecutionSubject
from codex_web.execution_workers import NativeCredentialCustody
from codex_web.owner_routing import OwnerRoutingError, OwnerRoutingPin
from codex_web.models import QueuedTurn
from codex_web.storage.turn_queue import TurnQueueRepository
from tests import test_owner_native_session_recipient as native


class CurrentOwnerRecipientScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.n = native.NativeRecipientScopeTests()
        self.n.setUp()
        self.addCleanup(self.n.doCleanups)
        self.n.prepare_fixture()
        self.f = self.n.fixture
        self.state = self.n.state
        self.pending = self.state.handoff.model_copy(deep=True)
        self.state.handoff = None
        self.state.current_owner = self.state.next_owner = 'orchestrator'
        self.state.current_stage = 'implementation_active'
        self.save()
        self.n.threads.create.side_effect = self.create

    def save(self):
        self.f.f.env.states.put(self.state.ref, self.state)

    async def create(self, **kwargs):
        result = await self.n.create(**kwargs)
        self.n.reserved.subject = ExecutionSubject(kind='thread_bootstrap',
            ref='bootstrap-' + kwargs['bootstrap_token'])
        return result

    def validate(self, pin):
        self.n.routing.validate_native(pin, thread_id=pin.recipient.thread_id,
            project=self.f.f.env.env.projects.get('project-a', scope=self.f.f.env.env.scope),
            work_item_ref=self.state.ref, handoff_requested_at=None,
            writable_repository_resource_ids=('platform',), read_only_repository_resource_ids=(),
            agent_profile_id=self.f.f.profile.profile_id,
            agent_profile_revision=self.f.f.profile.revision,
            agent_profile_actor_id=self.f.f.actor.identity_id)

    async def test_real_current_owner_dispatch_creates_reuses_exact_scope_without_handoff(self):
        # This is the actual old refusal: canonical native source is Core, Work
        # needs Platform, and no compatible live recipient exists.
        with self.assertRaisesRegex(OwnerRoutingError, 'native_scope_incompatible'):
            self.n.select()
        before = self.n.session.model_dump(mode='json')
        result = await self.f.continuity.dispatch_actionable_owner(self.state,
            source='operator-retry', actor='operator')
        self.assertTrue(result['dispatched'], result)
        self.f.fixture.dispatch.assert_awaited_once()
        pin = self.f.fixture.dispatch.call_args.kwargs['owner_routing_pin']
        self.assertEqual(pin.schema_version, '1.2')
        self.assertIsNotNone(pin.current_work_intent_sha256)
        self.assertIsNone(pin.handoff_requested_at)
        self.assertIsNone(self.f.f.env.states.get(self.state.ref).handoff)
        self.assertEqual(pin.original_thread_id, self.n.binding.thread_id)
        self.validate(pin)
        self.assertEqual(await self.n.prepare(), pin)
        self.n.threads.create.assert_awaited_once()
        self.assertEqual(self.n.store.get(self.n.session.id,
            organization_id='org-a', workspace_id='workspace-a').model_dump(mode='json'), before)
        self.assertEqual(self.f.f.binding.model_dump(mode='json'), self.f.binding_before)
        self.f.continuity.replace_nonperforming_thread.assert_not_called()

    async def test_unknown_reservation_does_not_rotate_after_retry_or_failure(self):
        tokens = []
        async def unknown(**kwargs):
            kwargs['admission_guard']()
            tokens.append(kwargs['bootstrap_token'])
            self.n.reserved = self.n.template.model_copy(deep=True, update={
                'execution_id': 'thread-bootstrap-' + kwargs['bootstrap_token']})
            raise TimeoutError('unknown provider outcome')
        self.n.threads.create.side_effect = unknown
        with self.assertRaises(TimeoutError):
            await self.n.prepare()
        self.state.execution.retry.attempt += 1
        self.save()
        with self.assertRaisesRegex(OwnerRoutingError, 'bootstrap_pending'):
            await self.n.prepare()
        self.assertEqual(len(tokens), 1)
        self.n.threads.create.assert_awaited_once()

    async def test_concurrent_current_owner_preparations_share_one_reservation(self):
        pins = await asyncio.gather(self.n.prepare(), self.n.prepare())
        self.assertEqual(pins[0], pins[1])
        self.n.threads.create.assert_awaited_once()

    async def test_changed_work_intent_at_actual_create_guard_refuses(self):
        for name in ('current_owner', 'next_owner', 'stage', 'retry', 'scope',
                     'pending_handoff', 'accepted_handoff', 'profile', 'definition'):
            with self.subTest(name=name):
                # Each case has a fresh canonical store and reservation owner.
                fixture = CurrentOwnerRecipientScopeTests()
                fixture.setUp()
                try:
                    # Bind the mutation to that fixture rather than this one.
                    if name == 'profile':
                        def mutate():
                            original = fixture.f.assignments['assignment-existing-thread']
                            original.agent_profile = original.agent_profile.model_copy(update={'profile_revision': 999})
                    elif name == 'definition':
                        mutate = fixture.f.f.env.env.publish
                    elif name == 'retry':
                        def mutate():
                            fixture.state.execution.retry.attempt = 1
                    else:
                        values = {'current_owner': 'carl', 'next_owner': 'carl',
                            'stage': 'ready_for_validation', 'scope': ['core'],
                            'pending_handoff': fixture.pending,
                            'accepted_handoff': fixture.pending.model_copy(update={'status': 'accepted'})}
                        fields = {'stage': 'current_stage', 'scope': 'resource_ids',
                            'pending_handoff': 'handoff', 'accepted_handoff': 'handoff'}
                        def mutate():
                            setattr(fixture.state, fields.get(name, name), values[name])
                    async def changed(**kwargs):
                        mutate()
                        fixture.save()
                        kwargs['admission_guard']()
                        self.fail('changed current-work intent reached provider creation')
                    fixture.n.threads.create.side_effect = changed
                    with self.assertRaises(OwnerRoutingError):
                        await fixture.n.prepare()
                    self.assertIsNone(fixture.n.reserved)
                finally:
                    fixture.doCleanups()

    async def test_retained_unknown_original_refuses_before_reservation(self):
        original = self.f.f.uncertain()
        self.n.routing.preflight.put(original)
        with self.assertRaisesRegex(OwnerRoutingError, 'native_outcome_unresolved'):
            await self.n.prepare()
        self.n.threads.create.assert_not_awaited()

    async def test_current_pin_rechecks_intent_after_queue_wait(self):
        pin = await self.n.prepare()
        self.state.next_owner = 'carl'
        self.save()
        with self.assertRaises(OwnerRoutingError):
            self.validate(pin)

    async def test_unrelated_bound_execution_cannot_substitute_reserved_recipient(self):
        pin = await self.n.prepare()
        self.n.bound.execution_id = 'thread-bootstrap-unrelated-work'
        with self.assertRaisesRegex(OwnerRoutingError, 'bootstrap_unverified'):
            self.validate(pin)

    async def test_initial_seal_uses_exact_current_work_proof_and_rejects_extra_refs(self):
        self.f.worker.service_identity_id = 'fixture-worker-identity'
        create = self.create
        async def pending(**kwargs):
            result = await create(**kwargs)
            assignment = self.n.reserved
            assignment.native_credential_custody = NativeCredentialCustody(
                requester_identity_id=assignment.created_by,
                worker_identity_id=self.f.worker.service_identity_id, fence=assignment.fence)
            return result
        self.n.threads.create.side_effect = pending
        pin = await self.n.prepare()
        assignment = self.n.reserved
        assignment.secret_refs = ('fixture-procedure-reference',)
        assignment.native_credential_custody = assignment.native_credential_custody.model_copy(
            update={'admitted_at': time.time(), 'procedure_secret_refs': assignment.secret_refs})
        self.assertNotEqual(pin.recipient.authority_checksum,
            self.n.routing._authority_checksum(assignment))
        self.validate(pin)
        assignment.secret_refs += ('unrecorded-reference',)
        with self.assertRaisesRegex(OwnerRoutingError, 'custody_changed'):
            self.validate(pin)

    async def test_own_original_exclusion_does_not_exclude_other_unknown(self):
        pin = await self.n.prepare()
        own = self.f.f.uncertain(pin=pin, item_id='current-work-own')
        self.n.routing.preflight.put(own)
        common = dict(thread_id=pin.recipient.thread_id,
            project=self.f.f.env.env.projects.get('project-a', scope=self.f.f.env.env.scope),
            work_item_ref=self.state.ref, handoff_requested_at=None,
            writable_repository_resource_ids=('platform',), read_only_repository_resource_ids=(),
            agent_profile_id=self.f.f.profile.profile_id,
            agent_profile_revision=self.f.f.profile.revision,
            agent_profile_actor_id=self.f.f.actor.identity_id,
            queued_admission_attempt_id=own.id)
        self.n.routing.validate_native(pin, **common)
        other = self.f.f.uncertain(item_id='other-current-work-unknown')
        self.n.routing.preflight.put(other)
        with self.assertRaisesRegex(OwnerRoutingError, 'native_outcome_unresolved'):
            self.n.routing.validate_native(pin, **common)

    async def test_pending_preparation_cannot_change_mode_when_handoff_removed(self):
        self.state.handoff = self.pending
        self.state.current_owner = 'carl'
        self.save()
        async def changed(**kwargs):
            self.state.handoff = None
            self.state.current_owner = 'orchestrator'
            self.save()
            kwargs['admission_guard']()
            self.fail('removed pending handoff became a new current-work invocation')
        self.n.threads.create.side_effect = changed
        with self.assertRaisesRegex(OwnerRoutingError, 'preparation_changed'):
            await self.n.prepare()
        self.assertIsNone(self.n.reserved)

    async def test_cancelled_unknown_create_retains_same_reservation(self):
        reserved = asyncio.Event()
        finish = asyncio.Event()
        async def held(**kwargs):
            kwargs['admission_guard']()
            self.n.reserved = self.n.template.model_copy(deep=True, update={
                'execution_id': 'thread-bootstrap-' + kwargs['bootstrap_token']})
            reserved.set()
            await finish.wait()
            self.fail('cancelled preparation continued provider work')
        self.n.threads.create.side_effect = held
        task = asyncio.create_task(self.n.prepare())
        await reserved.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.assertRaisesRegex(OwnerRoutingError, 'bootstrap_pending'):
            await self.n.prepare()
        self.n.threads.create.assert_awaited_once()

    async def test_pin_versions_preserve_old_shape_and_reject_unversioned_current_work(self):
        pin = await self.n.prepare()
        self.assertEqual(OwnerRoutingPin.model_validate(pin.model_dump(mode='json')), pin)
        path = Path(self.f.f.env.env.temp.name) / 'current-work-queue.json'
        queue = TurnQueueRepository(self.f.f.env.env.state, legacy_path=path)
        original = QueuedTurn(id='current-work-original', thread_id=pin.recipient.thread_id,
            project_id=self.state.project_id, message='Continue the current canonical Work',
            created_at=1, work_item_ref=self.state.ref, owner_routing_pin=pin)
        queue.save({original.thread_id: [original]})
        reopened = TurnQueueRepository(self.f.f.env.env.state, legacy_path=path)
        self.assertEqual(reopened.load()[original.thread_id][0], original)
        invalid = pin.model_dump(mode='json')
        invalid['schema_version'] = '1.1'
        with self.assertRaises(ValidationError):
            OwnerRoutingPin.model_validate(invalid)
        legacy = pin.model_copy(update={'schema_version': '1.1', 'current_work_intent_sha256': None})
        self.assertNotIn('current_work_intent_sha256', legacy.model_dump(mode='json'))
        self.assertEqual(OwnerRoutingPin.model_validate(legacy.model_dump(mode='json')), legacy)


if __name__ == '__main__':
    unittest.main()
