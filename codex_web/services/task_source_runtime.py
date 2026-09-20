from __future__ import annotations

import asyncio
import contextlib
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
    TaskSourceProjectionWriteCapable,
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
    """Coalesced canonical projection to authoritative TaskSource adapters."""

    MAX_RETRIES = 3
    BASE_RETRY_SECONDS = 0.1
    MAX_RETRY_SECONDS = 2.0

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
        self._pending: dict[
            str,
            tuple[WorkItemState, float, int],
        ] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._generation: dict[str, int] = {}
        self._metrics: dict[str, Any] = {
            "scheduled": 0,
            "coalesced": 0,
            "skipped": 0,
            "applied": 0,
            "retried": 0,
            "failures": 0,
            "providerReads": 0,
            "providerWrites": 0,
        }
        self._last_desired_revision: dict[str, str] = {}
        self._last_applied_revision: dict[str, str] = {}
        self._last_failure: dict[str, str] = {}
        self._backoff_until: dict[str, float] = {}

    @staticmethod
    def _key(state: WorkItemState) -> str:
        identity = getattr(state, "source_identity", None)
        if identity is None:
            return f"work-item:{state.ref}"
        return "|".join(
            (
                str(identity.source_type),
                str(identity.source_instance),
                str(identity.external_id),
            )
        )

    @staticmethod
    def _desired_revision(state: WorkItemState) -> str:
        return "|".join(
            (
                f"{float(state.updated_at):.6f}",
                str(state.current_owner or ""),
                str(state.current_stage or ""),
            )
        )

    def status(self) -> dict[str, Any]:
        now = time.time()
        pending_times = [
            item[1] for item in self._pending.values()
        ]
        active = sum(
            1 for task in self._tasks.values() if not task.done()
        )
        return {
            **self._metrics,
            "queueDepth": len(self._pending),
            "active": active,
            "oldestPendingAgeSeconds": (
                max(0.0, now - min(pending_times))
                if pending_times
                else 0.0
            ),
            "lastDesiredRevision": dict(
                self._last_desired_revision
            ),
            "lastAppliedRevision": dict(
                self._last_applied_revision
            ),
            "lastFailure": dict(self._last_failure),
            "backoff": {
                key: max(0.0, deadline - now)
                for key, deadline in self._backoff_until.items()
                if deadline > now
            },
        }

    def _latest_state(
        self,
        state: WorkItemState,
    ) -> WorkItemState:
        if self.dependencies.get_state is not None:
            latest = self.dependencies.get_state(state.ref)
            if latest is not None:
                return latest
        states = self.dependencies.load_states()
        return states.get(state.ref) or state

    def _save_snapshot(
        self,
        state: WorkItemState,
        snapshot: TaskSourceSnapshot,
    ) -> WorkItemState:
        # Provider completion may race a newer canonical update. Merge only
        # provider metadata into the latest record; never replace owner/stage
        # from the stale scheduled snapshot.
        latest = self._latest_state(state)
        latest.source_identity = snapshot.identity
        latest.labels = list(snapshot.labels)
        if self.dependencies.save_state is not None:
            self.dependencies.save_state(latest)
            return latest
        states = self.dependencies.load_states()
        states[latest.ref] = latest
        self.dependencies.save_states(states)
        return latest

    async def _sync_once(
        self,
        state: WorkItemState,
    ) -> tuple[WorkItemState, bool, int, int]:
        source = self.registry.resolve(state, required=False)
        identity = getattr(state, "source_identity", None)
        if source is None or identity is None:
            return state, False, 0, 0

        if isinstance(source, TaskSourceProjectionWriteCapable):
            result = await source.write_projection(
                identity,
                owner=state.current_owner,
                state=state.current_stage,
            )
            merged = self._save_snapshot(state, result.snapshot)
            return (
                merged,
                result.changed,
                result.provider_reads,
                result.provider_writes,
            )

        provider_reads = 0
        provider_writes = 0
        current = await source.read(identity)
        provider_reads += 1
        # Delta comparison must reflect provider facts, not the desired
        # canonical stage. Passing the desired stage as a projection hint can
        # make a less-specific provider snapshot appear synchronized.
        projection = source.project(
            current,
            current_stage=None,
        )
        snapshot = current

        if (
            source.capabilities.supports(
                TaskSourceCapability.OWNER_WRITE
            )
            and projection.owner != state.current_owner
        ):
            snapshot = await source.write_owner(
                snapshot.identity,
                state.current_owner,
            )
            provider_reads += 1
            provider_writes += 1
            projection = source.project(
                snapshot,
                current_stage=None,
            )

        if (
            source.capabilities.supports(
                TaskSourceCapability.STATE_WRITE
            )
            and projection.stage != state.current_stage
        ):
            snapshot = await source.write_state(
                snapshot.identity,
                state.current_stage,
            )
            provider_reads += 1
            provider_writes += 1

        merged = self._save_snapshot(state, snapshot)
        return (
            merged,
            provider_writes > 0,
            provider_reads,
            provider_writes,
        )

    async def sync(self, state: WorkItemState) -> WorkItemState:
        merged, changed, reads, writes = await self._sync_once(state)
        self._metrics["providerReads"] += reads
        self._metrics["providerWrites"] += writes
        if changed:
            self._metrics["applied"] += 1
        else:
            self._metrics["skipped"] += 1
        return merged

    def schedule(
        self,
        state: WorkItemState,
        *,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> WorkItemState:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return state

        snapshot = state.model_copy(deep=True)
        key = self._key(snapshot)
        generation = self._generation.get(key, 0) + 1
        self._generation[key] = generation
        self._pending[key] = (
            snapshot,
            time.time(),
            generation,
        )
        self._metrics["scheduled"] += 1
        self._last_desired_revision[key] = (
            self._desired_revision(snapshot)
        )

        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            self._metrics["coalesced"] += 1
            return state

        task = loop.create_task(
            self._run_key(key, event_sink=event_sink),
            name=f"task-source-writeback:{snapshot.ref}",
        )
        self._tasks[key] = task

        def done(completed: asyncio.Task[None]) -> None:
            if self._tasks.get(key) is completed:
                self._tasks.pop(key, None)
            with contextlib.suppress(
                asyncio.CancelledError,
                Exception,
            ):
                completed.result()

        task.add_done_callback(done)
        return state

    async def _run_key(
        self,
        key: str,
        *,
        event_sink: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        while True:
            pending = self._pending.pop(key, None)
            if pending is None:
                return
            state, _queued_at, generation = pending
            revision = self._desired_revision(state)
            attempts = 0

            while True:
                newer = self._pending.get(key)
                if newer is not None and newer[2] > generation:
                    break
                try:
                    merged, changed, reads, writes = (
                        await self._sync_once(state)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._metrics["failures"] += 1
                    self._last_failure[key] = str(exc)[:500]
                    attempts += 1
                    newer = self._pending.get(key)
                    if newer is not None and newer[2] > generation:
                        break
                    if attempts >= self.MAX_RETRIES:
                        if event_sink is not None:
                            event_sink(
                                {
                                    "type": "task_source_writeback_failed",
                                    "ref": state.ref,
                                    "error": str(exc)[:500],
                                    "attempts": attempts,
                                }
                            )
                        break
                    delay = min(
                        self.MAX_RETRY_SECONDS,
                        self.BASE_RETRY_SECONDS
                        * (2 ** (attempts - 1)),
                    )
                    self._metrics["retried"] += 1
                    self._backoff_until[key] = (
                        time.time() + delay
                    )
                    await asyncio.sleep(delay)
                    continue

                self._metrics["providerReads"] += reads
                self._metrics["providerWrites"] += writes
                if changed:
                    self._metrics["applied"] += 1
                else:
                    self._metrics["skipped"] += 1
                self._last_applied_revision[key] = revision
                self._last_failure.pop(key, None)
                self._backoff_until.pop(key, None)
                # If canonical state changed during the provider call, the
                # newer scheduled snapshot remains pending for the next pass.
                del merged
                break

    async def add_comment(
        self,
        state: WorkItemState,
        body: str,
    ) -> None:
        source = self.registry.resolve(state, required=True)
        identity = getattr(state, "source_identity", None)
        assert source is not None and identity is not None
        source.capabilities.require(TaskSourceCapability.COMMENTS)
        await source.add_comment(identity, body)

    async def attach_artifact(
        self,
        state: WorkItemState,
        url: str,
    ) -> None:
        source = self.registry.resolve(state, required=True)
        identity = getattr(state, "source_identity", None)
        assert source is not None and identity is not None
        source.capabilities.require(
            TaskSourceCapability.ARTIFACT_LINKS
        )
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
