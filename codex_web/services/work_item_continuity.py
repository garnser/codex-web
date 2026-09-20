from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException

from codex_web.models import WorkItemState
from codex_web.services.keyed_tasks import KeyedTaskCoordinator
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
        self._require().schedule_structured_handoff_dispatch(
            state,
            source=source,
        )

    def schedule_handoff_continuity_check(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        self._require().schedule_handoff_continuity_check(
            state,
            source=source,
        )

    def schedule_actionable_owner_dispatch(
        self,
        state: WorkItemState,
        *,
        source: str,
        actor: str | None = None,
    ) -> None:
        self._require().schedule_actionable_owner_dispatch(
            state,
            source=source,
            actor=actor,
        )

    def schedule_actionable_owner_continuity_check(
        self,
        state: WorkItemState,
        *,
        source: str,
    ) -> None:
        self._require().schedule_actionable_owner_continuity_check(
            state,
            source=source,
        )

    def dispatch_text(self, state: WorkItemState) -> str:
        return self._require().dispatch_text(state)

    async def stop(self) -> None:
        if self.service is not None:
            await self.service.stop()


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
        try:
            global_limit = int(
                os.environ.get(
                    "CODEX_WEB_CONTINUITY_DISPATCH_CONCURRENCY"
                )
                or "16"
            )
        except ValueError:
            global_limit = 16
        try:
            project_limit = int(
                os.environ.get(
                    "CODEX_WEB_CONTINUITY_DISPATCH_PROJECT_CONCURRENCY"
                )
                or "4"
            )
        except ValueError:
            project_limit = 4
        try:
            timeout_seconds = float(
                os.environ.get(
                    "CODEX_WEB_CONTINUITY_DISPATCH_TIMEOUT_SECONDS"
                )
                or "60"
            )
        except ValueError:
            timeout_seconds = 60.0
        self.dispatch_coordinator = KeyedTaskCoordinator(
            name="work-item-continuity",
            max_concurrency=max(1, min(global_limit, 64)),
            per_group_limit=max(1, min(project_limit, 32)),
            timeout_seconds=max(1.0, min(timeout_seconds, 600.0)),
            event_sink=append_event,
        )

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
        expected_recipient: str | None = None,
        expected_requested_at: float | None = None,
        expected_updated_at: float | None = None,
    ) -> None:
        handoff = state.handoff
        if not state.project_id or not handoff or handoff.status != "pending":
            return
        recipient = self.coerce_owner(handoff.to_agent)
        if not recipient:
            return
        if expected_recipient and recipient != expected_recipient:
            return
        if (
            expected_requested_at is not None
            and handoff.requested_at != expected_requested_at
        ):
            return
        if (
            expected_updated_at is not None
            and state.updated_at != expected_updated_at
        ):
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
        if expected_updated_at is not None:
            try:
                latest = self.get_state(state.ref)
            except HTTPException:
                return
            latest_handoff = latest.handoff
            latest_recipient = self.coerce_owner(
                latest_handoff.to_agent if latest_handoff else None
            )
            if (
                latest.updated_at != expected_updated_at
                or not latest_handoff
                or latest_handoff.status != "pending"
                or (
                    expected_recipient
                    and latest_recipient != expected_recipient
                )
                or (
                    expected_requested_at is not None
                    and latest_handoff.requested_at
                    != expected_requested_at
                )
            ):
                self.append_event(
                    {
                        "type": "work_item_handoff_dispatch_superseded",
                        "ref": state.ref,
                        "source": source,
                    }
                )
                return
            state = latest
            handoff = latest_handoff
            recipient = latest_recipient
            assert recipient is not None

        dispatch_key = (
            f"handoff-dispatch:{state.ref}:{binding.thread_id}:"
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
        handoff = state.handoff
        if (
            not state.project_id
            or not handoff
            or handoff.status != "pending"
        ):
            return
        recipient = self.coerce_owner(handoff.to_agent)
        if not recipient:
            return
        snapshot = state.model_copy(deep=True)
        expected_updated_at = state.updated_at
        expected_requested_at = handoff.requested_at

        async def run() -> None:
            await self.dispatch_structured_handoff(
                snapshot,
                source=source,
                expected_recipient=recipient,
                expected_requested_at=expected_requested_at,
                expected_updated_at=expected_updated_at,
            )

        self.dispatch_coordinator.submit(
            f"handoff:{state.ref}",
            run,
            group=state.project_id,
            metadata={
                "operation": "handoff",
                "ref": state.ref,
                "source": source,
            },
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
        expected_owner: str | None = None,
        expected_stage: str | None = None,
        expected_updated_at: float | None = None,
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
        if expected_owner and owner != expected_owner:
            return
        if expected_stage and state.current_stage != expected_stage:
            return
        if (
            expected_updated_at is not None
            and state.updated_at != expected_updated_at
        ):
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

        if expected_updated_at is not None:
            try:
                latest = self.get_state(state.ref)
            except HTTPException:
                return
            latest_owner = self.coerce_owner(
                latest.current_owner or latest.next_owner
            )
            if (
                latest.updated_at != expected_updated_at
                or latest_owner != expected_owner
                or latest.current_stage != expected_stage
                or latest.closed_at
                or (
                    latest.handoff
                    and latest.handoff.status == "pending"
                )
            ):
                self.append_event(
                    {
                        "type": "work_item_owner_dispatch_superseded",
                        "ref": state.ref,
                        "source": source,
                    }
                )
                return
            state = latest
            owner = latest_owner
            assert owner is not None

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
        owner = self.coerce_owner(state.current_owner or state.next_owner)
        if not owner:
            return
        snapshot = state.model_copy(deep=True)
        expected_stage = state.current_stage
        expected_updated_at = state.updated_at

        async def run() -> None:
            await self.dispatch_actionable_owner(
                snapshot,
                source=source,
                actor=actor,
                expected_owner=owner,
                expected_stage=expected_stage,
                expected_updated_at=expected_updated_at,
            )

        self.dispatch_coordinator.submit(
            f"owner:{state.ref}",
            run,
            group=state.project_id,
            metadata={
                "operation": "owner",
                "ref": state.ref,
                "source": source,
            },
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
            expected_owner=current_owner,
            expected_stage=state.current_stage,
            expected_updated_at=state.updated_at,
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

    def status(self) -> dict[str, Any]:
        return {
            "immediateDispatch": self.dispatch_coordinator.status(),
            "delayedActionableOwnerChecks": sum(
                1
                for task in self.actionable_owner_tasks.values()
                if not task.done()
            ),
            "delayedHandoffChecks": sum(
                1
                for task in self.handoff_tasks.values()
                if not task.done()
            ),
        }

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
