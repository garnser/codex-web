from __future__ import annotations

import asyncio
import time
from typing import Any

from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.identity import TenantScope
from codex_web.models import (
    TaskSourceConfiguration,
    WorkItemAckCreate,
    WorkItemHandoffCreate,
    WorkItemProgressUpdate,
)
from codex_web.services.gitlab_artifact_events import GitLabArtifactEventProjector
from codex_web.services.gitlab_sync_health import GitLabSyncHealth
from codex_web.services.builtin_task_source_runtime import install_builtin_task_source_runtime
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.gitlab_task_source_events import GitLabWebhookTaskSource
from codex_web.services.task_source_events import (
    TaskSourceEventReconciliationResult,
    TaskSourceWorkItemEventReconciler,
)
from codex_web.services.task_source_runtime import (
    TaskSourceRegistry,
    TaskSourceResolutionError,
    TaskSourceWritebackService,
)
from codex_web.services.task_source_work_items import TaskSourceWorkItemProjector
from codex_web.services.task_sources import (
    TaskSource,
    TaskSourceCapability,
    TaskSourceCreateCapable,
    TaskSourceCreateRequest,
    TaskSourceEvent,
)
from codex_web.services.work_item_state import WorkItemStateMachine


class _HostContinuityAdapter:
    """Dynamic compatibility edge for historical direct-call entrypoints."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        callback = getattr(self.host, name, None)
        if callback is None:
            return None
        return callback(*args, **kwargs)

    def schedule_structured_handoff_dispatch(
        self,
        state: Any,
        *,
        source: str,
    ) -> None:
        self._call(
            "_schedule_structured_handoff_dispatch",
            state,
            source=source,
        )

    def schedule_handoff_continuity_check(
        self,
        state: Any,
        *,
        source: str,
    ) -> None:
        self._call(
            "_schedule_handoff_continuity_check",
            state,
            source=source,
        )

    def schedule_actionable_owner_dispatch(
        self,
        state: Any,
        *,
        source: str,
        actor: str | None = None,
    ) -> None:
        self._call(
            "_schedule_actionable_owner_dispatch",
            state,
            source=source,
            actor=actor,
        )

    def schedule_actionable_owner_continuity_check(
        self,
        state: Any,
        *,
        source: str,
    ) -> None:
        self._call(
            "_schedule_actionable_owner_continuity_check",
            state,
            source=source,
        )

    def responsible_binding(self, state: Any) -> Any | None:
        owner_callback = getattr(
            self.host,
            "_coerce_owner",
            lambda owner: owner,
        )
        owner = owner_callback(
            getattr(state, "current_owner", None)
            or getattr(state, "next_owner", None)
        )
        project_id = getattr(state, "project_id", None)
        if not owner or not project_id:
            return None
        callback = getattr(self.host, "_binding_for_agent", None)
        if callback is None:
            return None
        return callback(
            owner,
            project_id,
            preferred_conversation_id=getattr(
                self.host,
                "HANDOFF_COORDINATION_CHANNEL",
                "",
            ),
        )

    def dispatch_text(self, state: Any) -> str:
        callback = getattr(self.host, "_work_item_dispatch_text", None)
        return callback(state) if callback is not None else state.ref


class _HostRecoveryAdapter:
    def __init__(self, host: Any) -> None:
        self.host = host

    def schedule(self, *, reason: str = "manual") -> bool:
        callback = getattr(
            self.host,
            "_schedule_native_recovery_cycles",
            None,
        )
        if callback is None:
            return False
        callback(reason=reason)
        return True


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
        gitlab_artifact_events: GitLabArtifactEventProjector | None = None,
        continuity: Any | None = None,
        recovery: Any | None = None,
        sync_health: GitLabSyncHealth | None = None,
        event_sink: Any | None = None,
        publish_event: Any | None = None,
        truncate_text: Any | None = None,
    ) -> None:
        self.host = host
        self.gitlab = gitlab or GitLabClient()
        self.continuity = continuity or _HostContinuityAdapter(host)
        self.recovery = recovery or _HostRecoveryAdapter(host)
        self.sync_health = sync_health or GitLabSyncHealth()
        self.event_sink = event_sink or getattr(
            host,
            "_append_bot_event",
            lambda _event: None,
        )
        self.publish_event = publish_event or host.hub.publish
        self.truncate_text = truncate_text or getattr(
            host,
            "_truncate_text",
            lambda value, limit: str(value)[:limit],
        )
        self.compatibility_continuity = _HostContinuityAdapter(host)
        self.compatibility_recovery = _HostRecoveryAdapter(host)
        self.state_machine = state_machine or WorkItemStateMachine(host)
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
        register_project = getattr(
            self.task_source_registry,
            "register_project",
            None,
        )
        if callable(register_project):
            register_project(
                "gitlab",
                self._gitlab_source_for_project,
            )
        app_state = getattr(getattr(host, "app", None), "state", None)
        identity_service = getattr(app_state, "identity_service", None)
        secret_broker = getattr(app_state, "secret_broker", None)
        self.builtin_task_source_runtime = None
        if identity_service is not None and secret_broker is not None:
            self.builtin_task_source_runtime = install_builtin_task_source_runtime(
                self.task_source_registry,
                host,
                identity_service,
                secret_broker,
            )
        self.gitlab_artifact_events = gitlab_artifact_events or GitLabArtifactEventProjector(
            host,
            self.state_machine,
        )

        # Preserve historical entrypoints at the integration edge. These aliases
        # terminate at TaskSource/integration services, never provider code in
        # the canonical state machine.
        host.create_work_item_handoff = self.compatibility_handoff
        host.ack_work_item_handoff = self.compatibility_acknowledge
        host.update_work_item_progress = self.compatibility_progress
        host._reconcile_task_source_event = self.reconcile_task_source_event
        host._upsert_work_item_state_from_gitlab_issue = self.project_gitlab_issue_compat
        host._upsert_work_item_state_from_gitlab_event = self.project_gitlab_event_compat
        host._sync_gitlab_issue_labels_from_work_item = self.schedule_task_source_writeback

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

    def _gitlab_source_for_project(
        self,
        configuration: TaskSourceConfiguration,
        project_id: str,
        scope: TenantScope,
    ) -> TaskSource | None:
        del scope  # tenant selection is enforced before registry resolution.
        if configuration.source_type.casefold() != "gitlab":
            return None
        token = self.host._gitlab_token_for_project(project_id)
        if not token:
            return None
        return GitLabTaskSource(
            configuration.source_instance,
            token,
            client=self.gitlab,
        )

    def _project_for_scope(
        self,
        project_id: str,
        scope: TenantScope,
    ) -> Any:
        project = next(
            (
                item
                for item in self.host._load_projects()
                if getattr(item, "id", None) == project_id
                and getattr(item, "organization_id", None) == scope.organization_id
                and getattr(item, "workspace_id", None) == scope.workspace_id
            ),
            None,
        )
        if project is None:
            raise LookupError("Project not found")
        return project

    def resolve_authoritative_create(
        self,
        project_id: str,
        *,
        scope: TenantScope,
    ) -> tuple[TaskSourceConfiguration, TaskSourceCreateCapable]:
        """Resolve and validate CREATE without performing an external mutation."""
        project = self._project_for_scope(project_id, scope)
        configuration = getattr(project, "authoritative_task_source", None)
        if configuration is None:
            raise TaskSourceResolutionError(
                "Project has no authoritative task source configured"
            )
        source = self.task_source_registry.resolve_project(
            configuration,
            project_id=project_id,
            scope=scope,
            required=True,
        )
        assert source is not None
        source.capabilities.require(TaskSourceCapability.CREATE)
        if not isinstance(source, TaskSourceCreateCapable):
            raise TaskSourceResolutionError(
                "Task-source adapter advertises CREATE without create() support"
            )
        return configuration, source

    async def create_authoritative(
        self,
        project_id: str,
        payload: TaskSourceCreateRequest,
        *,
        scope: TenantScope,
    ) -> Any:
        """Create through the project's authoritative source, then project state.

        This is an internal execution seam. Callers that originate a new external
        side effect must place it behind the canonical ActionIntent/ActionProvider
        boundary rather than exposing this method directly as an HTTP mutation.
        """
        configuration, source = self.resolve_authoritative_create(
            project_id,
            scope=scope,
        )
        snapshot = await source.create(payload, scope=configuration.scope)
        return self.task_source_projector.upsert(
            source,
            snapshot,
            project_id=project_id,
        )

    def schedule_task_source_writeback(self, state: Any) -> Any:
        """Compatibility scheduler backed by provider-neutral write-back."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return state
        snapshot = state.model_copy(deep=True) if hasattr(state, "model_copy") else state

        async def run() -> None:
            try:
                await self.task_source_writeback.sync(snapshot)
            except Exception as exc:
                append = getattr(self.host, "_append_bot_event", None)
                if callable(append):
                    append(
                        {
                            "type": "task_source_writeback_failed",
                            "ref": getattr(snapshot, "ref", None),
                            "error": str(exc)[:500],
                        }
                    )

        loop.create_task(run())
        return state

    async def list(
        self,
        *,
        project_id: str | None,
        owner: str | None,
        stage: str | None,
        release_gate: bool | None,
        scope: TenantScope | None = None,
    ) -> dict[str, Any]:
        states = list(self.host._load_work_item_states().values())
        if scope is not None:
            states = [
                state
                for state in states
                if state.organization_id == scope.organization_id
                and state.workspace_id == scope.workspace_id
            ]
        if project_id:
            states = [state for state in states if state.project_id == project_id]
        if owner:
            normalized_owner = self.state_machine._coerce_owner(owner)
            states = [
                state
                for state in states
                if self.state_machine._coerce_owner(state.current_owner or state.next_owner)
                == normalized_owner
            ]
        if stage:
            normalized_stage = self.state_machine._normalize_work_item_stage(
                stage,
                fallback="",
            )
            states = [state for state in states if state.current_stage == normalized_stage]
        if release_gate is not None:
            states = [state for state in states if state.release_gate is release_gate]
        states.sort(key=lambda item: item.updated_at, reverse=True)
        return {
            "items": [self.state_machine._work_item_state_public(state) for state in states],
            "count": len(states),
        }

    async def _sync_from_gitlab_async(self, scope: TenantScope | None = None) -> dict[str, int]:
        """Compatibility entrypoint backed by the provider-neutral TaskSource path."""

        synced = 0
        seen_refs: set[str] = set()
        settings = self.host._load_gitlab_routing_settings()
        allowed_project_ids: set[str] | None = None
        if scope is not None:
            allowed_project_ids = {
                project.id
                for project in self.host._load_projects()
                if project.organization_id == scope.organization_id
                and project.workspace_id == scope.workspace_id
            }
        for project_id, project_settings in settings.projects.items():
            if allowed_project_ids is not None and project_id not in allowed_project_ids:
                continue
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

    def project_gitlab_issue_compat(
        self,
        issue: dict[str, Any],
        *,
        project_id: str,
    ) -> Any:
        """Legacy issue-projection name backed by normalized TaskSource state."""

        source = GitLabWebhookTaskSource(
            self.host.GITLAB_API_BASE,
            client=self.gitlab,
        )
        try:
            snapshot = source._snapshot_from_issue(issue)
        except ValueError:
            return None
        return self.task_source_projector.upsert(
            source,
            snapshot,
            project_id=project_id,
        )

    def _append_legacy_gitlab_stale_event(
        self,
        state: Any,
        *,
        payload: dict[str, Any],
    ) -> None:
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
        if not any(
            str(label).startswith(("owner::", "status::")) for label in labels
        ):
            return state
        sync = getattr(
            self.host,
            "_sync_gitlab_issue_labels_from_work_item",
            self.schedule_task_source_writeback,
        )
        projected = sync(state) if callable(sync) else state
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
        """Compatibility hook used by GitLabService during migration."""

        source = GitLabWebhookTaskSource(
            self.host.GITLAB_API_BASE,
            client=self.gitlab,
        )
        event = source.normalize_event_sync(payload)
        if event is None:
            return self.gitlab_artifact_events.project(payload, project_id=project_id)

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

    async def sync_from_gitlab(
        self,
        scope: TenantScope | None = None,
    ) -> dict[str, Any]:
        try:
            result = await self._sync_from_gitlab_async(scope)
        except Exception as exc:
            error = self.truncate_text(str(exc), 500)
            health = self.sync_health.record_failure(error)
            self.event_sink(
                {
                    "type": "gitlab_work_item_sync_failed",
                    "failure_count": health["consecutive_failures"],
                    "error": health["last_error"],
                }
            )
            raise

        self.sync_health.record_success()
        await self.publish_event({"type": "work-item.sync", **result})
        return {"ok": True, **result}

    async def resume_provider_capacity_wait(
        self,
        wait: Any,
        execution: Any,
    ) -> bool:
        """Resume a capacity wait through canonical work-item state/continuity."""
        if not getattr(wait, "work_item_ref", None):
            return False
        try:
            state = self.state_machine._work_item_state(wait.work_item_ref)
        except Exception:
            return False
        binding = self.continuity.responsible_binding(state)
        if binding is None or not state.project_id:
            return False
        execution.enqueue_turn(
            thread_id=binding.thread_id,
            project_id=state.project_id,
            message=self.continuity.dispatch_text(state),
            source=f"provider-capacity-resume:{wait.id}",
            execution_id=wait.execution_id,
        )
        execution.schedule_queue_drain(binding.thread_id)
        return True

    async def get(self, ref: str) -> dict[str, Any]:
        return self.state_machine._work_item_state_public(
            self.state_machine._work_item_state(ref)
        )

    async def comment(self, ref: str, body: str) -> dict[str, Any]:
        state = self.state_machine._work_item_state(ref)
        await self.task_source_writeback.add_comment(state, body)
        return {"ok": True, "ref": ref}

    async def _handoff_with(
        self,
        ref: str,
        payload: WorkItemHandoffCreate,
        continuity: Any,
    ) -> dict[str, Any]:
        structured_handoff = self._compat(
            "_structured_handoff",
            self.state_machine._structured_handoff,
        )
        public_state = self._compat(
            "_work_item_state_public",
            self.state_machine._work_item_state_public,
        )
        state = structured_handoff(ref, payload)
        state = await self.task_source_writeback.sync(state)
        public = public_state(state)
        await self.host.hub.publish(
            {"type": "work-item.handoff", "ref": ref, "state": public}
        )
        continuity.schedule_structured_handoff_dispatch(
            state,
            source="work-item-handoff",
        )
        continuity.schedule_handoff_continuity_check(
            state,
            source="work-item-handoff-continuity",
        )
        return {"ok": True, "item": public}

    async def handoff(
        self,
        ref: str,
        payload: WorkItemHandoffCreate,
    ) -> dict[str, Any]:
        return await self._handoff_with(ref, payload, self.continuity)

    async def compatibility_handoff(
        self,
        ref: str,
        payload: WorkItemHandoffCreate,
    ) -> dict[str, Any]:
        return await self._handoff_with(
            ref,
            payload,
            self.compatibility_continuity,
        )

    async def _acknowledge_with(
        self,
        ref: str,
        payload: WorkItemAckCreate,
        continuity: Any,
        recovery: Any,
    ) -> dict[str, Any]:
        structured_ack = self._compat(
            "_structured_ack",
            self.state_machine._structured_ack,
        )
        public_state = self._compat(
            "_work_item_state_public",
            self.state_machine._work_item_state_public,
        )
        split_brain_findings = self._compat(
            "_work_item_split_brain_findings",
            self.state_machine._work_item_split_brain_findings,
        )
        state = structured_ack(ref, payload)
        state = await self.task_source_writeback.sync(state)
        public = public_state(state)
        await self.host.hub.publish(
            {"type": "work-item.ack", "ref": ref, "state": public}
        )
        continuity.schedule_actionable_owner_dispatch(
            state,
            source="work-item-ack",
            actor=payload.actor,
        )
        continuity.schedule_actionable_owner_continuity_check(
            state,
            source="work-item-ack-continuity",
        )
        if split_brain_findings(state):
            recovery.schedule(reason="work-item-ack-routing-drift")
        return {"ok": True, "item": public}

    async def acknowledge(
        self,
        ref: str,
        payload: WorkItemAckCreate,
    ) -> dict[str, Any]:
        return await self._acknowledge_with(
            ref,
            payload,
            self.continuity,
            self.recovery,
        )

    async def compatibility_acknowledge(
        self,
        ref: str,
        payload: WorkItemAckCreate,
    ) -> dict[str, Any]:
        return await self._acknowledge_with(
            ref,
            payload,
            self.compatibility_continuity,
            self.compatibility_recovery,
        )

    async def _progress_with(
        self,
        ref: str,
        payload: WorkItemProgressUpdate,
        continuity: Any,
        recovery: Any,
    ) -> dict[str, Any]:
        structured_progress = self._compat(
            "_structured_progress",
            self.state_machine._structured_progress,
        )
        public_state = self._compat(
            "_work_item_state_public",
            self.state_machine._work_item_state_public,
        )
        split_brain_findings = self._compat(
            "_work_item_split_brain_findings",
            self.state_machine._work_item_split_brain_findings,
        )
        state = structured_progress(ref, payload)
        state = await self.task_source_writeback.sync(state)
        public = public_state(state)
        await self.host.hub.publish(
            {"type": "work-item.progress", "ref": ref, "state": public}
        )
        continuity.schedule_actionable_owner_dispatch(
            state,
            source="work-item-progress",
            actor=payload.actor,
        )
        continuity.schedule_actionable_owner_continuity_check(
            state,
            source="work-item-progress-continuity",
        )
        if split_brain_findings(state):
            recovery.schedule(reason="work-item-progress-routing-drift")
        return {"ok": True, "item": public}

    async def progress(
        self,
        ref: str,
        payload: WorkItemProgressUpdate,
    ) -> dict[str, Any]:
        return await self._progress_with(
            ref,
            payload,
            self.continuity,
            self.recovery,
        )

    async def compatibility_progress(
        self,
        ref: str,
        payload: WorkItemProgressUpdate,
    ) -> dict[str, Any]:
        return await self._progress_with(
            ref,
            payload,
            self.compatibility_continuity,
            self.compatibility_recovery,
        )
