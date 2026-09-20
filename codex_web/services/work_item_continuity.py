from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from codex_web.models import WorkItemState
from codex_web.services.runtime_policy import RuntimePolicy


class WorkItemContinuityService:
    """Own handoff and actionable-owner dispatch/continuity scheduling."""

    def __init__(
        self,
        *,
        policy: RuntimePolicy,
        get_state: Callable[[str], WorkItemState],
        coerce_owner: Callable[[str | None], str | None],
        binding_for_agent: Callable[..., Any],
        replace_nonperforming_thread: Callable[[Any, str], Awaitable[Any]],
        dispatch_event: Callable[[Any, str, str], Awaitable[dict[str, Any]]],
        dispatch_text: Callable[[WorkItemState], str],
        append_event: Callable[[dict[str, Any]], None],
        truncate_text: Callable[[str, int], str],
        thread_is_active: Callable[[str], bool],
        thread_queue_depth: Callable[[str], int],
        thread_recently_active: Callable[[str], bool],
        watchdog_dispatch_allowed: Callable[[str], bool],
        record_watchdog_dispatch: Callable[[str], None],
        coordination_channel: str,
    ) -> None:
        self.policy = policy
        self.get_state = get_state
        self.coerce_owner = coerce_owner
        self.binding_for_agent = binding_for_agent
        self.replace_nonperforming_thread = replace_nonperforming_thread
        self.dispatch_event = dispatch_event
        self.dispatch_text = dispatch_text
        self.append_event = append_event
        self.truncate_text = truncate_text
        self.thread_is_active = thread_is_active
        self.thread_queue_depth = thread_queue_depth
        self.thread_recently_active = thread_recently_active
        self.watchdog_dispatch_allowed = watchdog_dispatch_allowed
        self.record_watchdog_dispatch = record_watchdog_dispatch
        self.coordination_channel = coordination_channel
        self.actionable_owner_tasks: dict[str, asyncio.Task[None]] = {}
        self.handoff_tasks: dict[str, asyncio.Task[None]] = {}

    @staticmethod
    def actionable_owner_stage(stage: str | None) -> bool:
        return stage in {
            "implementation_active",
            "failed_with_action_owner",
            "ready_for_validation",
            "validation_running",
            "ready_to_close",
        }

    def responsible_binding(self, state: WorkItemState) -> Any | None:
        owner = self.coerce_owner(state.current_owner or state.next_owner)
        if not owner or not state.project_id:
            return None
        return self.binding_for_agent(
            owner,
            state.project_id,
            preferred_conversation_id=self.coordination_channel,
        )

    async def dispatch_structured_handoff(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        handoff = state.handoff
        if not state.project_id or not handoff or handoff.status != "pending":
            return
        recipient = self.coerce_owner(handoff.to_agent)
        if not recipient:
            return
        binding = self.binding_for_agent(
            recipient,
            state.project_id,
            preferred_conversation_id=self.coordination_channel,
        )
        if not binding:
            self.append_event(
                {
                    "type": "work_item_handoff_dispatch_skipped",
                    "ref": state.ref,
                    "agent": recipient,
                    "reason": "no_binding",
                    "source": source,
                }
            )
            return
        binding = await self.replace_nonperforming_thread(binding, source)
        result = await self.dispatch_event(
            binding,
            self.dispatch_text(state),
            source,
        )
        self.append_event(
            {
                "type": "work_item_handoff_dispatched",
                "ref": state.ref,
                "thread_id": binding.thread_id,
                "agent": recipient,
                "source": source,
                "result": result,
            }
        )

    def schedule_structured_handoff_dispatch(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        async def run() -> None:
            try:
                await self.dispatch_structured_handoff(state, source=source)
            except Exception as exc:
                self.append_event(
                    {
                        "type": "work_item_handoff_dispatch_failed",
                        "ref": state.ref,
                        "source": source,
                        "error": self.truncate_text(
                            str(getattr(exc, "detail", exc)),
                            500,
                        ),
                    }
                )

        asyncio.create_task(run())

    async def run_handoff_continuity_check(
        self,
        ref: str,
        *,
        expected_recipient: str | None,
        expected_requested_at: float | None,
        source: str,
    ) -> None:
        delay = self.policy.handoff_continuity_delay()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            state = self.get_state(ref)
        except HTTPException:
            return
        handoff = state.handoff
        if not handoff or handoff.status != "pending":
            return
        recipient = self.coerce_owner(handoff.to_agent)
        if expected_recipient and recipient != expected_recipient:
            return
        if (
            expected_requested_at is not None
            and handoff.requested_at != expected_requested_at
        ):
            return
        if not state.project_id or not recipient:
            return
        binding = self.binding_for_agent(
            recipient,
            state.project_id,
            preferred_conversation_id=self.coordination_channel,
        )
        if not binding:
            return
        if (
            self.thread_is_active(binding.thread_id)
            or self.thread_queue_depth(binding.thread_id)
            or self.thread_recently_active(binding.thread_id)
        ):
            return
        binding = await self.replace_nonperforming_thread(binding, source)
        dispatch_key = (
            f"handoff-continuity:{ref}:{binding.thread_id}:"
            f"{recipient}:{handoff.requested_at}"
        )
        if not self.watchdog_dispatch_allowed(dispatch_key):
            return
        self.record_watchdog_dispatch(dispatch_key)
        result = await self.dispatch_event(
            binding,
            self.dispatch_text(state),
            source,
        )
        self.append_event(
            {
                "type": "work_item_handoff_continuity_dispatched",
                "ref": ref,
                "thread_id": binding.thread_id,
                "agent": recipient,
                "result": result,
            }
        )

    def schedule_handoff_continuity_check(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        handoff = state.handoff
        if not handoff or handoff.status != "pending":
            return
        recipient = self.coerce_owner(handoff.to_agent)
        if not recipient:
            return
        requested_at = handoff.requested_at
        existing = self.handoff_tasks.get(state.ref)
        if existing and not existing.done():
            existing.cancel()

        async def run() -> None:
            try:
                await self.run_handoff_continuity_check(
                    state.ref,
                    expected_recipient=recipient,
                    expected_requested_at=requested_at,
                    source=source,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.append_event(
                    {
                        "type": "handoff_continuity_check_failed",
                        "ref": state.ref,
                        "source": source,
                        "error": self.truncate_text(
                            str(getattr(exc, "detail", exc)),
                            500,
                        ),
                    }
                )
            finally:
                current = self.handoff_tasks.get(state.ref)
                if current is task:
                    self.handoff_tasks.pop(state.ref, None)

        task = asyncio.create_task(run())
        self.handoff_tasks[state.ref] = task

    async def dispatch_actionable_owner(
        self,
        state: WorkItemState,
        *,
        source: str,
        actor: str | None = None,
    ) -> None:
        if state.current_stage == "closed" or state.closed_at:
            return
        if not self.actionable_owner_stage(state.current_stage):
            return
        if state.handoff and state.handoff.status == "pending":
            return
        owner = self.coerce_owner(state.current_owner or state.next_owner)
        if not owner or self.coerce_owner(actor) == owner:
            return
        binding = self.responsible_binding(state)
        if not binding:
            return
        if (
            self.thread_is_active(binding.thread_id)
            or self.thread_queue_depth(binding.thread_id)
        ):
            return
        binding = await self.replace_nonperforming_thread(binding, source)
        dispatch_key = (
            f"work-item-owner-progress:{state.ref}:{binding.thread_id}:"
            f"{owner}:{state.current_stage}"
        )
        if not self.watchdog_dispatch_allowed(dispatch_key):
            return
        self.record_watchdog_dispatch(dispatch_key)
        result = await self.dispatch_event(
            binding,
            self.dispatch_text(state),
            source,
        )
        self.append_event(
            {
                "type": "work_item_owner_progress_dispatched",
                "ref": state.ref,
                "thread_id": binding.thread_id,
                "agent": owner,
                "current_stage": state.current_stage,
                "source": source,
                "actor": actor,
                "result": result,
            }
        )

    def schedule_actionable_owner_dispatch(
        self,
        state: WorkItemState,
        *,
        source: str,
        actor: str | None = None,
    ) -> None:
        async def run() -> None:
            try:
                await self.dispatch_actionable_owner(
                    state,
                    source=source,
                    actor=actor,
                )
            except Exception as exc:
                self.append_event(
                    {
                        "type": "work_item_owner_progress_dispatch_failed",
                        "ref": state.ref,
                        "source": source,
                        "actor": actor,
                        "error": self.truncate_text(
                            str(getattr(exc, "detail", exc)),
                            500,
                        ),
                    }
                )

        asyncio.create_task(run())

    async def run_actionable_owner_continuity_check(
        self,
        ref: str,
        *,
        expected_owner: str | None,
        expected_stage: str | None,
        source: str,
    ) -> None:
        delay = self.policy.actionable_owner_continuity_delay()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            state = self.get_state(ref)
        except HTTPException:
            return
        if state.current_stage == "closed" or state.closed_at:
            return
        if not self.actionable_owner_stage(state.current_stage):
            return
        current_owner = self.coerce_owner(
            state.current_owner or state.next_owner
        )
        if expected_owner and current_owner != expected_owner:
            return
        if expected_stage and state.current_stage != expected_stage:
            return
        await self.dispatch_actionable_owner(
            state,
            source=source,
            actor=None,
        )

    def schedule_actionable_owner_continuity_check(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        if state.current_stage == "closed" or state.closed_at:
            return
        if not self.actionable_owner_stage(state.current_stage):
            return
        owner = self.coerce_owner(state.current_owner or state.next_owner)
        if not owner:
            return
        existing = self.actionable_owner_tasks.get(state.ref)
        if existing and not existing.done():
            existing.cancel()

        async def run() -> None:
            try:
                await self.run_actionable_owner_continuity_check(
                    state.ref,
                    expected_owner=owner,
                    expected_stage=state.current_stage,
                    source=source,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.append_event(
                    {
                        "type": "actionable_owner_continuity_check_failed",
                        "ref": state.ref,
                        "source": source,
                        "error": self.truncate_text(
                            str(getattr(exc, "detail", exc)),
                            500,
                        ),
                    }
                )
            finally:
                current = self.actionable_owner_tasks.get(state.ref)
                if current is task:
                    self.actionable_owner_tasks.pop(state.ref, None)

        task = asyncio.create_task(run())
        self.actionable_owner_tasks[state.ref] = task

    async def stop(self) -> None:
        tasks = [
            *self.actionable_owner_tasks.values(),
            *self.handoff_tasks.values(),
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.actionable_owner_tasks.clear()
        self.handoff_tasks.clear()
