from __future__ import annotations

import asyncio
import contextvars
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from codex_web.models import BotBinding, WorkItemState
from codex_web.services.bot_delivery import BotDeliveryService
from codex_web.services.workflow_claims import WorkflowClaimPolicy


def _state(ref: str, *, owner: str = "james") -> WorkItemState:
    return WorkItemState(
        ref=ref,
        project_id="project-a",
        current_owner=owner,
        current_stage="implementation_active",
        last_meaningful_update_at=1.0,
        updated_at=1.0,
        created_at=1.0,
    )


class WorkflowClaimReferenceTests(unittest.TestCase):
    def _policy(self, states, load_states=None):
        return WorkflowClaimPolicy(
            load_states=load_states or (lambda: states),
            ensure_defaults=lambda state: state,
            coerce_owner=lambda owner: owner.casefold() if owner else None,
            owner_names=("james", "carl"),
        )

    def test_text_without_issue_references_performs_no_state_io(self):
        load_states = Mock(
            side_effect=AssertionError("unreferenced message loaded state")
        )
        policy = self._policy({}, load_states=load_states)

        self.assertEqual(
            policy.findings("Completed the requested analysis without a claim."),
            ([], []),
        )
        load_states.assert_not_called()

    def test_qualified_and_globally_unique_bare_references_preserve_matching(self):
        states = {
            "group/app#41": _state("group/app#41"),
            "other/app#41": _state("other/app#41", owner="carl"),
            "group/app#42": _state("group/app#42"),
        }
        policy = self._policy(states)

        self.assertEqual(
            [item.ref for item in policy.mentioned_states("group/app#41")],
            ["group/app#41"],
        )
        self.assertEqual(policy.mentioned_states("ambiguous #41"), [])
        self.assertEqual(
            [item.ref for item in policy.mentioned_states("unique #42")],
            ["group/app#42"],
        )

    def test_exact_claim_mismatch_and_correction_behavior_is_preserved(self):
        state = _state("group/app#42")
        policy = self._policy({state.ref: state})

        findings, mentioned = policy.findings(
            "group/app#42 is owned by Carl; current_stage=validation_running"
        )

        self.assertEqual([item.ref for item in mentioned], [state.ref])
        self.assertEqual(len(findings), 2)
        correction = policy.correction("James", mentioned, findings)
        self.assertIn("withheld an agent update", correction)
        self.assertIn("current_owner=james", correction)
        self.assertIn("current_stage=implementation_active", correction)


class BotDeliveryWorkflowClaimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.binding = BotBinding(
            id="binding-a",
            provider="slack",
            external_conversation_id="channel-a",
            thread_id="thread-a",
            project_id="project-a",
            created_at=1.0,
            updated_at=1.0,
        )
        self.targets = SimpleNamespace(
            outbound_bindings_for_thread=lambda _thread_id, bindings: bindings,
            remember_delivery_target=Mock(),
        )
        self.presentation = SimpleNamespace(
            binding_prefix=lambda _binding: "James",
            binding_report_name=lambda _binding: "James",
            format_outbound_item=lambda item, _name: item.get("text"),
            truncate_text=lambda text, limit: text[:limit],
            format_detail_item=lambda _item: None,
        )
        self.message = {
            "method": "item/completed",
            "params": {
                "threadId": "thread-a",
                "item": {
                    "type": "agentMessage",
                    "text": "group/app#42 is owned by Carl",
                },
            },
        }

    def _service(self, findings):
        service = BotDeliveryService(
            bindings=SimpleNamespace(for_thread=lambda _thread_id: [self.binding]),
            targets=self.targets,
            presentation=self.presentation,
            telemetry=SimpleNamespace(append=Mock()),
            collaboration=SimpleNamespace(),
            publish_event=AsyncMock(),
            workflow_claim_findings=findings,
            workflow_correction=lambda _name, _states, _findings: "corrected",
            thread_project_id=lambda _thread_id: "project-a",
        )
        service.send_outbound = AsyncMock(return_value={"sent": True})
        return service

    async def test_blocked_evaluation_keeps_loop_responsive_and_orders_send(self):
        entered = threading.Event()
        release = threading.Event()
        marker = contextvars.ContextVar("workflow-claim-marker")
        observed = []

        def findings(_text):
            observed.append(marker.get())
            entered.set()
            release.wait(2)
            return [], []

        service = self._service(findings)
        token = marker.set("request-context")
        try:
            task = asyncio.create_task(service.record_outbound(self.message))
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            await asyncio.wait_for(asyncio.sleep(0), 0.05)
            service.send_outbound.assert_not_awaited()
            release.set()
            await asyncio.wait_for(task, 1)
        finally:
            marker.reset(token)
            release.set()

        self.assertEqual(observed, ["request-context"])
        service.send_outbound.assert_awaited_once_with(
            self.binding,
            "group/app#42 is owned by Carl",
        )

    async def test_cancellation_while_evaluating_never_sends(self):
        entered = threading.Event()
        release = threading.Event()

        def findings(_text):
            entered.set()
            release.wait(2)
            return [], []

        service = self._service(findings)
        task = asyncio.create_task(service.record_outbound(self.message))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0)
        service.send_outbound.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
