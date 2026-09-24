from __future__ import annotations

import asyncio
import base64
import json
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
from codex_web.services.builtin_task_source_runtime import (
    SecretBoundTaskSource,
    install_builtin_task_source_runtime,
)
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
from codex_web.services.work_item_dependencies import (
    GitLabWorkItemDependencies,
    WorkItemRuntimeDependencies,
)
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.storage.work_item_list_index import WorkItemListIndex


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


class _BootstrapRecoveryScheduler:
    """No-op only during application composition before recovery is attached."""

    def schedule(self, *, reason: str = "manual") -> bool:
        del reason
        return False


class WorkItemCompatibilityFacade:
    """Historical server-module entrypoints isolated from production services."""

    def __init__(self, host: Any, service: "WorkItemService") -> None:
        self.host = host
        self.service = service
        self.continuity = _HostContinuityAdapter(host)
        self.recovery = _HostRecoveryAdapter(host)

    def resolve(self, name: str, fallback: Any) -> Any:
        return getattr(self.host, name, fallback)

    async def publish(self, event: dict[str, Any]) -> None:
        hub = getattr(self.host, "hub", None)
        publish = getattr(hub, "publish", None)
        if publish is not None:
            await publish(event)

    def append_work_item_event(self, event: Any) -> None:
        path = getattr(
            self.host,
            "WORK_ITEM_EVENTS_FILE",
            self.service.work_items.events_file,
        )
        canonical_path = self.service.work_items.events_file
        if path == canonical_path:
            self.service.state_machine._append_work_item_event(event)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json())
            handle.write("\n")

    def project_gitlab_issue(
        self,
        issue: dict[str, Any],
        *,
        project_id: str,
    ) -> Any:
        return self.service.project_gitlab_issue_compat(
            issue,
            project_id=project_id,
        )

    def project_gitlab_event(
        self,
        payload: dict[str, Any],
        *,
        project_id: str,
    ) -> Any:
        return self.service.project_gitlab_event_compat(
            payload,
            project_id=project_id,
            append_event=self.resolve(
                "_append_work_item_event",
                self.append_work_item_event,
            ),
            sync_writeback=self.resolve(
                "_sync_gitlab_issue_labels_from_work_item",
                self.service.schedule_task_source_writeback,
            ),
        )

    async def handoff(
        self,
        ref: str,
        payload: WorkItemHandoffCreate,
    ) -> dict[str, Any]:
        return await self.service._handoff_with(
            ref,
            payload,
            continuity=self.continuity,
            structured_handoff=self.resolve(
                "_structured_handoff",
                self.service.state_machine._structured_handoff,
            ),
            public_state=self.resolve(
                "_work_item_state_public",
                self.service.state_machine._work_item_state_public,
            ),
            publish_event=self.publish,
        )

    async def acknowledge(
        self,
        ref: str,
        payload: WorkItemAckCreate,
    ) -> dict[str, Any]:
        return await self.service._acknowledge_with(
            ref,
            payload,
            continuity=self.continuity,
            recovery=self.recovery,
            structured_ack=self.resolve(
                "_structured_ack",
                self.service.state_machine._structured_ack,
            ),
            public_state=self.resolve(
                "_work_item_state_public",
                self.service.state_machine._work_item_state_public,
            ),
            split_brain_findings=self.resolve(
                "_work_item_split_brain_findings",
                self.service.state_machine._work_item_split_brain_findings,
            ),
            publish_event=self.publish,
        )

    async def progress(
        self,
        ref: str,
        payload: WorkItemProgressUpdate,
    ) -> dict[str, Any]:
        return await self.service._progress_with(
            ref,
            payload,
            continuity=self.continuity,
            recovery=self.recovery,
            structured_progress=self.resolve(
                "_structured_progress",
                self.service.state_machine._structured_progress,
            ),
            public_state=self.resolve(
                "_work_item_state_public",
                self.service.state_machine._work_item_state_public,
            ),
            split_brain_findings=self.resolve(
                "_work_item_split_brain_findings",
                self.service.state_machine._work_item_split_brain_findings,
            ),
            publish_event=self.publish,
        )


