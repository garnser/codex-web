from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from codex_web.identity import AuthenticationActor
from codex_web.reconciliation_gates import (
    ReconcilerDeclaration,
    ReconcilerStartupClass,
    ReconciliationGateAuditEvent,
    ReconciliationGateDecision,
    ReconciliationGateState,
    ReconciliationGateStoreState,
    ReconciliationMaintenanceLease,
    ReconciliationProjectControl,
)
from codex_web.services.identity import IdentityService
from codex_web.storage.state_store import StateStore


ReadinessProbe = Callable[[str, AuthenticationActor], dict[str, Any]]


class ReconciliationGateError(RuntimeError):
    pass


class ReconciliationGateConflict(ReconciliationGateError):
    pass


class ReconciliationGateService:
    NAMESPACE = "reconciliation_gates"

    def __init__(
        self,
        store: StateStore,
        *,
        readiness: ReadinessProbe,
        identity: IdentityService,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.readiness = readiness
        self.identity = identity
        self.clock = clock
        self._declarations: dict[str, ReconcilerDeclaration] = {}

    def register(self, declaration: ReconcilerDeclaration) -> None:
        existing = self._declarations.get(declaration.service_id)
        if existing is not None and existing != declaration:
            raise ReconciliationGateConflict(
                f"reconciler declaration already registered: {declaration.service_id}"
            )
        self._declarations[declaration.service_id] = declaration

    def declarations(self) -> tuple[ReconcilerDeclaration, ...]:
        return tuple(
            self._declarations[key] for key in sorted(self._declarations)
        )

    def _load(self) -> ReconciliationGateStoreState:
        return ReconciliationGateStoreState.model_validate(
            self.store.get(self.NAMESPACE) or {}
        )

    def _update(self, updater) -> ReconciliationGateStoreState:
        raw = self.store.update(
            self.NAMESPACE,
            lambda current: updater(
                ReconciliationGateStoreState.model_validate(current or {})
            ).model_dump(mode="json"),
            default={},
        )
        return ReconciliationGateStoreState.model_validate(raw)

    @staticmethod
    def _same_scope(
        value: ReconciliationProjectControl | ReconciliationMaintenanceLease,
        actor: AuthenticationActor,
    ) -> bool:
        return (
            value.organization_id == actor.organization_id
            and value.workspace_id == actor.workspace_id
        )

    def _control(
        self,
        service_id: str,
        project_id: str,
        actor: AuthenticationActor,
    ) -> ReconciliationProjectControl:
        state = self._load()
        current = next(
            (
                item
                for item in state.controls
                if item.service_id == service_id
                and item.project_id == project_id
                and self._same_scope(item, actor)
            ),
            None,
        )
        if current is not None:
            return current
        return ReconciliationProjectControl(
            service_id=service_id,
            project_id=project_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )

    def _active_maintenance(
        self,
        project_id: str,
        actor: AuthenticationActor,
    ) -> ReconciliationMaintenanceLease | None:
        now = self.clock()
        return next(
            (
                item
                for item in self._load().maintenance
                if item.project_id == project_id
                and self._same_scope(item, actor)
                and item.expires_at > now
            ),
            None,
        )

    def decision(
        self,
        service_id: str,
        project_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ReconciliationGateDecision:
        declaration = self._declarations.get(service_id)
        if declaration is None:
            raise ReconciliationGateError(
                f"reconciler is not declared: {service_id}"
            )
        control = self._control(service_id, project_id, actor)
        maintenance = (
            self._active_maintenance(project_id, actor)
            if declaration.maintenance_incompatible
            else None
        )
        if control.paused:
            return ReconciliationGateDecision(
                service_id=service_id,
                project_id=project_id,
                state=ReconciliationGateState.PAUSED,
                eligible=False,
                reason_code="operator_paused",
                reason=control.pause_reason or "Reconciler is paused by an operator.",
                readiness_check=declaration.readiness_check,
                approval_required=declaration.initial_approval_required,
                approved=control.approved,
                paused=True,
                maintenance_blocked=False,
                last_start_at=control.last_start_at,
                last_stop_at=control.last_stop_at,
                last_completion_at=control.last_completion_at,
                cursor=control.last_cursor,
            )
        if maintenance is not None:
            return ReconciliationGateDecision(
                service_id=service_id,
                project_id=project_id,
                state=ReconciliationGateState.PAUSED,
                eligible=False,
                reason_code="maintenance_lease_active",
                reason=f"Maintenance lease is active: {maintenance.reason}",
                readiness_check=declaration.readiness_check,
                approval_required=declaration.initial_approval_required,
                approved=control.approved,
                paused=False,
                maintenance_blocked=True,
                last_start_at=control.last_start_at,
                last_stop_at=control.last_stop_at,
                last_completion_at=control.last_completion_at,
                cursor=control.last_cursor,
            )

        readiness = None
        if declaration.readiness_required:
            try:
                readiness = self.readiness(project_id, actor)
            except Exception:
                return ReconciliationGateDecision(
                    service_id=service_id,
                    project_id=project_id,
                    state=ReconciliationGateState.BLOCKED,
                    eligible=False,
                    reason_code="project_readiness_unavailable",
                    reason="Project readiness could not be evaluated.",
                    readiness_check=declaration.readiness_check,
                    approval_required=declaration.initial_approval_required,
                    approved=control.approved,
                    paused=False,
                    maintenance_blocked=False,
                    last_start_at=control.last_start_at,
                    last_stop_at=control.last_stop_at,
                    last_completion_at=control.last_completion_at,
                    cursor=control.last_cursor,
                )
            if not bool(readiness.get("execution_ready", False)):
                blocker = next(
                    (
                        item
                        for item in readiness.get("checks", [])
                        if item.get("status") == "blocked"
                    ),
                    None,
                )
                return ReconciliationGateDecision(
                    service_id=service_id,
                    project_id=project_id,
                    state=ReconciliationGateState.BLOCKED,
                    eligible=False,
                    reason_code=(
                        str(blocker.get("code"))
                        if blocker
                        else "project_not_execution_ready"
                    ),
                    reason=(
                        str(blocker.get("message"))
                        if blocker
                        else "Project execution readiness is blocked."
                    ),
                    readiness_check=(
                        str(blocker.get("id"))
                        if blocker
                        else declaration.readiness_check
                    ),
                    approval_required=declaration.initial_approval_required,
                    approved=control.approved,
                    paused=False,
                    maintenance_blocked=False,
                    last_start_at=control.last_start_at,
                    last_stop_at=control.last_stop_at,
                    last_completion_at=control.last_completion_at,
                    cursor=control.last_cursor,
                )

        if declaration.initial_approval_required and not control.approved:
            return ReconciliationGateDecision(
                service_id=service_id,
                project_id=project_id,
                state=ReconciliationGateState.WAITING,
                eligible=False,
                reason_code="initial_operator_approval_required",
                reason="Initial or recovery reconciliation requires explicit operator approval.",
                readiness_check=declaration.readiness_check,
                approval_required=True,
                approved=False,
                paused=False,
                maintenance_blocked=False,
                last_start_at=control.last_start_at,
                last_stop_at=control.last_stop_at,
                last_completion_at=control.last_completion_at,
                cursor=control.last_cursor,
            )

        return ReconciliationGateDecision(
            service_id=service_id,
            project_id=project_id,
            state=ReconciliationGateState.ELIGIBLE,
            eligible=True,
            reason_code="eligible",
            reason="Declared reconciliation prerequisites are satisfied.",
            readiness_check=declaration.readiness_check,
            approval_required=declaration.initial_approval_required,
            approved=control.approved,
            paused=False,
            maintenance_blocked=False,
            last_start_at=control.last_start_at,
            last_stop_at=control.last_stop_at,
            last_completion_at=control.last_completion_at,
            cursor=control.last_cursor,
        )

    def _mutate_control(
        self,
        service_id: str,
        project_id: str,
        actor: AuthenticationActor,
        mutator,
    ) -> ReconciliationProjectControl:
        result: list[ReconciliationProjectControl] = []

        def update(state: ReconciliationGateStoreState):
            index = next(
                (
                    i
                    for i, item in enumerate(state.controls)
                    if item.service_id == service_id
                    and item.project_id == project_id
                    and self._same_scope(item, actor)
                ),
                None,
            )
            current = (
                state.controls[index]
                if index is not None
                else ReconciliationProjectControl(
                    service_id=service_id,
                    project_id=project_id,
                    organization_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                )
            )
            changed = mutator(current)
            changed.updated_at = self.clock()
            if index is None:
                state.controls.append(changed)
            else:
                state.controls[index] = changed
            result.append(changed)
            return state

        self._update(update)
        return result[0]

    def _audit(
        self,
        service_id: str,
        project_id: str,
        actor: AuthenticationActor,
        action: str,
        *,
        correlation_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        event = ReconciliationGateAuditEvent(
            id=f"reconciliation-audit-{uuid.uuid4().hex}",
            service_id=service_id,
            project_id=project_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            actor_identity_id=actor.identity_id,
            action=action,
            correlation_id=correlation_id,
            reason=reason,
            created_at=self.clock(),
        )

        def update(state: ReconciliationGateStoreState):
            state.audit.append(event)
            state.audit = state.audit[-5000:]
            return state

        self._update(update)

    def approve(
        self,
        service_id: str,
        project_id: str,
        *,
        actor: AuthenticationActor,
        correlation_id: str | None = None,
    ) -> ReconciliationGateDecision:
        IdentityService.require_admin(actor)
        declaration = self._declarations.get(service_id)
        if declaration is None:
            raise ReconciliationGateError("reconciler is not declared")
        self._mutate_control(
            service_id,
            project_id,
            actor,
            lambda current: current.model_copy(
                update={
                    "approved": True,
                    "approved_by": actor.identity_id,
                    "approved_at": self.clock(),
                    "approval_correlation_id": correlation_id,
                }
            ),
        )
        self._audit(
            service_id,
            project_id,
            actor,
            "approve",
            correlation_id=correlation_id,
        )
        return self.decision(service_id, project_id, actor=actor)

    def pause(
        self,
        service_id: str,
        project_id: str,
        *,
        actor: AuthenticationActor,
        reason: str,
    ) -> ReconciliationGateDecision:
        IdentityService.require_admin(actor)
        self._mutate_control(
            service_id,
            project_id,
            actor,
            lambda current: current.model_copy(
                update={
                    "paused": True,
                    "paused_by": actor.identity_id,
                    "paused_at": self.clock(),
                    "pause_reason": reason,
                }
            ),
        )
        self._audit(service_id, project_id, actor, "pause", reason=reason)
        return self.decision(service_id, project_id, actor=actor)

    def resume(
        self,
        service_id: str,
        project_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ReconciliationGateDecision:
        IdentityService.require_admin(actor)
        self._mutate_control(
            service_id,
            project_id,
            actor,
            lambda current: current.model_copy(
                update={
                    "paused": False,
                    "paused_by": None,
                    "paused_at": None,
                    "pause_reason": None,
                }
            ),
        )
        self._audit(service_id, project_id, actor, "resume")
        return self.decision(service_id, project_id, actor=actor)

    def record_start(
        self,
        service_id: str,
        project_id: str,
        *,
        actor: AuthenticationActor,
    ) -> None:
        self._mutate_control(
            service_id,
            project_id,
            actor,
            lambda current: current.model_copy(
                update={
                    "last_start_at": self.clock(),
                    "last_reason": "running",
                }
            ),
        )

    def record_completion(
        self,
        service_id: str,
        project_id: str,
        *,
        actor: AuthenticationActor,
        cursor: str | None = None,
    ) -> None:
        self._mutate_control(
            service_id,
            project_id,
            actor,
            lambda current: current.model_copy(
                update={
                    "last_completion_at": self.clock(),
                    "last_stop_at": self.clock(),
                    "last_cursor": cursor or current.last_cursor,
                    "last_reason": "completed",
                }
            ),
        )

    def acquire_maintenance(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
        owner: str,
        reason: str,
        ttl_seconds: float = 300.0,
    ) -> ReconciliationMaintenanceLease:
        IdentityService.require_admin(actor)
        now = self.clock()
        lease = ReconciliationMaintenanceLease(
            project_id=project_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            owner=owner,
            reason=reason,
            acquired_at=now,
            expires_at=now + max(1.0, ttl_seconds),
        )

        def update(state: ReconciliationGateStoreState):
            active = [
                item
                for item in state.maintenance
                if item.expires_at > now
            ]
            conflict = next(
                (
                    item
                    for item in active
                    if item.project_id == project_id
                    and self._same_scope(item, actor)
                    and item.owner != owner
                ),
                None,
            )
            if conflict is not None:
                raise ReconciliationGateConflict(
                    "Project already has an active maintenance lease"
                )
            state.maintenance = [
                item
                for item in active
                if not (
                    item.project_id == project_id
                    and self._same_scope(item, actor)
                    and item.owner == owner
                )
            ]
            state.maintenance.append(lease)
            return state

        self._update(update)
        self._audit(
            "maintenance",
            project_id,
            actor,
            "acquire",
            correlation_id=owner,
            reason=reason,
        )
        return lease

    def release_maintenance(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
        owner: str,
    ) -> None:
        IdentityService.require_admin(actor)

        def update(state: ReconciliationGateStoreState):
            state.maintenance = [
                item
                for item in state.maintenance
                if not (
                    item.project_id == project_id
                    and self._same_scope(item, actor)
                    and item.owner == owner
                )
            ]
            return state

        self._update(update)
        self._audit(
            "maintenance",
            project_id,
            actor,
            "release",
            correlation_id=owner,
        )

    def status(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        decisions = [
            self.decision(item.service_id, project_id, actor=actor)
            for item in self.declarations()
        ]
        state = self._load()
        return {
            "version": "1.0",
            "project_id": project_id,
            "items": [item.model_dump(mode="json") for item in decisions],
            "maintenance": [
                item.model_dump(mode="json")
                for item in state.maintenance
                if item.project_id == project_id
                and self._same_scope(item, actor)
                and item.expires_at > self.clock()
            ],
            "audit": [
                item.model_dump(mode="json")
                for item in state.audit[-100:]
                if item.project_id == project_id
                and item.organization_id == actor.organization_id
                and item.workspace_id == actor.workspace_id
            ],
        }
