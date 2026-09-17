from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from codex_web.compatibility import ContractCompatibilityError, TASK_SOURCE_CONTRACT
from codex_web.models import WorkItemState
from codex_web.services.task_sources import (
    TaskSource,
    TaskSourceCapability,
    TaskSourceSnapshot,
)


TaskSourceFactory = Callable[[WorkItemState], TaskSource | None]


class TaskSourceResolutionError(LookupError):
    pass


class TaskSourceRegistry:
    """Resolve authoritative task-source adapters without provider branching."""

    def __init__(self) -> None:
        self._factories: dict[str, TaskSourceFactory] = {}

    def register(self, source_type: str, factory: TaskSourceFactory) -> None:
        key = str(source_type or "").strip().casefold()
        if not key:
            raise ValueError("task-source type must not be empty")
        self._factories[key] = factory

    def resolve(self, state: WorkItemState, *, required: bool = False) -> TaskSource | None:
        identity = getattr(state, "source_identity", None)
        if identity is None:
            if required:
                raise TaskSourceResolutionError("Work item has no authoritative task-source identity")
            return None
        factory = self._factories.get(identity.source_type.casefold())
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
        if source.source_type.casefold() != identity.source_type.casefold():
            raise TaskSourceResolutionError("Resolved task-source type does not match persisted identity")
        if source.source_instance.rstrip("/") != identity.source_instance.rstrip("/"):
            raise TaskSourceResolutionError("Resolved task-source instance does not match persisted identity")
        # Adapters created before this contract was introduced are treated as
        # task-source 1.0 during the migration window. Once every in-tree and
        # supported extension adapter declares its version, this fallback can
        # be deprecated through the normal compatibility policy.
        version = str(
            getattr(source, "contract_version", TASK_SOURCE_CONTRACT.current)
            or TASK_SOURCE_CONTRACT.current
        ).strip()
        try:
            TASK_SOURCE_CONTRACT.require(version)
        except (ContractCompatibilityError, ValueError) as exc:
            raise TaskSourceResolutionError(
                f"Task-source adapter {identity.source_type!r} has incompatible contract version {version!r}"
            ) from exc
        return source


class TaskSourceWritebackService:
    """Project canonical work state to its authoritative source through capabilities."""

    def __init__(self, host: Any, registry: TaskSourceRegistry) -> None:
        self.host = host
        self.registry = registry

    def _save_snapshot(self, state: WorkItemState, snapshot: TaskSourceSnapshot) -> WorkItemState:
        state.source_identity = snapshot.identity
        state.labels = list(snapshot.labels)
        state.updated_at = max(state.updated_at, time.time())
        states = self.host._load_work_item_states()
        states[state.ref] = state
        self.host._save_work_item_states(states)
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


def install_task_source_runtime(app: Any, host: Any) -> tuple[TaskSourceRegistry, TaskSourceWritebackService]:
    existing_registry = getattr(app.state, "task_source_registry", None)
    existing_writeback = getattr(app.state, "task_source_writeback_service", None)
    if isinstance(existing_registry, TaskSourceRegistry) and isinstance(
        existing_writeback, TaskSourceWritebackService
    ):
        return existing_registry, existing_writeback

    registry = TaskSourceRegistry()
    writeback = TaskSourceWritebackService(host, registry)
    app.state.task_source_registry = registry
    app.state.task_source_writeback_service = writeback
    return registry, writeback
