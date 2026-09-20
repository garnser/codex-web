from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException

from codex_web.models import WorkItemState
from codex_web.services.keyed_background_tasks import KeyedTaskCoordinator
from codex_web.services.runtime_policy import RuntimePolicy


class DeferredWorkItemContinuityService:
    """Stable early reference that binds to the composed continuity owner later."""

    def __init__(self) -> None:
        self.service: WorkItemContinuityService | None = None

    def bind(self, service: "WorkItemContinuityService") -> None:
        self.service = service

    def _require(self) -> "WorkItemContinuityService":
        if self.service is None:
            raise RuntimeError("work-item continuity is not composed yet")
        return self.service

    def responsible_binding(self, state: WorkItemState) -> Any | None:
        return self._require().responsible_binding(state)

    def schedule_structured_handoff_dispatch(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        handoff = state.handoff
        if (
            not state.project_id
            or not handoff
            or handoff.status != "pending"
        ):
            return
        expected_recipient = self.coerce_owner(handoff.to_agent)
        if not expected_recipient:
            return
        expected_requested_at = handoff.requested_at
        ref = state.ref

        async def run() -> None:
            try:
                latest = self.get_state(ref)
            except HTTPException:
                return
            latest_handoff = latest.handoff
            if (
                not latest_handoff
                or latest_handoff.status != "pending"
                or self.coerce_owner(latest_handoff.to_agent)
                != expected_recipient
                or latest_handoff.requested_at
                != expected_requested_at
            ):
                return
            try:
                await self.dispatch_structured_handoff(
                    latest,
                    source=source,
                )
            except Exception as exc:
                self.append_event(
                    {
                        "type": "work_item_handoff_dispatch_failed",
                        "ref": ref,
                        "source": source,
                        "error": self.truncate_text(
                            str(getattr(exc, "detail", exc)),
                            500,
                        ),
                    }
                )

        self.dispatch_coordinator.schedule(
            f"handoff-dispatch:{ref}",
            run,
            revision=self._handoff_revision(state),
            scope=state.project_id,
            timeout_seconds=(
                self.policy.continuity_dispatch_timeout()
            ),
        )

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
        try:
            latest = self.get_state(state.ref)
        except HTTPException:
            return
        latest_owner = self.coerce_owner(
            latest.current_owner or latest.next_owner
        )
        if (
            latest.current_stage == "closed"
            or latest.closed_at
            or not self.actionable_owner_stage(latest.current_stage)
            or (latest.handoff and latest.handoff.status == "pending")
            or latest_owner != owner
            or latest.current_stage != state.current_stage
        ):
            return
        state = latest
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
        if (
            not state.project_id
            or state.current_stage == "closed"
            or state.closed_at
            or not self.actionable_owner_stage(state.current_stage)
        ):
            return
        if state.handoff and state.handoff.status == "pending":
            return
        expected_owner = self.coerce_owner(
            state.current_owner or state.next_owner
        )
        if not expected_owner:
            return
        expected_stage = state.current_stage
        ref = state.ref

        async def run() -> None:
            try:
                latest = self.get_state(ref)
            except HTTPException:
                return
            current_owner = self.coerce_owner(
                latest.current_owner or latest.next_owner
            )
            if (
                latest.current_stage == "closed"
                or latest.closed_at
                or not self.actionable_owner_stage(
                    latest.current_stage
                )
                or (
                    latest.handoff
                    and latest.handoff.status == "pending"
                )
                or current_owner != expected_owner
                or latest.current_stage != expected_stage
            ):
                return
            try:
                await self.dispatch_actionable_owner(
                    latest,
                    source=source,
                    actor=actor,
                )
            except Exception as exc:
                self.append_event(
                    {
                        "type": (
                            "work_item_owner_progress_dispatch_failed"
                        ),
                        "ref": ref,
                        "source": source,
                        "actor": actor,
                        "error": self.truncate_text(
                            str(getattr(exc, "detail", exc)),
                            500,
                        ),
                    }
                )

        self.dispatch_coordinator.schedule(
            f"owner-dispatch:{ref}",
            run,
            revision=self._owner_revision(state),
            scope=state.project_id,
            timeout_seconds=(
                self.policy.continuity_dispatch_timeout()
            ),
        )

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
        await self.dispatch_coordinator.stop()
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


def build_work_item_continuity_compatibility_service(
    host: Any,
) -> WorkItemContinuityService:
    """Build a dynamic adapter for historical direct-call entrypoints.

    Every collaborator resolves through the host at call time so supported
    tests/integrations can patch one historical seam. Canonical application
    paths use the explicitly composed WorkItemContinuityService instead.
    """

    policy = SimpleNamespace(
        actionable_owner_continuity_delay=lambda: host._actionable_owner_continuity_delay_seconds(),
        handoff_continuity_delay=lambda: host._handoff_continuity_delay_seconds(),
    )
    return WorkItemContinuityService(
        policy=policy,
        get_state=lambda ref: host._work_item_state(ref),
        coerce_owner=lambda owner: host._coerce_owner(owner),
        binding_for_agent=lambda *args, **kwargs: host._binding_for_agent(
            *args,
            **kwargs,
        ),
        replace_nonperforming_thread=(
            lambda binding, reason: host._replace_nonperforming_thread_if_needed(
                binding,
                reason,
            )
        ),
        dispatch_event=lambda binding, text, source: host._dispatch_event_to_binding(
            binding,
            text,
            source,
        ),
        dispatch_text=lambda state: host._work_item_dispatch_text(state),
        append_event=lambda event: host._append_bot_event(event),
        truncate_text=lambda value, limit: host._truncate_text(value, limit),
        thread_is_active=lambda thread_id: host._thread_is_active(thread_id),
        thread_queue_depth=lambda thread_id: host._thread_queue_depth(thread_id),
        thread_recently_active=lambda thread_id: host._thread_recently_active(
            thread_id
        ),
        watchdog_dispatch_allowed=lambda key: host._watchdog_dispatch_allowed(
            key
        ),
        record_watchdog_dispatch=lambda key: host._record_watchdog_dispatch(
            key
        ),
        coordination_channel=host.HANDOFF_COORDINATION_CHANNEL,
    )
