from __future__ import annotations

import time
from typing import Any

from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.models import (
    WorkItemAckCreate,
    WorkItemHandoffCreate,
    WorkItemProgressUpdate,
)
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.gitlab_task_source_events import GitLabWebhookTaskSource
from codex_web.services.task_source_events import (
    TaskSourceEventReconciliationResult,
    TaskSourceWorkItemEventReconciler,
)
from codex_web.services.task_source_runtime import (
    TaskSourceRegistry,
    TaskSourceWritebackService,
)
from codex_web.services.task_source_work_items import TaskSourceWorkItemProjector
from codex_web.services.task_sources import TaskSource, TaskSourceEvent
from codex_web.services.work_item_state import WorkItemStateMachine


class WorkItemService:
    """Work-item API behavior backed by canonical state and TaskSource services."""

    def __init__(
        self,
        host: Any,
        gitlab: GitLabClient | None = None,
        state_machine: WorkItemStateMachine | None = None,
        task_source_projector: TaskSourceWorkItemProjector | None = None,
        task_source_event_reconciler: TaskSourceWorkItemEventReconciler | None = None,
        task_source_registry: TaskSourceRegistry | None = None,
        task_source_writeback: TaskSourceWritebackService | None = None,
    ) -> None:
        self.host = host
        self.gitlab = gitlab or GitLabClient()
        self.state_machine = state_machine or WorkItemStateMachine(host, self.gitlab)
        self.task_source_projector = task_source_projector or TaskSourceWorkItemProjector(
            host,
            self.state_machine,
        )
        self.task_source_event_reconciler = (
            task_source_event_reconciler
            or TaskSourceWorkItemEventReconciler(host, self.task_source_projector)
        )
        if task_source_writeback is None:
            registry = task_source_registry or TaskSourceRegistry()
            registry.register("gitlab", self._gitlab_source_for_state)
            self.task_source_writeback = TaskSourceWritebackService(host, registry)
            self.task_source_registry = registry
        else:
            self.task_source_writeback = task_source_writeback
            self.task_source_registry = task_source_registry or task_source_writeback.registry

        self._legacy_gitlab_event_projector = getattr(
            host,
            "_upsert_work_item_state_from_gitlab_event",
            None,
        ) or getattr(
            self.state_machine,
            "_upsert_work_item_state_from_gitlab_event",
            None,
        )

        # Preserve historical entrypoints while publishing canonical service
        # behavior to remaining legacy composition seams.
        host.create_work_item_handoff = self.handoff
        host.ack_work_item_handoff = self.acknowledge
        host.update_work_item_progress = self.progress
        host._reconcile_task_source_event = self.reconcile_task_source_event
        # GitLabService still invokes this historical synchronous hook. Issue
        # events now terminate at the provider-neutral TaskSource boundary;
        # non-issue events temporarily retain the legacy projector until their
        # artifact semantics are migrated in the next #101 slice.
        host._upsert_work_item_state_from_gitlab_event = self.project_gitlab_event_compat

    def _compat(self, name: str, fallback: Any) -> Any:
        """Resolve a composed host seam while supporting lightweight hosts."""
        return getattr(self.host, name, fallback)

    def _gitlab_source_for_state(self, state: Any) -> TaskSource | None:
        project_id = getattr(state, "project_id", None)
        if not project_id:
            return None
        token = self.host._gitlab_token_for_project(project_id)
        if not token:
            return None
        return GitLabTaskSource(
            self.host.GITLAB_API_BASE,
            token,
            client=self.gitlab,
        )

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
        """Compatibility entrypoint backed by the provider-neutral TaskSource path."""

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

            source = GitLabTaskSource(
                self.host.GITLAB_API_BASE,
                token,
                client=self.gitlab,
            )
            snapshots = await source.discover(scope=group)
            for snapshot in snapshots:
                state = self.task_source_projector.upsert(
                    source,
                    snapshot,
                    project_id=project_id,
                )
                synced += 1
                seen_refs.add(state.ref)
        return {"synced": synced, "refs": len(seen_refs)}

    def reconcile_task_source_event(
        self,
        source: TaskSource,
        event: TaskSourceEvent,
        *,
        project_id: str,
        event_cursor: str | None = None,
    ) -> TaskSourceEventReconciliationResult:
        """Reconcile one normalized authoritative-source event locally."""

        return self.task_source_event_reconciler.reconcile(
            source,
            event,
            project_id=project_id,
            event_cursor=event_cursor,
        )

    def _append_legacy_gitlab_stale_event(self, state: Any, *, payload: dict[str, Any]) -> None:
        append = getattr(self.state_machine, "_append_work_item_event", None)
        make_event = getattr(self.state_machine, "_work_item_event", None)
        if state is None or not callable(append) or not callable(make_event):
            return
        attrs = payload.get("object_attributes") or {}
        append(
            make_event(
                state.ref,
                "gitlab_event_stale_ignored",
                payload={
                    "projected_owner": None,
                    "projected_stage": state.current_stage,
                    "projected_status_label": state.status_label,
                    "event_timestamp": attrs.get("updated_at") or attrs.get("closed_at"),
                },
            )
        )

    def _preserve_gitlab_closed_label_cleanup(self, state: Any) -> Any:
        if state is None or getattr(state, "current_stage", None) != "closed":
            return state
        labels = list(getattr(state, "labels", None) or [])
        if not any(str(label).startswith(("owner::", "status::")) for label in labels):
            return state
        sync = getattr(self.host, "_sync_gitlab_issue_labels_from_work_item", None)
        if not callable(sync):
            return state
        projected = sync(state)
        if projected is not None:
            state = projected
        save = getattr(self.state_machine, "_save_work_item_state", None)
        if callable(save):
            state = save(state)
        return state

    def project_gitlab_event_compat(
        self,
        payload: dict[str, Any],
        *,
        project_id: str,
    ) -> Any:
        """Compatibility hook used by GitLabService during incremental migration."""

        source = GitLabWebhookTaskSource(
            self.host.GITLAB_API_BASE,
            client=self.gitlab,
        )
        event = source.normalize_event_sync(payload)
        if event is None:
            if self._legacy_gitlab_event_projector is None:
                return None
            return self._legacy_gitlab_event_projector(payload, project_id=project_id)

        result = self.reconcile_task_source_event(
            source,
            event,
            project_id=project_id,
        )
        state = result.state
        if result.decision.outcome.value == "stale":
            self._append_legacy_gitlab_stale_event(state, payload=payload)
        state = self._preserve_gitlab_closed_label_cleanup(state)
        return state

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

    async def comment(self, ref: str, body: str) -> dict[str, Any]:
        state = self.state_machine._work_item_state(ref)
        await self.task_source_writeback.add_comment(state, body)
        return {"ok": True, "ref": ref}

    async def handoff(self, ref: str, payload: WorkItemHandoffCreate) -> dict[str, Any]:
        structured_handoff = self._compat("_structured_handoff", self.state_machine._structured_handoff)
        public_state = self._compat("_work_item_state_public", self.state_machine._work_item_state_public)
        state = structured_handoff(ref, payload)
        state = await self.task_source_writeback.sync(state)
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
        state = await self.task_source_writeback.sync(state)
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
        state = await self.task_source_writeback.sync(state)
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
