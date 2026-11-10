from __future__ import annotations

import asyncio
import os
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from codex_web.action_intents import ActionIntentClaimRequest, ActionIntentState, ActionIntentStatus
from codex_web.action_providers import ActionDefinition
from codex_web.services.capacity import CapacityService
from codex_web.services.action_intents import ActionIntentLeaseError
from codex_web.services.action_providers import ActionRequirementError
from codex_web.services.code_host_action_contract import CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_GUARDED_ACTION_ID
from codex_web.storage.capacity import CapacityStore
from tests import test_action_intents as fixture_module


class ActionTimeoutValuesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = fixture_module.ActionIntentTests(methodName="runTest")
        await self.f.asyncSetUp()

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    async def test_real_owned_verification_accepts_its_initial_and_later_canonical_claim_renewals(self):
        f = self.f
        intent = f._create(timeout_seconds=0)
        f.service.claim(ActionIntentClaimRequest(worker_id="worker", lease_seconds=30),
            actor=f.worker_actor, intent_id=intent.id)
        expected = f.service._mark_executing(intent.id, "worker", f.worker_actor)
        clock = [expected.lease.acquired_at + 1]
        entered, tick, renewed = (asyncio.Event() for _ in range(3))
        original_renew = f.service.renew_claim
        loop = asyncio.get_running_loop()
        renewals = []

        def renew(*args, **kwargs):
            value = original_renew(*args, **kwargs)
            renewals.append(value)
            if len(renewals) >= 2:
                loop.call_soon_threadsafe(renewed.set)
            return value

        async def controlled_wait(_duration):
            await tick.wait()
            tick.clear()

        async def verification():
            f.service._check_ready_intent(f.service.get(intent.id, f.actor), expected)
            entered.set()
            await renewed.wait()
            # The expected snapshot's old lease can expire while the SAME
            # acquisition generation remains canonically renewed and live.
            f.service._check_ready_intent(f.service.get(intent.id, f.actor), expected)
            return "verified"

        with patch("codex_web.services.action_intents.time.time", side_effect=lambda: clock[0]), \
                patch("codex_web.services.action_intents.asyncio.sleep", side_effect=controlled_wait), \
                patch.object(f.service, "renew_claim", side_effect=renew):
            task = asyncio.create_task(f.service._await_owned_action(verification, expected, f.worker_actor))
            entry_task = asyncio.create_task(entered.wait())
            try:
                done, _ = await asyncio.wait((task, entry_task),
                    timeout=30, return_when=asyncio.FIRST_COMPLETED)  # fixture deadlock guard
                if task in done:
                    await task  # surface the actual strict-verification failure
                self.assertTrue(entered.is_set())
                clock[0] = expected.lease.expires_at + 0.5
                tick.set()
                self.assertEqual(await asyncio.wait_for(asyncio.shield(task), 30), "verified")
            finally:
                entry_task.cancel()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, entry_task, return_exceptions=True)
        self.assertGreaterEqual(len(renewals), 2)

    async def test_readiness_renewal_proof_rejects_changed_generation_expiry_and_other_intent_fields(self):
        f = self.f
        intent = f._create(timeout_seconds=0)
        f.service.claim(ActionIntentClaimRequest(worker_id="worker", lease_seconds=30),
            actor=f.worker_actor, intent_id=intent.id)
        expected = f.service._mark_executing(intent.id, "worker", f.worker_actor)
        now = expected.lease.acquired_at + 1
        with patch("codex_web.services.action_intents.time.time", return_value=now):
            current = f.service.renew_claim(intent.id, "worker", 30, actor=f.worker_actor,
                                           expected_acquired_at=expected.lease.acquired_at)
            f.service._check_ready_intent(current, expected)
            for changes in ({"owner": "other"}, {"acquired_at": expected.lease.acquired_at + .5},
                            {"expires_at": now}, {"expires_at": expected.lease.expires_at - .5},
                            {"renewed_at": expected.lease.acquired_at - 1}, {"renewed_at": now + 1},
                            {"expires_at": float("inf")}):
                with self.subTest(changes=changes), self.assertRaises(ActionRequirementError):
                    f.service._check_ready_intent(current.model_copy(update={
                        "lease": current.lease.model_copy(update=changes)}), expected)
            for changes in ({"lease": None}, {"attempt": current.attempt + 1},
                            {"requested_by": "other"}, {"status": ActionIntentStatus.UNCERTAIN}):
                with self.subTest(changes=changes), self.assertRaises(ActionRequirementError):
                    f.service._check_ready_intent(current.model_copy(update=changes), expected)

    async def test_explicit_disabled_does_not_inherit_positive_definition(self):
        for value in (0, -1):
            intent = self.f._create(timeout_seconds=value,
                request=self.f._request(idempotency_key=f"duration-sentinel-{value}"))
            self.assertEqual(intent.timeout_seconds, value)
            state = ActionIntentState(intents=[intent])
            self.assertEqual(state.schema_version, "1.7")
            raw = state.model_dump(mode="json")
            self.assertEqual(self.f.service.store._decode(raw).model_dump(mode="json"), raw)
        positive = self.f._create(timeout_seconds=3,
            request=self.f._request(idempotency_key="duration-positive"))
        state = ActionIntentState(intents=[positive])
        self.assertEqual(state.model_dump(mode="json")["schema_version"], "1.6")
        raw = state.model_dump(mode="json")
        self.assertEqual(self.f.service.store._decode(raw).model_dump(mode="json"), raw)

    async def test_default_policy_override_and_explicit_positive_are_distinct(self):
        with patch.dict(os.environ, {"CODEX_WEB_ACTION_TIMEOUT_SECONDS": "0"}):
            default = self.f._create(request=self.f._request(idempotency_key="default-disabled"))
            positive = self.f._create(timeout_seconds=3,
                request=self.f._request(idempotency_key="explicit-positive"))
        self.assertEqual(default.timeout_seconds, 0)
        self.assertEqual(positive.timeout_seconds, 3)

    async def test_legacy_provider_cannot_advertise_disabled_definition(self):
        provider = self.f.reference
        self.assertEqual(provider.contract_version, "1.2")
        definition = provider.actions()[0]
        with patch.object(provider, "contract_version", "1.2"), patch.object(
                provider, "actions", return_value=(definition.model_copy(update={"timeout_seconds": 0}),)):
            with self.assertRaisesRegex(ValueError, "contract 1.3"):
                self.f.registry.register(provider)
        # Canonical model validation still rejects unsupported negatives.
        with self.assertRaises(ValueError):
            ActionDefinition(action_id="bad", title="bad", timeout_seconds=-2)

    async def test_explicit_sentinel_capable_provider_can_register_both_disabled_values(self):
        provider = self.f.reference
        definition = provider.actions()[0]
        for value in (0, -1):
            with self.subTest(value=value), patch.object(provider, "contract_version", "1.3"), patch.object(
                    provider, "actions", return_value=(definition.model_copy(update={"timeout_seconds": value}),)):
                self.f.registry.register(provider)
                self.assertIs(self.f.registry.provider(provider.provider_type, provider.provider_instance), provider)

    async def test_guarded_remaining_and_commit_preserve_real_deadline_when_elapsed_cap_off(self):
        f = self.f
        intent = f._create(timeout_seconds=0).model_copy(update={
            "action_id": CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_GUARDED_ACTION_ID})
        deadline = [None]
        f.service.procedure_execution = SimpleNamespace(
            readonly_operation_deadline=lambda *_: deadline[0], clock=lambda: 100)
        self.assertIsNone(f.service._guarded_readonly_remaining(intent, f.worker_actor))
        f.service._guarded_readonly_commit_guard(intent, f.worker_actor)["guard"]({})
        deadline[0] = 102
        self.assertEqual(f.service._guarded_readonly_remaining(intent, f.worker_actor), 2)
        deadline[0] = 99
        with self.assertRaises(TimeoutError):
            f.service._guarded_readonly_remaining(intent, f.worker_actor)

    async def test_renewal_refusal_cancels_and_joins_same_operation(self):
        f = self.f
        intent = f._create(timeout_seconds=0)
        claimed = f.service.claim(ActionIntentClaimRequest(worker_id="worker", lease_seconds=30),
            actor=f.worker_actor, intent_id=intent.id)
        entered, finished, tick = (asyncio.Event() for _ in range(3))
        calls = []

        async def operation():
            calls.append(True)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        async def controlled_wait(_duration):
            await tick.wait()

        def change_owner(state):
            state.intents = [item.model_copy(update={"lease": item.lease.model_copy(update={"owner": "other"})})
                             if item.id == intent.id else item for item in state.intents]
            return state

        with patch("codex_web.services.action_intents.asyncio.sleep", side_effect=controlled_wait):
            task = asyncio.create_task(f.service._await_owned_action(operation, claimed, f.worker_actor))
            try:
                await asyncio.wait_for(entered.wait(), 30)  # fixture deadlock guard, not response SLA
                f.service.store.update(change_owner, intent_ids=(intent.id,))
                tick.set()
                with self.assertRaises(ActionIntentLeaseError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(finished.is_set())
        self.assertEqual(calls, [True])

    async def test_disabled_execution_renews_finite_claim_and_capacity_until_same_provider_finishes(self):
        f = self.f
        clock = [time.time()]
        f.service.capacity = CapacityService(CapacityStore(f.sqlite), clock=lambda: clock[0])
        entered, release, tick, sleeping, renewed = (asyncio.Event() for _ in range(5))
        original_execute = f.reference.execute
        original_renew = f.service.renew_claim
        original_capacity_renew = f.service.capacity.renew
        loop = asyncio.get_running_loop()
        renewals = []
        capacity_renewals = []

        async def execute(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_execute(*args, **kwargs)

        def renew(*args, **kwargs):
            value = original_renew(*args, **kwargs)
            renewals.append(value)
            return value

        def renew_capacity(*args, **kwargs):
            value = original_capacity_renew(*args, **kwargs)
            capacity_renewals.append(value)
            if len(capacity_renewals) >= 2:
                loop.call_soon_threadsafe(renewed.set)
            return value

        async def controlled_heartbeat_wait(_duration):
            sleeping.set()
            await tick.wait()
            tick.clear()

        with patch("codex_web.services.action_intents.time.time", side_effect=lambda: clock[0]), \
                patch("codex_web.services.action_intents.asyncio.sleep", side_effect=controlled_heartbeat_wait), \
                patch.object(f.reference, "execute", side_effect=execute), \
                patch.object(f.service, "renew_claim", side_effect=renew), \
                patch.object(f.service.capacity, "renew", side_effect=renew_capacity):
            intent = f._create(timeout_seconds=0)
            claimed = f.service.claim(ActionIntentClaimRequest(worker_id="worker", lease_seconds=10),
                actor=f.worker_actor, intent_id=intent.id)
            task = asyncio.create_task(f.service.execute_claimed(intent.id, "worker", actor=f.worker_actor))
            try:
                await asyncio.wait_for(entered.wait(), 30)  # fixture deadlock guard
                await asyncio.wait_for(sleeping.wait(), 30)
                clock[0] += 9
                tick.set()
                await asyncio.wait_for(renewed.wait(), 30)
                clock[0] += 2  # The original claim is now expired; renewed claim is live.
                current = f.service.get(intent.id, f.actor)
                self.assertGreater(current.lease.expires_at, clock[0])
                self.assertGreater(clock[0], claimed.lease.expires_at)
                self.assertEqual(current.lease.acquired_at, claimed.lease.acquired_at)
                self.assertTrue(all(value.lease.expires_at - value.updated_at <= 10 for value in renewals))
                capacities = list(f.service.capacity.store.load().leases.values())
                self.assertEqual(len(capacities), 1)
                self.assertGreater(capacities[0].expires_at, capacities[0].acquired_at + 120)
                self.assertLess(capacities[0].expires_at, capacities[0].acquired_at + 200)
                self.assertFalse(task.done())
            finally:
                release.set()
                result = await asyncio.wait_for(asyncio.shield(task), 30)  # cleanup-only deadlock guard
        self.assertEqual(result.status, ActionIntentStatus.SUCCEEDED)
        self.assertEqual(f.reference.values["feature"], "enabled")
        self.assertFalse(any(task.get_name() in {f"action-renewal-{intent.id}", f"action-operation-{intent.id}"}
                             for task in asyncio.all_tasks() if not task.done()))

    async def test_disabled_execution_explicit_cancel_joins_original_and_releases_capacity(self):
        f = self.f
        f.service.capacity = CapacityService(CapacityStore(f.sqlite))
        entered, finished = asyncio.Event(), asyncio.Event()

        async def execute(*_args, **_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        with patch.object(f.reference, "execute", side_effect=execute):
            intent = f._create(timeout_seconds=-1)
            f.service.claim(ActionIntentClaimRequest(worker_id="worker", lease_seconds=30),
                actor=f.worker_actor, intent_id=intent.id)
            task = asyncio.create_task(f.service.execute_claimed(intent.id, "worker", actor=f.worker_actor))
            try:
                await asyncio.wait_for(entered.wait(), 30)  # fixture deadlock guard
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(finished.is_set())
        self.assertEqual(f.service.get(intent.id, f.actor).status, ActionIntentStatus.EXECUTING)
        self.assertFalse(f.service.capacity.store.load().leases)
        self.assertFalse(any(task.get_name() in {f"action-renewal-{intent.id}", f"action-operation-{intent.id}"}
                             for task in asyncio.all_tasks() if not task.done()))
