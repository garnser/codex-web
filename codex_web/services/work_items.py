from __future__ import annotations

import asyncio
import time
from typing import Any

from codex_web.models import WorkItemAckCreate, WorkItemHandoffCreate, WorkItemProgressUpdate


class WorkItemService:
    """Work-item API behavior with blocking persistence/GitLab work isolated.

    The compatibility runtime still owns the detailed state machine. This
    service owns the HTTP-facing orchestration and guarantees that synchronous
    GitLab requests and read/modify/write state operations do not run on the
    FastAPI event loop.
    """

    def __init__(self, host: Any) -> None:
        self.host = host

    async def list(
        self,
        *,
        project_id: str | None,
        owner: str | None,
        stage: str | None,
        release_gate: bool | None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._list_sync,
            project_id,
            owner,
            stage,
            release_gate,
        )

    def _list_sync(
        self,
        project_id: str | None,
        owner: str | None,
        stage: str | None,
        release_gate: bool | None,
    ) -> dict[str, Any]:
        states = list(self.host._load_work_item_states().values())
        if project_id:
            states = [state for state in states if state.project_id == project_id]
        if owner:
            normalized_owner = self.host._coerce_owner(owner)
            states = [
                state
                for state in states
                if self.host._coerce_owner(state.current_owner or state.next_owner) == normalized_owner
            ]
        if stage:
            normalized_stage = self.host._normalize_work_item_stage(stage, fallback="")
            states = [state for state in states if state.current_stage == normalized_stage]
        if release_gate is not None:
            states = [state for state in states if state.release_gate is release_gate]
        states.sort(key=lambda item: item.updated_at, reverse=True)
        return {
            "items": [self.host._work_item_state_public(state) for state in states],
            "count": len(states),
        }

    async def sync_from_gitlab(self) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(self.host._sync_work_item_states_from_gitlab)
        except Exception as exc:
            self.host.GITLAB_SYNC_CONSECUTIVE_FAILURES += 1
            self.host.GITLAB_SYNC_LAST_ERROR = self.host._truncate_text(str(exc), 500)
            self.host.GITLAB_SYNC_LAST_ERROR_AT = time.time()
            self.host._append_bot_event(
                {
                    "type": "gitlab_work_item_sync_failed",
                    "failure_count": self.host.GITLAB_SYNC_CONSECUTIVE_FAILURES,
                    "error": self.host.GITLAB_SYNC_LAST_ERROR,
                }
            )
            raise

        self.host.GITLAB_SYNC_CONSECUTIVE_FAILURES = 0
        self.host.GITLAB_SYNC_LAST_ERROR = None
        self.host.GITLAB_SYNC_LAST_SUCCESS_AT = time.time()
        await self.host.hub.publish({"type": "work-item.sync", **result})
        return {"ok": True, **result}

    async def get(self, ref: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_sync, ref)

    def _get_sync(self, ref: str) -> dict[str, Any]:
        return self.host._work_item_state_public(self.host._work_item_state(ref))

    async def handoff(self, ref: str, payload: WorkItemHandoffCreate) -> dict[str, Any]:
        # _structured_handoff can synchronously update GitLab labels.
        state = await asyncio.to_thread(self.host._structured_handoff, ref, payload)
        public = self.host._work_item_state_public(state)
        await self.host.hub.publish({"type": "work-item.handoff", "ref": ref, "state": public})
        self.host._schedule_structured_handoff_dispatch(state, source="work-item-handoff")
        self.host._schedule_handoff_continuity_check(state, source="work-item-handoff-continuity")
        return {"ok": True, "item": public}

    async def acknowledge(self, ref: str, payload: WorkItemAckCreate) -> dict[str, Any]:
        # _structured_ack can synchronously update GitLab labels.
        state = await asyncio.to_thread(self.host._structured_ack, ref, payload)
        public = self.host._work_item_state_public(state)
        await self.host.hub.publish({"type": "work-item.ack", "ref": ref, "state": public})
        self.host._schedule_actionable_owner_dispatch(state, source="work-item-ack", actor=payload.actor)
        self.host._schedule_actionable_owner_continuity_check(
            state,
            source="work-item-ack-continuity",
        )
        if self.host._work_item_split_brain_findings(state):
            self.host._schedule_native_recovery_cycles(reason="work-item-ack-routing-drift")
        return {"ok": True, "item": public}

    async def progress(self, ref: str, payload: WorkItemProgressUpdate) -> dict[str, Any]:
        # _structured_progress can synchronously update GitLab labels.
        state = await asyncio.to_thread(self.host._structured_progress, ref, payload)
        public = self.host._work_item_state_public(state)
        await self.host.hub.publish({"type": "work-item.progress", "ref": ref, "state": public})
        self.host._schedule_actionable_owner_dispatch(
            state,
            source="work-item-progress",
            actor=payload.actor,
        )
        self.host._schedule_actionable_owner_continuity_check(
            state,
            source="work-item-progress-continuity",
        )
        if self.host._work_item_split_brain_findings(state):
            self.host._schedule_native_recovery_cycles(reason="work-item-progress-routing-drift")
        return {"ok": True, "item": public}
