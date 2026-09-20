from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from codex_web.compatibility import ContractCompatibilityError, TASK_SOURCE_CONTRACT
from codex_web.identity import TenantScope
from codex_web.models import TaskSourceConfiguration, WorkItemState
from codex_web.services.work_item_dependencies import WorkItemRuntimeDependencies
from codex_web.services.task_sources import (
    TaskSource,
    TaskSourceCapability,
    TaskSourceSnapshot,
)


TaskSourceFactory = Callable[[WorkItemState], TaskSource | None]
TaskSourceProjectFactory = Callable[
    [TaskSourceConfiguration, str, TenantScope],
    TaskSource | None,
]


class TaskSourceResolutionError(LookupError):
    pass


class TaskSourceRegistry:
    """Resolve authoritative task-source adapters without provider branching."""

    def __init__(self) -> None:
        self._factories: dict[str, TaskSourceFactory] = {}
        self._tenant_factories: dict[
            tuple[str, str, str],
            TaskSourceFactory,
        ] = {}
        self._project_factories: dict[str, TaskSourceProjectFactory] = {}
        self._tenant_project_factories: dict[
            tuple[str, str, str],
            TaskSourceProjectFactory,
        ] = {}

    @staticmethod
    def _source_key(source_type: str) -> str:
        key = str(source_type or "").strip().casefold()
        if not key:
            raise ValueError("task-source type must not be empty")
        return key

    def register(self, source_type: str, factory: TaskSourceFactory) -> None:
        self._factories[self._source_key(source_type)] = factory

    def register_project(
        self,
        source_type: str,
        factory: TaskSourceProjectFactory,
    ) -> None:
        """Register resolution for project bindings before a Work Item exists."""
        self._project_factories[self._source_key(source_type)] = factory

    def register_tenant(
        self,
        scope: TenantScope,
        source_type: str,
        factory: TaskSourceFactory,
    ) -> None:
        key = (
            scope.organization_id,
            scope.workspace_id,
            self._source_key(source_type),
        )
        self._tenant_factories[key] = factory

    def register_project_tenant(
        self,
        scope: TenantScope,
        source_type: str,
        factory: TaskSourceProjectFactory,
    ) -> None:
        self._tenant_project_factories[
            (
                scope.organization_id,
                scope.workspace_id,
                self._source_key(source_type),
            )
        ] = factory

    def unregister_tenant(
        self,
        scope: TenantScope,
        source_type: str,
    ) -> None:
        key = (
            scope.organization_id,
            scope.workspace_id,
            self._source_key(source_type),
        )
        self._tenant_factories.pop(key, None)
        self._tenant_project_factories.pop(key, None)

    @staticmethod
    def _validate_resolved(
        source: TaskSource,
        *,
        source_type: str,
        source_instance: str,
    ) -> TaskSource:
        if source.source_type.casefold() != source_type.casefold():
            raise TaskSourceResolutionError(
                "Resolved task-source type does not match configured identity"
            )
        if source.source_instance.rstrip("/") != source_instance.rstrip("/"):
            raise TaskSourceResolutionError(
                "Resolved task-source instance does not match configured identity"
            )
        version = str(
            getattr(source, "contract_version", TASK_SOURCE_CONTRACT.current)
            or TASK_SOURCE_CONTRACT.current
        ).strip()
        try:
            TASK_SOURCE_CONTRACT.require(version)
        except (ContractCompatibilityError, ValueError) as exc:
            raise TaskSourceResolutionError(
                f"Task-source adapter {source_type!r} has incompatible contract version {version!r}"
            ) from exc
        return source

    def resolve_project(
        self,
        configuration: TaskSourceConfiguration,
        *,
        project_id: str,
        scope: TenantScope,
        required: bool = False,
    ) -> TaskSource | None:
        """Resolve the configured authoritative source before an item exists."""
        source_key = self._source_key(configuration.source_type)
        factory = self._tenant_project_factories.get(
            (scope.organization_id, scope.workspace_id, source_key)
        )
        if factory is None:
            factory = self._project_factories.get(source_key)
        if factory is None:
            if required:
                raise TaskSourceResolutionError(
                    f"No project task-source adapter is registered for {configuration.source_type!r}"
                )
            return None
        source = factory(configuration, project_id, scope)
        if source is None:
            if required:
                raise TaskSourceResolutionError(
                    f"Task-source adapter {configuration.source_type!r} is not currently available"
                )
            return None
        return self._validate_resolved(
            source,
            source_type=configuration.source_type,
            source_instance=configuration.source_instance,
        )

    def resolve(self, state: WorkItemState, *, required: bool = False) -> TaskSource | None:
        identity = getattr(state, "source_identity", None)
        if identity is None:
            if required:
                raise TaskSourceResolutionError("Work item has no authoritative task-source identity")
            return None
        source_key = identity.source_type.casefold()
        factory = self._tenant_factories.get(
            (
                state.organization_id,
                state.workspace_id,
                source_key,
            )
        )
        if factory is None:
            factory = self._factories.get(source_key)
        if factory is None:
            if required:
                raise TaskSourceResolutionError(
                    f"No task-source adapter is registered for {identity.source_type!r}"
                )
            return None
        source = factory(state)
        if source is None:
            if required:
                raise TaskSourceResolutionError(
                    f"Task-source adapter {identity.source_type!r} is not currently available"
                )
            return None
        return self._validate_resolved(
            source,
            source_type=identity.source_type,
            source_instance=identity.source_instance,
        )


