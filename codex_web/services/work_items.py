from __future__ import annotations

import time
from typing import Any

from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.models import WorkItemAckCreate, WorkItemHandoffCreate, WorkItemProgressUpdate
from codex_web.services.work_item_state import WorkItemStateMachine


class WorkItemService:
    """Work-item API behavior backed by the canonical extracted state machine."""

    def __init__(
        self,
        host: Any,
        gitlab: GitLabClient | None = None,
        state_machine: WorkItemStateMachine | None = None,
    ) -> None:
        self.host = host
        self.gitlab = gitlab or GitLabClient()
        self.state_machine = state_machine or WorkItemStateMachine(host, self.gitlab)
        # Preserve the historical direct-call entrypoints without retaining
        # duplicate implementations in the legacy runtime.
        host.create_work_item_handoff = self.handoff
        host.ack_work_item_handoff = self.acknowledge
        host.update_work_item_progress = self.progress

    def _compat(self, name: str, fallback: Any) -> Any:
        """Resolve a composed host seam while supporting lightweight hosts.

        Application composition publishes state-machine methods on the legacy
        compatibility host. Tests and extensions historically monkeypatch those
        names directly, so prefer the host seam when present; standalone service
        hosts can fall back to the canonical state-machine method.
        """
        return getattr(self.host, name, fallback)

    async def list(
        self,
        *,
        project_id: str | None,
        owner: str | None,
        stage: str | None,
        release_gate: bool | None,
    ) -> dict[str, Any]:
        states = list(self.host._load_work_item_states().values())
        if project_id:
            states = [state for state in states if state.project_id == project_id]
        if owner:
            normalized_owner = self.state_machine._coerce_owner(owner)
            states = [
                state
                for state in states
                if self.state_machine._coerce_owner(state.current_owner or state.next_owner) == normalized_owner
            ]
        if stage:
            normalized_stage = self.state_machine._normalize_work_item_stage(stage, fallback="")
            states = [state for state in states if state.current_stage == normalized_stage]
        if release_gate is not None:
            states = [state for state in states if state.release_gate is release_gate]
        states.sort(key=lambda item: item.updated_at, reverse=True)
        return {
            "items": [self.state_machine._work_item_state_public(state) for state in states],
            "count": len(states),
        }

    async def _sync_from_gitlab_async(self) -> dict[str, int]:
        synced = 0
        seen_refs: set[str] = set()
        settings = self.host._load_gitlab_routing_settings()
        for project_id, project_settings in settings.projects.items():
            if not project_settings.enabled:
                continue
            token = self.host._gitlab_token_for_project(project_id)
            group = self.host._gitlab_group_path(project_settings)
            if not token or not group:
                continue
            issues = await self.gitlab.group_issues(
                self.host.GITLAB_API_BASE,
                group,
                token=token,
                state="opened",
            )
            for issue in issues:
                state = self.state_machine._upsert_work_item_state_from_gitlab_issue(
                    issue,
                    project_id=project_id,
                )
                if not state:
                    continue
                synced += 1
                seen_refs.add(state.ref)
        return {"synced": synced, "refs": len(seen_refs)}

    async def sync_from_gitlab(self) -> dict[str, Any]:
        try:
            result = await self._sync_from_gitlab_async()
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
        return self.state_machine._work_item_state_public(self.state_machine._work_item_state(ref))

    async def handoff(self, ref: str, payload: WorkItemHandoffCreate) -> dict[str, Any]:
        structured_handoff = self._compat("_structured_handoff", self.state_machine._structured_handoff)
        public_state = self._compat("_work_item_state_public", self.state_machine._work_item_state_public)
        state = structured_handoff(ref, payload)
        state = await self.state_machine.sync_gitlab_issue_labels(state)
        public = public_state(state)
        await self.host.hub.publish({"type": "work-item.handoff", "ref": ref, "state": public})
        self.host._schedule_structured_handoff_dispatch(state, source="work-item-handoff")
        self.host._schedule_handoff_continuity_check(state, source="work-item-handoff-continuity")
        return {"ok": True, "item": public}

    async def acknowledge(self, ref: str, payload: WorkItemAckCreate) -> dict[str, Any]:
        structured_ack = self._compat("_structured_ack", self.state_machine._structured_ack)
        public_state = self._compat("_work_item_state_public", self.state_machine._work_item_state_public)
        split_brain_findings = self._compat(
            "_work_item_split_brain_findings",
            self.state_machine._work_item_split_brain_findings,
        )
        state = structured_ack(ref, payload)
        state = await self.state_machine.sync_gitlab_issue_labels(state)
        public = public_state(state)
        await self.host.hub.publish({"type": "work-item.ack", "ref": ref, "state": public})
        self.host._schedule_actionable_owner_dispatch(state, source="work-item-ack", actor=payload.actor)
        self.host._schedule_actionable_owner_continuity_check(
            state,
            source="work-item-ack-continuity",
        )
        if split_brain_findings(state):
            self.host._schedule_native_recovery_cycles(reason="work-item-ack-routing-drift")
        return {"ok": True, "item": public}

    async def progress(self, ref: str, payload: WorkItemProgressUpdate) -> dict[str, Any]:
        structured_progress = self._compat("_structured_progress", self.state_machine._structured_progress)
        public_state = self._compat("_work_item_state_public", self.state_machine._work_item_state_public)
        split_brain_findings = self._compat(
            "_work_item_split_brain_findings",
            self.state_machine._work_item_split_brain_findings,
        )
        state = structured_progress(ref, payload)
        state = await self.state_machine.sync_gitlab_issue_labels(state)
        public = public_state(state)
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
        if split_brain_findings(state):
            self.host._schedule_native_recovery_cycles(reason="work-item-progress-routing-drift")
        return {"ok": True, "item": public}