class WorkItemService:
    """Work-item API behavior over explicit canonical dependencies."""

    def __init__(
        self,
        host: Any | None = None,
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
        work_item_dependencies: WorkItemRuntimeDependencies | None = None,
        gitlab_dependencies: GitLabWorkItemDependencies | None = None,
        identity_service: Any | None = None,
        secret_broker: Any | None = None,
        work_item_list_index: WorkItemListIndex | None = None,
    ) -> None:
        async def _publish_noop(_event: dict[str, Any]) -> None:
            return None

        if work_item_dependencies is None:
            work_item_dependencies = getattr(
                state_machine,
                "dependencies",
                None,
            )
        if work_item_dependencies is None:
            if host is None:
                raise TypeError(
                    "WorkItemService requires work-item dependencies"
                )
            work_item_dependencies = (
                WorkItemRuntimeDependencies.from_host(host)
            )
        if gitlab_dependencies is None:
            if host is None:
                raise TypeError(
                    "WorkItemService requires GitLab dependencies"
                )
            gitlab_dependencies = GitLabWorkItemDependencies.from_host(host)

        self.work_items = work_item_dependencies
        self.work_item_list_index = work_item_list_index
        self.gitlab_dependencies = gitlab_dependencies
        self.gitlab = gitlab or GitLabClient()
        self.continuity = (
            continuity
            or (_HostContinuityAdapter(host) if host is not None else None)
        )
        self.recovery = (
            recovery
            or (
                _HostRecoveryAdapter(host)
                if host is not None
                else _BootstrapRecoveryScheduler()
            )
        )
        if self.continuity is None:
            raise TypeError(
                "WorkItemService requires a continuity service"
            )
        self.sync_health = sync_health or GitLabSyncHealth()
        self.event_sink = event_sink or (
            getattr(host, "_append_bot_event", None)
            if host is not None
            else None
        ) or (lambda _event: None)
        host_hub = getattr(host, "hub", None) if host is not None else None
        self.publish_event = (
            publish_event
            or getattr(host_hub, "publish", None)
            or _publish_noop
        )
        self.truncate_text = truncate_text or (
            getattr(host, "_truncate_text", None)
            if host is not None
            else None
        ) or (lambda value, limit: str(value)[:limit])

        self.state_machine = state_machine or WorkItemStateMachine(
            host,
            dependencies=self.work_items,
        )
        self.task_source_projector = (
            task_source_projector
            or TaskSourceWorkItemProjector(
                host,
                self.state_machine,
                dependencies=self.work_items,
            )
        )
        self.task_source_event_reconciler = (
            task_source_event_reconciler
            or TaskSourceWorkItemEventReconciler(
                host,
                self.task_source_projector,
                dependencies=self.work_items,
            )
        )
        if task_source_writeback is None:
            registry = task_source_registry or TaskSourceRegistry()
            self.task_source_writeback = TaskSourceWritebackService(
                host,
                registry,
                dependencies=self.work_items,
            )
            self.task_source_registry = registry
        else:
            self.task_source_writeback = task_source_writeback
            self.task_source_registry = (
                task_source_registry or task_source_writeback.registry
            )

        register_source = getattr(
            self.task_source_registry,
            "register",
            None,
        )
        if callable(register_source):
            register_source(
                "gitlab",
                self._gitlab_source_for_state,
            )
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

        if identity_service is None and host is not None:
            app_state = getattr(getattr(host, "app", None), "state", None)
            identity_service = getattr(
                app_state,
                "identity_service",
                None,
            )
            secret_broker = secret_broker or getattr(
                app_state,
                "secret_broker",
                None,
            )
        self.identity_service = identity_service
        self.secret_broker = secret_broker
        self.builtin_task_source_runtime = None
        if identity_service is not None and secret_broker is not None:
            self.builtin_task_source_runtime = (
                install_builtin_task_source_runtime(
                    self.task_source_registry,
                    host,
                    identity_service,
                    secret_broker,
                    load_projects=self.work_items.load_projects,
                )
            )

        self.gitlab_artifact_events = (
            gitlab_artifact_events
            or GitLabArtifactEventProjector(
                host,
                self.state_machine,
                work_item_dependencies=self.work_items,
                gitlab_dependencies=self.gitlab_dependencies,
            )
        )

        if host is not None:
            compatibility = WorkItemCompatibilityFacade(host, self)
            host.create_work_item_handoff = compatibility.handoff
            host.ack_work_item_handoff = compatibility.acknowledge
            host.update_work_item_progress = compatibility.progress
            host._reconcile_task_source_event = (
                self.reconcile_task_source_event
            )
            host._upsert_work_item_state_from_gitlab_issue = (
                self.project_gitlab_issue_compat
            )
            host._upsert_work_item_state_from_gitlab_event = (
                self.project_gitlab_event_compat
            )
            host._sync_gitlab_issue_labels_from_work_item = (
                self.schedule_task_source_writeback
            )
            app_state = getattr(getattr(host, "app", None), "state", None)
            if app_state is not None:
                app_state.work_item_compatibility_service = compatibility

    def _canonical_gitlab_source(
        self,
        configuration: TaskSourceConfiguration,
        *,
        scope: TenantScope,
    ) -> TaskSource | None:
        secret_id = str(
            configuration.credential_secret_id or ""
        ).strip()
        if not secret_id:
            return None
        if self.identity_service is None or self.secret_broker is None:
            raise TaskSourceResolutionError(
                "canonical GitLab credential runtime is unavailable"
            )
        actor = self.identity_service.bootstrap_service_actor(
            identity_id="service-task-source-runtime",
            name="Task source runtime",
            scope=scope,
            service_scopes=("secret:use",),
        )
        try:
            self.secret_broker.metadata(
                secret_id,
                actor=actor,
                require_use=True,
            )
        except Exception as exc:
            raise TaskSourceResolutionError(
                "gitlab task-source credential is unavailable"
            ) from exc

        def builder(secret: str) -> TaskSource:
            return GitLabTaskSource(
                configuration.source_instance,
                secret,
                client=self.gitlab,
            )

        projection = GitLabTaskSource(
            configuration.source_instance,
            "__credential_not_loaded__",
            client=self.gitlab,
        )
        return SecretBoundTaskSource(
            source_type="gitlab",
            source_instance=configuration.source_instance,
            credential_secret_id=secret_id,
            actor=actor,
            secret_broker=self.secret_broker,
            builder=builder,
            projection_source=projection,
        )

    def _gitlab_source_for_state(
        self,
        state: Any,
    ) -> TaskSource | None:
        project_id = getattr(state, "project_id", None)
        if not project_id:
            return None
        scope = TenantScope(
            organization_id=getattr(
                state,
                "organization_id",
                "local",
            ),
            workspace_id=getattr(
                state,
                "workspace_id",
                "default",
            ),
        )
        try:
            project = self._project_for_scope(project_id, scope)
        except LookupError:
            project = None
        configuration = getattr(
            project,
            "authoritative_task_source",
            None,
        )
        if (
            configuration is not None
            and configuration.source_type.casefold() == "gitlab"
            and configuration.credential_secret_id
        ):
            return self._canonical_gitlab_source(
                configuration,
                scope=scope,
            )

        token = self.gitlab_dependencies.token_for_project(project_id)
        if not token:
            return None
        return GitLabTaskSource(
            self.gitlab_dependencies.api_base_url,
            token,
            client=self.gitlab,
        )

    def _gitlab_source_for_project(
        self,
        configuration: TaskSourceConfiguration,
        project_id: str,
        scope: TenantScope,
    ) -> TaskSource | None:
        if configuration.source_type.casefold() != "gitlab":
            return None
        if configuration.credential_secret_id:
            return self._canonical_gitlab_source(
                configuration,
                scope=scope,
            )

        # Compatibility-only fallback for Projects not yet materialized by
        # canonical legacy migration. Once a SecretReference is present the
        # raw legacy credential path is never consulted.
        token = self.gitlab_dependencies.token_for_project(project_id)
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
        dependencies = getattr(self, "work_items", None)
        if dependencies is not None:
            projects = dependencies.load_projects()
        else:
            compatibility_host = getattr(self, "host", None)
            projects = (
                compatibility_host._load_projects()
                if compatibility_host is not None
                else []
            )
        project = next(
            (
                item
                for item in projects
                if getattr(item, "id", None) == project_id
                and getattr(item, "organization_id", None)
                == scope.organization_id
                and getattr(item, "workspace_id", None)
                == scope.workspace_id
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
        """Compatibility entrypoint backed by keyed coalesced writeback."""
        return self.task_source_writeback.schedule(state)

    @staticmethod
    def _list_cursor_payload(
        *,
        after: str,
        project_id: str,
        owner: str | None,
        stage: str | None,
        release_gate: bool | None,
        query: str | None = None,
        revision: float | None = None,
    ) -> str:
        payload = {
            "after": after,
            "projectId": project_id,
            "owner": str(owner or "").strip(),
            "stage": str(stage or "").strip(),
            "releaseGate": release_gate,
            "query": str(query or "").strip().casefold(),
            "revision": revision,
        }
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_list_cursor(
        cursor: str,
        *,
        project_id: str,
        owner: str | None,
        stage: str | None,
        release_gate: bool | None,
        query: str | None = None,
    ) -> dict[str, Any]:
        from fastapi import HTTPException

        try:
            padding = "=" * (-len(cursor) % 4)
            payload = json.loads(
                base64.urlsafe_b64decode(
                    (cursor + padding).encode()
                )
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail="invalid Work Item cursor",
            ) from exc
        expected = {
            "projectId": project_id,
            "owner": str(owner or "").strip(),
            "stage": str(stage or "").strip(),
            "releaseGate": release_gate,
            "query": str(query or "").strip().casefold(),
        }
        actual = {
            "projectId": str(payload.get("projectId") or ""),
            "owner": str(payload.get("owner") or "").strip(),
            "stage": str(payload.get("stage") or "").strip(),
            "releaseGate": payload.get("releaseGate"),
            "query": str(payload.get("query") or "").strip().casefold(),
        }
        if not payload.get("after") or actual != expected:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Work Item cursor does not match the current "
                    "Project/filter query"
                ),
            )
        return payload

    def _work_item_list_public(self, state: Any) -> dict[str, Any]:
        routing_errors = self.state_machine._work_item_split_brain_findings(
            state
        )
        return {
            "ref": state.ref,
            "project_id": state.project_id,
            "title": state.title,
            "url": state.url,
            "kind": state.kind,
            "priority": state.priority,
            "current_owner": state.current_owner,
            "current_stage": state.current_stage,
            "next_owner": state.next_owner,
            "next_action": state.next_action,
            "artifact_state": state.artifact_state,
            "blocker": state.blocker,
            "release_gate": state.release_gate,
            "status_label": state.status_label,
            "updated_at": state.updated_at,
            "created_at": state.created_at,
            "routingError": routing_errors[0] if routing_errors else None,
        }

    async def list(
        self,
        *,
        project_id: str | None,
        owner: str | None,
        stage: str | None,
        release_gate: bool | None,
        q: str | None = None,
        scope: TenantScope | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        from fastapi import HTTPException

        if scope is None:
            scope = TenantScope()
        if not project_id:
            raise HTTPException(
                status_code=400,
                detail="project_id is required for paginated Work Item lists",
            )
        self._project_for_scope(project_id, scope)

        page_size = max(1, min(int(limit or 50), 100))
        normalized_owner = (
            self.state_machine._coerce_owner(owner)
            if owner
            else None
        )
        normalized_stage = (
            self.state_machine._normalize_work_item_stage(
                stage,
                fallback="",
            )
            if stage
            else None
        )
        normalized_query = str(q or "").strip().casefold()
        cursor_payload = (
            self._decode_list_cursor(
                cursor,
                project_id=project_id,
                owner=normalized_owner,
                stage=normalized_stage,
                release_gate=release_gate,
                query=normalized_query,
            )
            if cursor
            else None
        )

        index = self.work_item_list_index
        if index is None:
            # Compatibility-only construction: keep response bounded even
            # though production composition always supplies the keyed index.
            states = [
                state
                for state in self.work_items.load_states().values()
                if state.organization_id == scope.organization_id
                and state.workspace_id == scope.workspace_id
                and state.project_id == project_id
            ]
            states.sort(
                key=lambda item: (-float(item.updated_at), item.ref)
            )
            states = states[:page_size]
            return {
                "items": [
                    self._work_item_list_public(state)
                    for state in states
                ],
                "pageSize": page_size,
                "nextCursor": None,
                "hasMore": False,
                "compatibilityFallback": True,
            }

        current_revision = index.revision(
            scope=scope,
            project_id=project_id,
        )
        if (
            cursor_payload is not None
            and cursor_payload.get("revision") != current_revision
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "work_item_cursor_stale",
                    "message": (
                        "Work Items changed after this cursor was issued; "
                        "restart pagination from the first page"
                    ),
                },
            )

        def predicate(state: Any) -> bool:
            if normalized_query:
                searchable = " ".join(
                    str(value or "")
                    for value in (
                        state.ref,
                        state.title,
                        state.current_owner,
                        state.next_owner,
                        state.next_action,
                        state.status_label,
                    )
                ).casefold()
                if normalized_query not in searchable:
                    return False
            if normalized_owner:
                candidate = self.state_machine._coerce_owner(
                    state.current_owner or state.next_owner
                )
                if candidate != normalized_owner:
                    return False
            if (
                normalized_stage
                and state.current_stage != normalized_stage
            ):
                return False
            if (
                release_gate is not None
                and state.release_gate is not release_gate
            ):
                return False
            return True

        get_state = self.work_items.get_state
        if get_state is None:
            loaded = self.work_items.load_states()
            get_state = loaded.get

        states, next_after, scan_truncated = index.page(
            scope=scope,
            project_id=project_id,
            after=(
                str(cursor_payload.get("after"))
                if cursor_payload is not None
                else None
            ),
            limit=page_size,
            get_state=get_state,
            predicate=predicate,
        )
        next_cursor = (
            self._list_cursor_payload(
                after=next_after,
                project_id=project_id,
                owner=normalized_owner,
                stage=normalized_stage,
                release_gate=release_gate,
                query=normalized_query,
                revision=current_revision,
            )
            if next_after
            else None
        )
        return {
            "items": [
                self._work_item_list_public(state)
                for state in states
            ],
            "pageSize": page_size,
            "nextCursor": next_cursor,
            "hasMore": bool(next_cursor),
            "scanTruncated": bool(scan_truncated),
            "revision": current_revision,
            "query": normalized_query,
            "sort": "updated_desc",
        }

    async def _sync_from_gitlab_async(
        self,
        scope: TenantScope | None = None,
        *,
        progress: Any | None = None,
        cancelled: Any | None = None,
    ) -> dict[str, int | bool]:
        """Provider-neutral GitLab discovery with off-loop projection.

        Provider I/O remains asynchronous. Deterministic projection and
        synchronous StateStore/index mutation run in worker threads so a large
        import cannot monopolize the Uvicorn event loop.
        """

        synced = 0
        processed = 0
        discovered = 0
        seen_refs: set[str] = set()
        was_cancelled = False

        async def report(current_external_id: str | None = None) -> None:
            if progress is None:
                return
            await asyncio.to_thread(
                progress,
                {
                    "discovered": discovered,
                    "processed": processed,
                    "synced": synced,
                    "unique_refs": len(seen_refs),
                    "current_external_id": current_external_id,
                },
            )

        async def cancellation_requested() -> bool:
            if cancelled is None:
                return False
            return bool(await asyncio.to_thread(cancelled))

        settings = self.gitlab_dependencies.load_routing_settings()
        allowed_project_ids: set[str] | None = None
        if scope is not None:
            allowed_project_ids = {
                project.id
                for project in self.work_items.load_projects()
                if project.organization_id == scope.organization_id
                and project.workspace_id == scope.workspace_id
            }
        for project_id, project_settings in settings.projects.items():
            if allowed_project_ids is not None and project_id not in allowed_project_ids:
                continue
            if not project_settings.enabled:
                continue
            if await cancellation_requested():
                was_cancelled = True
                break
            token = self.gitlab_dependencies.token_for_project(project_id)
            group = self.gitlab_dependencies.group_path(project_settings)
            if not token or not group:
                continue

            source = GitLabTaskSource(
                self.gitlab_dependencies.api_base_url,
                token,
                client=self.gitlab,
            )
            snapshots = await source.discover(scope=group)
            discovered += len(snapshots)
            await report()
            for snapshot in snapshots:
                if await cancellation_requested():
                    was_cancelled = True
                    break
                external_id = snapshot.identity.external_id
                await report(external_id)
                state = await asyncio.to_thread(
                    self.task_source_projector.upsert,
                    source,
                    snapshot,
                    project_id=project_id,
                )
                processed += 1
                synced += 1
                seen_refs.add(state.ref)
                await report(external_id)
            if was_cancelled:
                break
        await report()
        return {
            "discovered": discovered,
            "processed": processed,
            "synced": synced,
            "refs": len(seen_refs),
            "cancelled": was_cancelled,
        }

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
            self.gitlab_dependencies.api_base_url,
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
        append_event: Any | None = None,
    ) -> None:
        append = append_event or getattr(
            self.state_machine,
            "_append_work_item_event",
            None,
        )
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

    def _preserve_gitlab_closed_label_cleanup(
        self,
        state: Any,
        *,
        sync_writeback: Any | None = None,
    ) -> Any:
        if state is None or getattr(state, "current_stage", None) != "closed":
            return state
        labels = list(getattr(state, "labels", None) or [])
        if not any(
            str(label).startswith(("owner::", "status::")) for label in labels
        ):
            return state
        sync = sync_writeback or self.schedule_task_source_writeback
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
        append_event: Any | None = None,
        sync_writeback: Any | None = None,
    ) -> Any:
        """Compatibility hook used by GitLabService during migration."""

        source = GitLabWebhookTaskSource(
            self.gitlab_dependencies.api_base_url,
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
            self._append_legacy_gitlab_stale_event(
                state,
                payload=payload,
                append_event=append_event,
            )
        state = self._preserve_gitlab_closed_label_cleanup(
            state,
            sync_writeback=sync_writeback,
        )
        return state

    async def sync_from_gitlab(
        self,
        scope: TenantScope | None = None,
        *,
        progress: Any | None = None,
        cancelled: Any | None = None,
    ) -> dict[str, Any]:
        try:
            result = await self._sync_from_gitlab_async(
                scope,
                progress=progress,
                cancelled=cancelled,
            )
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
        *,
        continuity: Any,
        structured_handoff: Any,
        public_state: Any,
        publish_event: Any,
    ) -> dict[str, Any]:
        state = structured_handoff(ref, payload)
        state = await self.task_source_writeback.sync(state)
        public = public_state(state)
        await publish_event(
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
        return await self._handoff_with(
            ref,
            payload,
            continuity=self.continuity,
            structured_handoff=self.state_machine._structured_handoff,
            public_state=self.state_machine._work_item_state_public,
            publish_event=self.publish_event,
        )

    async def compatibility_handoff(
        self,
        ref: str,
        payload: WorkItemHandoffCreate,
    ) -> dict[str, Any]:
        return await self.handoff(ref, payload)

    async def _acknowledge_with(
        self,
        ref: str,
        payload: WorkItemAckCreate,
        *,
        continuity: Any,
        recovery: Any,
        structured_ack: Any,
        public_state: Any,
        split_brain_findings: Any,
        publish_event: Any,
    ) -> dict[str, Any]:
        state = structured_ack(ref, payload)
        state = await self.task_source_writeback.sync(state)
        public = public_state(state)
        await publish_event(
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
            continuity=self.continuity,
            recovery=self.recovery,
            structured_ack=self.state_machine._structured_ack,
            public_state=self.state_machine._work_item_state_public,
            split_brain_findings=(
                self.state_machine._work_item_split_brain_findings
            ),
            publish_event=self.publish_event,
        )

    async def compatibility_acknowledge(
        self,
        ref: str,
        payload: WorkItemAckCreate,
    ) -> dict[str, Any]:
        return await self.acknowledge(ref, payload)

    async def _progress_with(
        self,
        ref: str,
        payload: WorkItemProgressUpdate,
        *,
        continuity: Any,
        recovery: Any,
        structured_progress: Any,
        public_state: Any,
        split_brain_findings: Any,
        publish_event: Any,
    ) -> dict[str, Any]:
        state = structured_progress(ref, payload)
        state = await self.task_source_writeback.sync(state)
        public = public_state(state)
        await publish_event(
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
            continuity=self.continuity,
            recovery=self.recovery,
            structured_progress=self.state_machine._structured_progress,
            public_state=self.state_machine._work_item_state_public,
            split_brain_findings=(
                self.state_machine._work_item_split_brain_findings
            ),
            publish_event=self.publish_event,
        )

    async def compatibility_progress(
        self,
        ref: str,
        payload: WorkItemProgressUpdate,
    ) -> dict[str, Any]:
        return await self.progress(ref, payload)



def install_work_item_compatibility(
    app: Any,
    host: Any,
    service: WorkItemService,
) -> WorkItemCompatibilityFacade:
    """Attach the verified historical work-item surface at the edge only."""

    compatibility = WorkItemCompatibilityFacade(host, service)
    app.state.work_item_compatibility_service = compatibility
    host.create_work_item_handoff = compatibility.handoff
    host.ack_work_item_handoff = compatibility.acknowledge
    host.update_work_item_progress = compatibility.progress
    host._reconcile_task_source_event = service.reconcile_task_source_event
    host._append_work_item_event = compatibility.append_work_item_event
    host._upsert_work_item_state_from_gitlab_issue = (
        compatibility.project_gitlab_issue
    )
    host._upsert_work_item_state_from_gitlab_event = (
        compatibility.project_gitlab_event
    )
    host._sync_gitlab_issue_labels_from_work_item = (
        service.schedule_task_source_writeback
    )
    return compatibility