class TaskSourceWritebackService:
    """Project canonical work state to its authoritative source through capabilities."""

    def __init__(
        self,
        host: Any | None,
        registry: TaskSourceRegistry,
        *,
        dependencies: WorkItemRuntimeDependencies | None = None,
    ) -> None:
        if dependencies is None:
            if host is None:
                raise TypeError(
                    "TaskSourceWritebackService requires work-item dependencies"
                )
            dependencies = WorkItemRuntimeDependencies.from_host(host)
        self.dependencies = dependencies
        self.registry = registry

    def _save_snapshot(
        self,
        state: WorkItemState,
        snapshot: TaskSourceSnapshot,
    ) -> WorkItemState:
        state.source_identity = snapshot.identity
        state.labels = list(snapshot.labels)
        state.updated_at = max(state.updated_at, time.time())
        states = self.dependencies.load_states()
        states[state.ref] = state
        self.dependencies.save_states(states)
        return state

    async def sync(self, state: WorkItemState) -> WorkItemState:
        """Best-effort canonical owner/state projection for normal work updates.

        A temporarily unavailable configured provider must not make canonical
        progress impossible. Direct provider operations such as comments remain
        strict and fail visibly through ``required=True`` resolution.
        """

        source = self.registry.resolve(state, required=False)
        identity = getattr(state, "source_identity", None)
        if source is None or identity is None:
            return state

        snapshot: TaskSourceSnapshot | None = None
        if source.capabilities.supports(TaskSourceCapability.OWNER_WRITE):
            snapshot = await source.write_owner(identity, state.current_owner)
            identity = snapshot.identity
        if source.capabilities.supports(TaskSourceCapability.STATE_WRITE):
            snapshot = await source.write_state(identity, state.current_stage)
        return self._save_snapshot(state, snapshot) if snapshot is not None else state

    async def add_comment(self, state: WorkItemState, body: str) -> None:
        source = self.registry.resolve(state, required=True)
        identity = getattr(state, "source_identity", None)
        assert source is not None and identity is not None
        source.capabilities.require(TaskSourceCapability.COMMENTS)
        await source.add_comment(identity, body)

    async def attach_artifact(self, state: WorkItemState, url: str) -> None:
        source = self.registry.resolve(state, required=True)
        identity = getattr(state, "source_identity", None)
        assert source is not None and identity is not None
        source.capabilities.require(TaskSourceCapability.ARTIFACT_LINKS)
        await source.attach_artifact(identity, url)

    def supports(self, state: WorkItemState, capability: TaskSourceCapability) -> bool:
        source = self.registry.resolve(state, required=False)
        return bool(source and source.capabilities.supports(capability))


def install_task_source_runtime(
    app: Any,
    host: Any,
    *,
    dependencies: WorkItemRuntimeDependencies | None = None,
) -> tuple[TaskSourceRegistry, TaskSourceWritebackService]:
    existing_registry = getattr(app.state, "task_source_registry", None)
    existing_writeback = getattr(app.state, "task_source_writeback_service", None)
    if isinstance(existing_registry, TaskSourceRegistry) and isinstance(
        existing_writeback, TaskSourceWritebackService
    ):
        return existing_registry, existing_writeback

    registry = TaskSourceRegistry()
    writeback = TaskSourceWritebackService(
        host,
        registry,
        dependencies=dependencies,
    )
    app.state.task_source_registry = registry
    app.state.task_source_writeback_service = writeback
    return registry, writeback
