from __future__ import annotations
import asyncio
import contextvars
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from codex_web.models import WorkItemState
from codex_web.services.bot_delivery import BotDeliveryService
from codex_web.services.workflow_claims import WorkflowClaimPolicy

scope = contextvars.ContextVar("workflow_claim_test_scope", default=None)

def policy(states, loader=None):
    return WorkflowClaimPolicy(load_states=loader or (lambda: states),
        ensure_defaults=lambda state: state, coerce_owner=lambda value: value,
        owner_names=("james", "quinn"))

class MentionTests(unittest.TestCase):
    def test_no_issue_mentions_never_load_catalog(self):
        load = Mock(side_effect=AssertionError("unrelated catalog read"))
        p = policy({}, load)
        self.assertEqual(p.findings("I will inspect MR !288; current_owner=quinn"), ([], []))
        load.assert_not_called()

    def test_qualified_and_unique_bare_refs_preserve_findings_and_deduplicate(self):
        state = WorkItemState(ref="group/app#438", current_owner="james", created_at=1, updated_at=1, last_meaningful_update_at=1)
        p = policy({state.ref: state})
        findings, mentions = p.findings("group/app#438 and #438 current_owner=quinn")
        self.assertEqual(mentions, [state])
        self.assertEqual(findings, ["owner claim mismatch for group/app#438: claimed=quinn canonical=james"])

    def test_ambiguous_bare_ref_never_selects_arbitrary_project(self):
        states = {ref: WorkItemState(ref=ref, current_owner="james", created_at=1, updated_at=1, last_meaningful_update_at=1)
                  for ref in ("group/app#438", "other/app#438")}
        self.assertEqual(policy(states).findings("#438 current_owner=quinn"), ([], []))

    def test_missing_qualified_ref_still_runs_existing_canonical_lookup(self):
        load = Mock(return_value={})
        self.assertEqual(policy({}, load).findings("group/app#438 current_owner=quinn"), ([], []))
        load.assert_called_once_with()

class OutboundClaimsAsyncTests(unittest.IsolatedAsyncioTestCase):
    def service(self, findings, sequence):
        binding = SimpleNamespace(thread_id="t", project_id="p", provider="slack",
            external_conversation_id="c", thread_name="Quinn", route_prefix="quinn")
        async def send(binding, text):
            sequence.append(("send", text));return {"sent": True}
        async def publish(event): sequence.append(("publish", event))
        s = BotDeliveryService(
            bindings=SimpleNamespace(for_thread=lambda _: [binding]),
            targets=SimpleNamespace(outbound_bindings_for_thread=lambda _, bs: bs,
                remember_delivery_target=lambda *_: sequence.append(("remember", None))),
            presentation=SimpleNamespace(format_detail_item=lambda _: None,
                binding_prefix=lambda _: "quinn", binding_report_name=lambda _: "Quinn",
                format_outbound_item=lambda item, _: item["text"],
                truncate_text=lambda value, limit: value[:limit]),
            telemetry=SimpleNamespace(append=lambda event: sequence.append(("event", event))),
            workflow_claim_findings=findings, workflow_correction=WorkflowClaimPolicy.correction,
            thread_project_id=lambda _: "p", publish_event=publish)
        s.send_outbound = AsyncMock(side_effect=send)
        return s

    def message(self):
        return {"method": "item/completed", "params": {"threadId": "t",
            "item": {"type": "agentMessage", "text": "#438 current_owner=quinn"}}}

    async def test_blocked_claims_yield_preserve_context_correction_and_consumer_order(self):
        loop = asyncio.get_running_loop();entered = asyncio.Event();release = threading.Event()
        sequence = [];observed = []
        state = WorkItemState(ref="group/app#438", current_owner="james", created_at=1, updated_at=1, last_meaningful_update_at=1)
        def findings(text):
            observed.append((threading.get_ident(), scope.get(), text))
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3): raise TimeoutError("claim barrier")
            sequence.append(("findings", None))
            return policy({state.ref: state}).findings(text)
        service = self.service(findings, sequence)
        async def consume():
            await service.record_outbound(self.message())
            sequence.append(("next-notification", None))
        token = scope.set("workspace-a");task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(entered.wait(), 4)
            heartbeat = asyncio.Event();loop.call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), .5)
            self.assertFalse(task.done());service.send_outbound.assert_not_awaited()
            self.assertEqual(sequence, [])
            self.assertNotEqual(observed[0][0], threading.get_ident())
            self.assertEqual(observed[0][1], "workspace-a")
        finally:
            release.set();scope.reset(token);await task
        self.assertEqual([item[0] for item in sequence],
            ["findings", "send", "remember", "event", "publish", "next-notification"])
        sent = sequence[1][1]
        self.assertIn("withheld an agent update", sent)
        self.assertIn("canonical=james", sent)
        event = sequence[3][1]
        self.assertEqual(event["workflow_verification"]["refs"], [state.ref])
        self.assertEqual(event["workflow_verification"]["original_text"], "#438 current_owner=quinn")

    async def test_cancelled_claim_wait_never_sends_or_advances_notification(self):
        loop = asyncio.get_running_loop();entered = asyncio.Event();finished = asyncio.Event()
        release = threading.Event();calls = [];sequence = []
        def findings(text):
            calls.append(text);loop.call_soon_threadsafe(entered.set)
            release.wait(3);loop.call_soon_threadsafe(finished.set);return [], []
        service = self.service(findings, sequence)
        task = asyncio.create_task(service.record_outbound(self.message()))
        try:
            await asyncio.wait_for(entered.wait(), 4)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        finally:
            release.set();await asyncio.wait_for(finished.wait(), 4)
        service.send_outbound.assert_not_awaited()
        self.assertEqual(len(calls), 1);self.assertEqual(sequence, [])

    async def test_claim_read_failure_prevents_delivery_and_remains_visible(self):
        error = RuntimeError("canonical claims unavailable")
        def findings(_): raise error
        service = self.service(findings, [])
        with self.assertRaises(RuntimeError) as raised:
            await service.record_outbound(self.message())
        self.assertIs(raised.exception, error)
        service.send_outbound.assert_not_awaited()
