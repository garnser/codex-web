from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException

from codex_web.models import TaskSourceIdentity, WorkItemEvent, WorkItemState
from codex_web.services.task_source_runtime import TaskSourceResolutionError
from codex_web.services.task_sources import (
    TaskSource,
    TaskSourceCapability,
    UnsupportedTaskSourceCapability,
)
from codex_web.services.work_item_dependencies import WorkItemRuntimeDependencies
from codex_web.services.work_item_execution import WorkItemExecutionLifecycleService
from codex_web.work_item_execution_models import WorkItemExecutionUpdate


class WorkItemOperatorService:
    """Explain and operate canonical work items without introducing shadow state."""

    DIAGNOSTIC_MARKERS = (
        "stale",
        "duplicate",
        "conflict",
        "failed",
        "ignored",
        "unsupported",
        "split_brain",
    )

    def __init__(self, work_items: Any) -> None:
        self.work_items = work_items
        self.state_machine = work_items.state_machine
        compatibility_host = getattr(work_items, "host", None)
        dependencies = getattr(work_items, "work_items", None)
        if dependencies is None:
            if compatibility_host is None:
                raise TypeError(
                    "WorkItemOperatorService requires work-item dependencies"
                )
            dependencies = WorkItemRuntimeDependencies.from_host(
                compatibility_host
            )
        self.dependencies = dependencies
        self.lifecycle = WorkItemExecutionLifecycleService(
            compatibility_host,
            self.state_machine,
            dependencies=dependencies,
        )
        self.sync_health = getattr(work_items, "sync_health", None)
        self.publish_event = getattr(work_items, "publish_event", None)
        if self.publish_event is None and compatibility_host is not None:
            hub = getattr(compatibility_host, "hub", None)
            self.publish_event = getattr(hub, "publish", None)
        self.gitlab_api_base = str(
            getattr(
                getattr(work_items, "gitlab_dependencies", None),
                "api_base_url",
                getattr(compatibility_host, "GITLAB_API_BASE", ""),
            )
            or ""
        )
        self.execution_contract = getattr(
            work_items,
            "execution_contract",
            None,
        )
        if self.execution_contract is None and compatibility_host is not None:
            self.execution_contract = getattr(
                compatibility_host,
                "_work_item_execution_contract",
                None,
            )
        continuity = getattr(work_items, "continuity", None)
        self.schedule_owner_dispatch = getattr(
            continuity,
            "schedule_actionable_owner_dispatch",
            None,
        )
        if (
            self.schedule_owner_dispatch is None
            and compatibility_host is not None
        ):
            self.schedule_owner_dispatch = getattr(
                compatibility_host,
                "_schedule_actionable_owner_dispatch",
                None,
            )

    def _projects(self) -> list[Any]:
        try:
            return list(self.dependencies.load_projects() or [])
        except Exception:
            return []

    def _project(self, project_id: str | None) -> Any | None:
        if not project_id:
            return None
        for project in self._projects():
            if getattr(project, "id", None) == project_id:
                return project
        return None

    @staticmethod
    def _project_source(project: Any | None) -> Any | None:
        return getattr(project, "authoritative_task_source", None) if project is not None else None

    def _source_for_configuration(
        self,
        project_id: str,
        configuration: Any,
        *,
        required: bool,
    ) -> TaskSource | None:
        now = time.time()
        probe = WorkItemState(
            ref=f"task-source-config:{project_id}",
            project_id=project_id,
            source_identity=TaskSourceIdentity(
                source_type=configuration.source_type,
                source_instance=configuration.source_instance,
                external_id="__configuration_probe__",
            ),
            last_meaningful_update_at=now,
            updated_at=now,
            created_at=now,
        )
        return self.work_items.task_source_registry.resolve(probe, required=required)

    def _source_for_state(self, state: WorkItemState, *, required: bool) -> TaskSource | None:
        return self.work_items.task_source_registry.resolve(state, required=required)

    @staticmethod
    def _capability_values(source: TaskSource | None) -> list[str]:
        if source is None:
            return []
        return sorted(capability.value for capability in source.capabilities.supported)

    def _sync_status(self) -> dict[str, Any]:
        if self.sync_health is None:
            return {
                "last_success_at": None,
                "last_error": None,
                "last_error_at": None,
                "consecutive_failures": 0,
            }
        return self.sync_health.snapshot()

    def task_source_catalog(self) -> dict[str, Any]:
        """Describe configured authoritative sources and their live capabilities."""
        items: list[dict[str, Any]] = []
        seen_types: set[str] = set()
        for project in self._projects():
            configuration = self._project_source(project)
            if configuration is None:
                continue
            source: TaskSource | None = None
            error: str | None = None
            try:
                source = self._source_for_configuration(project.id, configuration, required=False)
            except Exception as exc:
                error = str(exc)[:500]
            source_type = str(configuration.source_type)
            seen_types.add(source_type.casefold())
            items.append(
                {
                    "project_id": project.id,
                    "source_type": source_type,
                    "source_instance": configuration.source_instance,
                    "scope": configuration.scope,
                    "available": source is not None,
                    "capabilities": self._capability_values(source),
                    "error": error,
                }
            )
        # Advertise built-in adapters before configuration so the canonical
        # operator can create a binding without provider-specific shadow UI.
        builtin_catalog = {
            "gitlab": {
                "source_instance": self.gitlab_api_base,
                "capabilities": [
                    "comments",
                    "discovery",
                    "events",
                    "owner_write",
                    "read",
                    "state_write",
                ],
            },
            "jira": {
                "source_instance": "",
                "capabilities": [
                    "comments",
                    "create",
                    "discovery",
                    "events",
                    "owner_write",
                    "read",
                    "state_write",
                ],
            },
            "servicenow": {
                "source_instance": "",
                "capabilities": [
                    "comments",
                    "create",
                    "discovery",
                    "events",
                    "owner_write",
                    "read",
                    "state_write",
                ],
            },
            "github": {
                "source_instance": "https://api.github.com",
                "capabilities": ["comments", "create", "discovery", "events", "owner_write", "read", "state_write"],
            },
        }
        for source_type, descriptor in builtin_catalog.items():
            if source_type in seen_types:
                continue
            items.append(
                {
                    "project_id": None,
                    "source_type": source_type,
                    "source_instance": descriptor["source_instance"],
                    "scope": None,
                    "available": False,
                    "capabilities": descriptor["capabilities"],
                    "error": None,
                }
            )
        return {"items": items, "sync": self._sync_status()}

    def _execution_contract(self, state: WorkItemState) -> dict[str, Any] | None:
        builder = self.execution_contract
        if not callable(builder):
            return None
        try:
            contract = builder(state)
        except Exception as exc:
            return {"error": str(exc)[:500]}
        compact = getattr(contract, "compact_public", None)
        if callable(compact):
            return compact()
        dump = getattr(contract, "model_dump", None)
        return dump(mode="json", exclude_none=True) if callable(dump) else None

    def _source_summary(self, state: WorkItemState) -> dict[str, Any]:
        project = self._project(state.project_id)
        configuration = self._project_source(project)
        source: TaskSource | None = None
        error: str | None = None
        try:
            source = self._source_for_state(state, required=False)
        except Exception as exc:
            error = str(exc)[:500]
        identity = state.source_identity
        return {
            "configuration": (
                configuration.model_dump(mode="json")
                if configuration is not None and hasattr(configuration, "model_dump")
                else None
            ),
            "identity": identity.model_dump(mode="json") if identity is not None else None,
            "available": source is not None,
            "capabilities": self._capability_values(source),
            "projected_status_label": state.status_label,
            "projected_labels": list(state.labels),
            "last_projected_event_at": state.last_gitlab_event_at,
            "sync": self._sync_status(),
            "error": error,
        }

    def _diagnostics(self, state: WorkItemState, history: dict[str, Any]) -> list[dict[str, Any]]:
        diagnostics: list[dict[str, Any]] = []
        for finding in self.state_machine._work_item_split_brain_findings(state):
            diagnostics.append(
                {
                    "kind": "split_brain",
                    "severity": "error",
                    "message": finding,
                    "created_at": state.updated_at,
                }
            )
        failure = state.execution.failure_reason
        if failure is not None:
            diagnostics.append(
                {
                    "kind": "execution_failure",
                    "severity": "error",
                    "message": failure.message,
                    "code": failure.code,
                    "category": failure.category,
                    "created_at": failure.recorded_at,
                }
            )
        for event in history.get("items", []):
            event_type = str(event.get("event_type") or "")
            if not any(marker in event_type.casefold() for marker in self.DIAGNOSTIC_MARKERS):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            message = (
                payload.get("error")
                or payload.get("message")
                or payload.get("reason")
                or event_type.replace("_", " ")
            )
            diagnostics.append(
                {
                    "kind": event_type,
                    "severity": "warning",
                    "message": str(message),
                    "created_at": event.get("created_at"),
                }
            )
        diagnostics.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        return diagnostics[:30]

    def _actions(self, state: WorkItemState, source: TaskSource | None) -> dict[str, Any]:
        retry_allowed = bool(
            state.current_stage != "closed"
            and (state.current_owner or state.next_owner)
            and state.execution.retry.attempt < state.execution.retry.policy.max_attempts
        )
        return {
            "retry": {
                "allowed": retry_allowed,
                "reason": None if retry_allowed else "closed, ownerless, or retry policy exhausted",
            },
            "reconcile": {
                "allowed": bool(
                    source
                    and state.source_identity
                    and source.capabilities.supports(TaskSourceCapability.READ)
                ),
                "reason": None if source else "authoritative source adapter unavailable",
            },
        }

    def detail(self, ref: str) -> dict[str, Any]:
        state = self.state_machine._work_item_state(ref)
        history = self.lifecycle.history(ref, limit=200)
        source: TaskSource | None = None
        try:
            source = self._source_for_state(state, required=False)
        except Exception:
            source = None
        project = self._project(state.project_id)
        return {
            "item": self.state_machine._work_item_state_public(state),
            "external": self._source_summary(state),
            "execution_contract": self._execution_contract(state),
            "execution_policy": {
                "sandbox": getattr(project, "sandbox", None),
                "approval_policy": getattr(project, "approval_policy", None),
                "source": "project-default" if project is not None else None,
            },
            "history": history,
            "diagnostics": self._diagnostics(state, history),
            "actions": self._actions(state, source),
        }

    async def retry(
        self,
        ref: str,
        *,
        actor: str | None,
        reason: str | None,
    ) -> dict[str, Any]:
        state = self.state_machine._work_item_state(ref)
        actions = self._actions(state, self._source_for_state(state, required=False))
        if not actions["retry"]["allowed"]:
            raise HTTPException(
                status_code=409,
                detail={"code": "work_item_retry_not_allowed", "reason": actions["retry"]["reason"]},
            )
        attempt = state.execution.retry.attempt + 1
        self.lifecycle.update(
            ref,
            WorkItemExecutionUpdate(
                actor=actor,
                source="operator-ui",
                reason=reason or "operator retry",
                retry_attempt=attempt,
                clear_failure=True,
            ),
        )
        state = self.state_machine._work_item_state(ref)
        if callable(self.schedule_owner_dispatch):
            self.schedule_owner_dispatch(
                state,
                source="operator-retry",
                actor=actor,
            )
        return self.detail(ref)

    async def reconcile(
        self,
        ref: str,
        *,
        actor: str | None,
        reason: str | None,
    ) -> dict[str, Any]:
        state = self.state_machine._work_item_state(ref)
        try:
            source = self._source_for_state(state, required=True)
            assert source is not None and state.source_identity is not None
            source.capabilities.require(TaskSourceCapability.READ)
            snapshot = await source.read(state.source_identity)
            state = self.work_items.task_source_projector.upsert(
                source,
                snapshot,
                project_id=state.project_id or "home",
            )
        except UnsupportedTaskSourceCapability as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "task_source_capability_unsupported", "capability": exc.capability.value},
            ) from exc
        except TaskSourceResolutionError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "task_source_unavailable", "message": str(exc)},
            ) from exc
        self.state_machine._append_work_item_event(
            WorkItemEvent(
                ref=state.ref,
                event_type="operator_reconciled",
                created_at=time.time(),
                actor=actor,
                source="operator-ui",
                reason=reason or "operator reconcile",
                payload={"source_type": state.source_identity.source_type if state.source_identity else None},
            )
        )
        return self.detail(ref)

    async def sync_project(
        self,
        project_id: str,
        *,
        actor: str | None,
        reason: str | None,
    ) -> dict[str, Any]:
        project = self._project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail={"code": "project_not_found"})
        configuration = self._project_source(project)
        if configuration is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "authoritative_task_source_not_configured"},
            )
        try:
            source = self._source_for_configuration(project_id, configuration, required=True)
            assert source is not None
            source.capabilities.require(TaskSourceCapability.DISCOVERY)
            snapshots = await source.discover(scope=configuration.scope)
        except UnsupportedTaskSourceCapability as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "task_source_capability_unsupported", "capability": exc.capability.value},
            ) from exc
        except TaskSourceResolutionError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "task_source_unavailable", "message": str(exc)},
            ) from exc

        refs: list[str] = []
        for snapshot in snapshots:
            state = self.work_items.task_source_projector.upsert(
                source,
                snapshot,
                project_id=project_id,
            )
            refs.append(state.ref)
            self.state_machine._append_work_item_event(
                WorkItemEvent(
                    ref=state.ref,
                    event_type="operator_source_sync",
                    created_at=time.time(),
                    actor=actor,
                    source="operator-ui",
                    reason=reason or "operator source sync",
                    payload={"source_type": configuration.source_type},
                )
            )

        if self.sync_health is not None:
            self.sync_health.record_success()
        if callable(self.publish_event):
            await self.publish_event(
                {
                    "type": "work-item.sync",
                    "project_id": project_id,
                    "synced": len(refs),
                    "refs": len(set(refs)),
                }
            )
        return {
            "ok": True,
            "project_id": project_id,
            "synced": len(refs),
            "refs": len(set(refs)),
            "sync": self._sync_status(),
        }
