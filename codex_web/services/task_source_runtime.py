from __future__ import annotations

import asyncio
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
    TaskSourceCombinedWriteCapable,
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
    """Coalesced projection of canonical Work Item state to TaskSources."""

    MAX_RETRIES = 5

    def __init__(
        self,
        host: Any | None,
        registry: TaskSourceRegistry,
        *,
        dependencies: WorkItemRuntimeDependencies | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if dependencies is None:
            if host is None:
                raise TypeError(
                    "TaskSourceWritebackService requires work-item dependencies"
                )
            dependencies = WorkItemRuntimeDependencies.from_host(host)
        self.dependencies = dependencies
        self.registry = registry
        self.event_sink = event_sink or (
            getattr(host, "_append_bot_event", None)
            if host is not None
            else None
        ) or (lambda _event: None)
        self._desired: dict[tuple[str, ...], WorkItemState] = {}
        self._desired_at: dict[tuple[str, ...], float] = {}
        self._generation: dict[tuple[str, ...], int] = {}
        self._tasks: dict[
            tuple[str, ...],
            asyncio.Task[None],
        ] = {}
        self._last_applied: dict[
            tuple[str, ...],
            tuple[str | None, str],
        ] = {}
        self._stats: dict[str, int] = {
            "scheduled": 0,
            "coalesced": 0,
            "skipped": 0,
            "applied": 0,
            "retried": 0,
            "failed": 0,
            "providerReads": 0,
            "providerWrites": 0,
        }
        self._last_error: dict[tuple[str, ...], str] = {}
        self._backoff_until: dict[tuple[str, ...], float] = {}

    @staticmethod
    def _desired_fingerprint(
        state: WorkItemState,
    ) -> tuple[str | None, str]:
        return state.current_owner, state.current_stage

    @staticmethod
    def _key(state: WorkItemState) -> tuple[str, ...] | None:
        identity = getattr(state, "source_identity", None)
        if identity is None:
            return None
        return (
            str(state.organization_id or ""),
            str(state.workspace_id or ""),
            str(identity.source_type or "").casefold(),
            str(identity.source_instance or "").rstrip("/"),
            str(identity.external_id or ""),
        )

    def _copy_state(self, state: WorkItemState) -> WorkItemState:
        return state.model_copy(deep=True)

    def _is_current(
        self,
        key: tuple[str, ...],
        generation: int,
    ) -> bool:
        return self._generation.get(key) == generation

    def _save_snapshot(
        self,
        state: WorkItemState,
        snapshot: TaskSourceSnapshot,
    ) -> WorkItemState:
        # Provider feedback must never restore an older scheduled owner/stage.
        # Merge provider metadata into the latest canonical row.
        current = (
            self.dependencies.get_state(state.ref)
            if self.dependencies.get_state is not None
            else None
        )
        target = (
            current.model_copy(deep=True)
            if current is not None
            else state.model_copy(deep=True)
        )
        target.source_identity = snapshot.identity
        target.labels = list(snapshot.labels)
        target.updated_at = max(target.updated_at, time.time())
        if self.dependencies.save_state is not None:
            self.dependencies.save_state(target)
            return target
        states = self.dependencies.load_states()
        latest = states.get(target.ref)
        if latest is not None:
            latest.source_identity = snapshot.identity
            latest.labels = list(snapshot.labels)
            latest.updated_at = max(latest.updated_at, time.time())
            target = latest
        states[target.ref] = target
        self.dependencies.save_states(states)
        return target

    async def _sync_once(
        self,
        state: WorkItemState,
        *,
        key: tuple[str, ...] | None = None,
        generation: int | None = None,
    ) -> WorkItemState:
        source = self.registry.resolve(state, required=False)
        identity = getattr(state, "source_identity", None)
        if source is None or identity is None:
            self._stats["skipped"] += 1
            return state

        desired = self._desired_fingerprint(state)
        if (
            key is not None
            and self._last_applied.get(key) == desired
        ):
            self._stats["skipped"] += 1
            return state

        combined = (
            source
            if isinstance(source, TaskSourceCombinedWriteCapable)
            else None
        )
        if (
            combined is not None
            and source.capabilities.supports(TaskSourceCapability.READ)
            and source.capabilities.supports(
                TaskSourceCapability.OWNER_WRITE
            )
            and source.capabilities.supports(
                TaskSourceCapability.STATE_WRITE
            )
        ):
            current = await source.read(identity)
            self._stats["providerReads"] += 1
            if (
                key is not None
                and generation is not None
                and not self._is_current(key, generation)
            ):
                self._stats["coalesced"] += 1
                return state
            result = await combined.write_projection(
                identity,
                current,
                owner=state.current_owner,
                stage=state.current_stage,
            )
            if result.mutated:
                self._stats["providerWrites"] += 1
                self._stats["applied"] += 1
            else:
                self._stats["skipped"] += 1
            if key is not None:
                self._last_applied[key] = desired
            return self._save_snapshot(state, result.snapshot)

        snapshot: TaskSourceSnapshot | None = None
        if source.capabilities.supports(TaskSourceCapability.OWNER_WRITE):
            snapshot = await source.write_owner(
                identity,
                state.current_owner,
            )
            self._stats["providerWrites"] += 1
            identity = snapshot.identity
        if source.capabilities.supports(TaskSourceCapability.STATE_WRITE):
            if (
                key is not None
                and generation is not None
                and not self._is_current(key, generation)
            ):
                self._stats["coalesced"] += 1
                return state
            snapshot = await source.write_state(
                identity,
                state.current_stage,
            )
            self._stats["providerWrites"] += 1
        if snapshot is None:
            self._stats["skipped"] += 1
            return state
        self._stats["applied"] += 1
        if key is not None:
            self._last_applied[key] = desired
        return self._save_snapshot(state, snapshot)

    async def sync(self, state: WorkItemState) -> WorkItemState:
        """Synchronously project one canonical state update.

        Direct API flows still await provider synchronization, but provider
        adapters that support combined projection use one read plus at most one
        mutation and skip unchanged desired state.
        """
        key = self._key(state)
        return await self._sync_once(state, key=key)

    def schedule(self, state: WorkItemState) -> WorkItemState:
        """Coalesce asynchronous writeback by authoritative source identity."""
        key = self._key(state)
        if key is None:
            return state
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return state

        self._stats["scheduled"] += 1
        if key in self._desired:
            self._stats["coalesced"] += 1
        self._desired[key] = self._copy_state(state)
        self._desired_at.setdefault(key, time.time())
        generation = self._generation.get(key, 0) + 1
        self._generation[key] = generation

        task = self._tasks.get(key)
        if task is None or task.done():
            self._tasks[key] = asyncio.create_task(
                self._run_key(key),
                name=(
                    "task-source-writeback:"
                    + ":".join(key[-3:])
                ),
            )
        return state

    async def _run_key(self, key: tuple[str, ...]) -> None:
        retry = 0
        # One event-loop turn intentionally collapses synchronous update bursts.
        await asyncio.sleep(0)
        try:
            while key in self._desired:
                generation = self._generation[key]
                desired = self._copy_state(self._desired[key])
                try:
                    await self._sync_once(
                        desired,
                        key=key,
                        generation=generation,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    retry += 1
                    self._stats["failed"] += 1
                    self._last_error[key] = str(exc)[:500]
                    if retry > self.MAX_RETRIES:
                        self.event_sink(
                            {
                                "type": "task_source_writeback_failed",
                                "ref": desired.ref,
                                "attempts": retry,
                                "error": str(exc)[:500],
                            }
                        )
                        return
                    self._stats["retried"] += 1
                    delay = min(5.0, 0.25 * (2 ** (retry - 1)))
                    self._backoff_until[key] = time.time() + delay
                    await asyncio.sleep(delay)
                    continue

                retry = 0
                self._last_error.pop(key, None)
                self._backoff_until.pop(key, None)
                if self._generation.get(key) != generation:
                    continue
                self._desired.pop(key, None)
                self._desired_at.pop(key, None)
                return
        finally:
            current = asyncio.current_task()
            if self._tasks.get(key) is current:
                self._tasks.pop(key, None)

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            **self._stats,
            "queueDepth": len(self._desired),
            "activeTasks": sum(
                1 for task in self._tasks.values() if not task.done()
            ),
            "oldestAgeSeconds": (
                max(
                    0.0,
                    now - min(self._desired_at.values()),
                )
                if self._desired_at
                else 0.0
            ),
            "backoffItems": sum(
                1
                for until in self._backoff_until.values()
                if until > now
            ),
            "lastErrors": {
                "|".join(key): value
                for key, value in self._last_error.items()
            },
            "items": {
                "|".join(key): {
                    "generation": self._generation.get(key, 0),
                    "lastDesired": (
                        {
                            "owner": desired.current_owner,
                            "stage": desired.current_stage,
                            "updatedAt": desired.updated_at,
                        }
                        if desired is not None
                        else None
                    ),
                    "lastApplied": (
                        {
                            "owner": applied[0],
                            "stage": applied[1],
                        }
                        if (
                            applied := self._last_applied.get(key)
                        )
                        is not None
                        else None
                    ),
                    "backoffUntil": self._backoff_until.get(key),
                }
                for key, desired in self._desired.items()
            },
        }

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

    def supports(
        self,
        state: WorkItemState,
        capability: TaskSourceCapability,
    ) -> bool:
        source = self.registry.resolve(state, required=False)
        return bool(
            source and source.capabilities.supports(capability)
        )


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
