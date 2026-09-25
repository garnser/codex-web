from __future__ import annotations

import time

from codex_web.goal_execution_bindings import (
    GoalExecutionBinding,
    GoalExecutionBindingCreate,
    GoalExecutionBindingReconcile,
    GoalExecutionBindingEvent,
    GoalExecutionBindingState,
    GoalExecutionBindingStatus,
    GoalExecutionBindingUpdate,
)
from codex_web.identity import TenantScope
from codex_web.services.goals import GoalNotFoundError, GoalService
from codex_web.storage.goal_execution_bindings import GoalExecutionBindingStore


class GoalExecutionBindingError(RuntimeError):
    pass


class GoalExecutionBindingNotFoundError(GoalExecutionBindingError):
    pass


class GoalExecutionBindingConflictError(GoalExecutionBindingError):
    pass


class GoalExecutionBindingService:
    """Durable provider-neutral execution projection for canonical Goals."""

    def __init__(
        self,
        store: GoalExecutionBindingStore,
        goals: GoalService,
    ) -> None:
        self.store = store
        self.goals = goals

    @staticmethod
    def _visible(binding: GoalExecutionBinding, scope: TenantScope) -> bool:
        return (
            binding.organization_id == scope.organization_id
            and binding.workspace_id == scope.workspace_id
        )

    def _binding(
        self,
        state: GoalExecutionBindingState,
        binding_id: str,
        scope: TenantScope,
    ) -> GoalExecutionBinding:
        binding = next((item for item in state.bindings if item.id == binding_id), None)
        if binding is None or not self._visible(binding, scope):
            raise GoalExecutionBindingNotFoundError("goal execution binding not found")
        return binding

    def create(
        self,
        goal_id: str,
        payload: GoalExecutionBindingCreate,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> GoalExecutionBinding:
        try:
            goal = self.goals.get(goal_id, scope=scope)
        except GoalNotFoundError as exc:
            raise GoalExecutionBindingNotFoundError("goal not found") from exc
        revision = payload.goal_revision or goal.revision
        if revision != goal.revision:
            raise GoalExecutionBindingConflictError(
                f"goal revision changed: expected {revision}, active {goal.revision}"
            )
        matching_scope = next(
            (
                item
                for item in goal.work_graph_bindings
                if item.project_id == payload.project_id
            ),
            None,
        )
        if goal.work_graph_bindings and matching_scope is None:
            raise GoalExecutionBindingConflictError(
                "execution binding project is outside the canonical Goal Work Graph scope"
            )
        if (
            matching_scope is not None
            and matching_scope.root_work_item_refs
            and payload.work_item_refs
            and not set(payload.work_item_refs).issubset(
                set(matching_scope.root_work_item_refs)
            )
        ):
            raise GoalExecutionBindingConflictError(
                "execution binding Work Item scope exceeds the canonical Goal roots"
            )

        now = time.time()
        binding = GoalExecutionBinding(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            goal_id=goal.id,
            goal_revision=goal.revision,
            project_id=payload.project_id,
            work_item_refs=payload.work_item_refs,
            provider_id=payload.provider_id,
            runtime_id=payload.runtime_id,
            agent_session_id=payload.agent_session_id,
            thread_id=payload.thread_id,
            execution_owner_id=payload.execution_owner_id,
            provider_native_objective_id=payload.provider_native_objective_id,
            native_objective_supported=payload.native_objective_supported,
            capability_snapshot=payload.capability_snapshot,
            cursor_ref=payload.cursor_ref,
            checkpoint_ref=payload.checkpoint_ref,
            created_by=actor_id,
            updated_by=actor_id,
            change_reason=payload.reason,
            created_at=now,
            updated_at=now,
        )

        def apply(state: GoalExecutionBindingState) -> GoalExecutionBindingState:
            if any(
                item.goal_id == goal.id
                and item.agent_session_id == payload.agent_session_id
                and item.status.value not in {"completed", "cancelled", "failed"}
                for item in state.bindings
                if self._visible(item, scope)
            ):
                raise GoalExecutionBindingConflictError(
                    "session already has a non-terminal binding for this Goal"
                )
            state.bindings.append(binding)
            state.events.append(
                GoalExecutionBindingEvent(
                    binding_id=binding.id,
                    goal_id=goal.id,
                    event_type="binding_created",
                    actor_id=actor_id,
                    reason=payload.reason,
                    occurred_at=now,
                )
            )
            return state

        self.store.update(apply)
        return binding

    def list_all(
        self,
        *,
        scope: TenantScope,
    ) -> tuple[GoalExecutionBinding, ...]:
        rows = [
            item
            for item in self.store.load().bindings
            if self._visible(item, scope)
        ]
        rows.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return tuple(rows)

    def list(
        self,
        goal_id: str,
        *,
        scope: TenantScope,
    ) -> tuple[GoalExecutionBinding, ...]:
        self.goals.get(goal_id, scope=scope)
        return tuple(
            item
            for item in self.list_all(scope=scope)
            if item.goal_id == goal_id
        )

    def get(
        self,
        binding_id: str,
        *,
        scope: TenantScope,
    ) -> GoalExecutionBinding:
        return self._binding(self.store.load(), binding_id, scope)

    def update(
        self,
        binding_id: str,
        payload: GoalExecutionBindingUpdate,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> GoalExecutionBinding:
        result: GoalExecutionBinding | None = None

        def apply(state: GoalExecutionBindingState) -> GoalExecutionBindingState:
            nonlocal result
            current = self._binding(state, binding_id, scope)
            changes = payload.model_dump(
                mode="python",
                exclude={"reason"},
                exclude_unset=True,
            )
            if not changes:
                raise GoalExecutionBindingConflictError(
                    "execution binding update contains no changes"
                )
            result = current.model_copy(
                update={
                    **changes,
                    "updated_by": actor_id,
                    "change_reason": payload.reason,
                    "updated_at": time.time(),
                }
            )
            state.bindings = [
                result if item.id == binding_id else item
                for item in state.bindings
            ]
            state.events.append(
                GoalExecutionBindingEvent(
                    binding_id=binding_id,
                    goal_id=current.goal_id,
                    event_type=f"binding_{result.status.value}",
                    actor_id=actor_id,
                    reason=payload.reason,
                )
            )
            return state

        self.store.update(apply)
        assert result is not None
        return result

    def reconcile_unknown(
        self,
        binding_id: str,
        payload: GoalExecutionBindingReconcile,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> GoalExecutionBinding:
        result: GoalExecutionBinding | None = None
        now = time.time()

        def apply(state: GoalExecutionBindingState) -> GoalExecutionBindingState:
            nonlocal result
            current = self._binding(state, binding_id, scope)
            if current.status != GoalExecutionBindingStatus.UNKNOWN:
                raise GoalExecutionBindingConflictError(
                    "only UNKNOWN execution bindings can be reconciled"
                )
            result = current.model_copy(
                update={
                    "status": payload.outcome,
                    "lease_owner_id": None,
                    "lease_expires_at": None,
                    "heartbeat_at": now,
                    "retry_not_before_at": None,
                    "stop_reason": payload.reason,
                    "updated_by": actor_id,
                    "change_reason": payload.reason,
                    "updated_at": now,
                }
            )
            state.bindings = [
                result if item.id == binding_id else item
                for item in state.bindings
            ]
            state.events.append(
                GoalExecutionBindingEvent(
                    binding_id=binding_id,
                    goal_id=current.goal_id,
                    event_type="binding_reconciled",
                    actor_id=actor_id,
                    reason=(
                        f"{payload.reason}; reconciled provider outcome as "
                        f"{payload.outcome.value}"
                    ),
                    occurred_at=now,
                )
            )
            return state

        self.store.update(apply)
        assert result is not None
        return result

    def claim_continuation(
        self,
        binding_id: str,
        *,
        scope: TenantScope,
        owner_id: str,
        lease_seconds: float = 60.0,
        now: float | None = None,
    ) -> GoalExecutionBinding | None:
        current_time = time.time() if now is None else float(now)
        duration = max(1.0, float(lease_seconds))
        result: GoalExecutionBinding | None = None

        def apply(state: GoalExecutionBindingState) -> GoalExecutionBindingState:
            nonlocal result
            current = self._binding(state, binding_id, scope)
            if current.status.value in {"completed", "cancelled", "blocked"}:
                return state
            if (
                current.retry_not_before_at is not None
                and current.retry_not_before_at > current_time
            ):
                return state
            lease_active = (
                current.lease_owner_id is not None
                and current.lease_expires_at is not None
                and current.lease_expires_at > current_time
            )
            if lease_active and current.lease_owner_id != owner_id:
                return state
            result = current.model_copy(
                update={
                    "status": GoalExecutionBindingStatus.ACTIVE,
                    "lease_owner_id": owner_id,
                    "lease_expires_at": current_time + duration,
                    "heartbeat_at": current_time,
                    "retry_not_before_at": None,
                    "updated_by": owner_id,
                    "change_reason": "continuation lease claimed",
                    "updated_at": current_time,
                }
            )
            state.bindings = [
                result if item.id == binding_id else item
                for item in state.bindings
            ]
            state.events.append(
                GoalExecutionBindingEvent(
                    binding_id=binding_id,
                    goal_id=current.goal_id,
                    event_type="binding_continuation_claimed",
                    actor_id=owner_id,
                    reason="continuation lease claimed",
                    occurred_at=current_time,
                )
            )
            return state

        self.store.update(apply)
        return result

    def heartbeat_continuation(
        self,
        binding_id: str,
        *,
        scope: TenantScope,
        owner_id: str,
        lease_seconds: float = 60.0,
        now: float | None = None,
    ) -> GoalExecutionBinding:
        current_time = time.time() if now is None else float(now)
        duration = max(1.0, float(lease_seconds))
        result: GoalExecutionBinding | None = None

        def apply(state: GoalExecutionBindingState) -> GoalExecutionBindingState:
            nonlocal result
            current = self._binding(state, binding_id, scope)
            if current.lease_owner_id != owner_id:
                raise GoalExecutionBindingConflictError(
                    "continuation lease is owned by another worker"
                )
            if (
                current.lease_expires_at is not None
                and current.lease_expires_at <= current_time
            ):
                raise GoalExecutionBindingConflictError(
                    "continuation lease has expired"
                )
            result = current.model_copy(
                update={
                    "lease_expires_at": current_time + duration,
                    "heartbeat_at": current_time,
                    "updated_by": owner_id,
                    "change_reason": "continuation lease heartbeat",
                    "updated_at": current_time,
                }
            )
            state.bindings = [
                result if item.id == binding_id else item
                for item in state.bindings
            ]
            return state

        self.store.update(apply)
        assert result is not None
        return result

    def release_continuation(
        self,
        binding_id: str,
        *,
        scope: TenantScope,
        owner_id: str,
        status: GoalExecutionBindingStatus,
        reason: str,
        retry_after_seconds: float | None = None,
        increment_recovery: bool = False,
        now: float | None = None,
    ) -> GoalExecutionBinding:
        current_time = time.time() if now is None else float(now)
        result: GoalExecutionBinding | None = None

        def apply(state: GoalExecutionBindingState) -> GoalExecutionBindingState:
            nonlocal result
            current = self._binding(state, binding_id, scope)
            if current.lease_owner_id != owner_id:
                raise GoalExecutionBindingConflictError(
                    "continuation lease is owned by another worker"
                )
            retry_at = (
                current_time + max(0.0, float(retry_after_seconds))
                if retry_after_seconds is not None
                else None
            )
            result = current.model_copy(
                update={
                    "status": status,
                    "lease_owner_id": None,
                    "lease_expires_at": None,
                    "heartbeat_at": current_time,
                    "retry_not_before_at": retry_at,
                    "stop_reason": reason,
                    "recovery_attempts": (
                        current.recovery_attempts + 1
                        if increment_recovery
                        else current.recovery_attempts
                    ),
                    "updated_by": owner_id,
                    "change_reason": reason,
                    "updated_at": current_time,
                }
            )
            state.bindings = [
                result if item.id == binding_id else item
                for item in state.bindings
            ]
            state.events.append(
                GoalExecutionBindingEvent(
                    binding_id=binding_id,
                    goal_id=current.goal_id,
                    event_type=f"binding_{result.status.value}",
                    actor_id=owner_id,
                    reason=reason,
                    occurred_at=current_time,
                )
            )
            return state

        self.store.update(apply)
        assert result is not None
        return result

    def events(
        self,
        goal_id: str,
        *,
        scope: TenantScope,
    ) -> tuple[GoalExecutionBindingEvent, ...]:
        visible_ids = {item.id for item in self.list(goal_id, scope=scope)}
        return tuple(
            item
            for item in self.store.load().events
            if item.goal_id == goal_id and item.binding_id in visible_ids
        )
