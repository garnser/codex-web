from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import HTTPException

from codex_web.identity import AuthenticationActor
from codex_web.models import (
    ExecutionPreflightAttempt,
    ExecutionPreflightBlocker,
    ExecutionPreflightBlockerSnapshot,
    TurnCreate,
    QueuedTurn,
    QueuedNativeStartReceipt,
)
from codex_web.services.identity import IdentityService
from codex_web.storage.execution_preflight import ExecutionPreflightStore


class ExecutionPreflightService:
    """Persist and retry execution attempts blocked before active execution."""

    RETRY_CLAIM_TTL_SECONDS = 30.0
    MAX_HISTORY = 50

    def __init__(
        self,
        store: ExecutionPreflightStore,
        *,
        clock=time.time,
    ) -> None:
        self.store = store
        self.clock = clock

    @staticmethod
    def _attempt_id(execution_id: str) -> str:
        return f"preflight-{execution_id}"

    def retain_queued(
        self, queued: QueuedTurn, *, organization_id: str, workspace_id: str,
    ) -> ExecutionPreflightAttempt:
        """Retain admission intent, without claiming a native start or delivery."""
        correlation = queued.execution_id or f"reserved-queue-{queued.id}"
        now = float(self.clock())
        attempt = ExecutionPreflightAttempt(
            id=f'preflight-queued-{queued.id}', correlation_id=correlation,
            execution_id=correlation, organization_id=organization_id,
            workspace_id=workspace_id, thread_id=queued.thread_id,
            project_id=queued.project_id, message=queued.message,
            queued_turn_snapshot=queued.model_dump(mode="json"),
            sandbox=queued.sandbox, approval_policy=queued.approval_policy,
            model=queued.model, reasoning_effort=queued.reasoning_effort,
            repository_resource_id=queued.repository_resource_id,
            writable_repository_resource_ids=queued.writable_repository_resource_ids,
            read_only_repository_resource_ids=queued.read_only_repository_resource_ids,
            execution_profile_id=queued.execution_profile_id,
            agent_profile_id=queued.agent_profile_id,
            agent_profile_revision=queued.agent_profile_revision,
            agent_profile_actor_id=queued.agent_profile_actor_id,
            source=queued.source, status="queued", created_at=now, updated_at=now,
        )
        return self.store.retain_queued(attempt)

    def claim_queued_admission(self, attempt_id: str) -> tuple[ExecutionPreflightAttempt, str | None]:
        claim_id = f'queued-admission-{uuid.uuid4().hex}'
        acquired = False
        def update(current):
            nonlocal acquired
            acquired = False
            if current.status != 'queued' or current.queued_turn_snapshot is None:
                return current
            acquired = True
            return current.model_copy(update={'status':'admitting',
                'queued_admission_claim_id':claim_id, 'updated_at':float(self.clock())})
        updated = self.store.update(attempt_id, update)
        return updated, claim_id if acquired else None

    def begin_queued_start(self, attempt_id: str, *, claim_id: str) -> bool:
        acquired = False
        def update(current):
            nonlocal acquired
            acquired = False
            if current.status != 'admitting' or current.queued_admission_claim_id != claim_id:
                return current
            acquired = True
            return current.model_copy(update={'status':'reconciliation_required',
                'blockers':(), 'last_error':None, 'updated_at':float(self.clock())})
        self.store.update(attempt_id, update)
        return acquired

    def record_queued_native_start(
        self, attempt_id: str, receipt: QueuedNativeStartReceipt,
    ) -> ExecutionPreflightAttempt:
        """Only the native runtime response may supply this internal receipt."""
        def update(current):
            if current.status == 'started' and current.queued_start_receipt == receipt:
                return current
            if (current.queued_turn_snapshot is None
                    or current.status != 'reconciliation_required'
                    or current.queued_admission_claim_id != receipt.admission_claim_id
                    or current.execution_id != receipt.requested_execution_id
                    or (current.replacement_thread_id or current.thread_id) != receipt.thread_id):
                raise ValueError('native start receipt does not match the retained admission')
            return current.model_copy(update={'status':'started',
                'queued_start_receipt':receipt, 'started_at':receipt.observed_at,
                'updated_at':receipt.observed_at, 'last_error':None, 'blockers':()})
        return self.store.update(attempt_id, update)

    def queued_disposition(self, attempt_id: str, *, status: str,
                           detail: dict[str, Any] | None = None,
                           claim_id: str | None = None) -> ExecutionPreflightAttempt:
        """Internal runtime outcome; unknown native starts remain non-retryable."""
        if status not in {'queued', 'blocked', 'started', 'reconciliation_required'}:
            raise ValueError('invalid queued admission disposition')
        now = float(self.clock())
        def update(current):
            if current.queued_turn_snapshot is None:
                raise ValueError('queued disposition requires the original request')
            if current.status == 'started':
                return current
            if claim_id is not None and current.queued_admission_claim_id != claim_id:
                return current
            return current.model_copy(update={
                'status':status, 'updated_at':now,
                'started_at':now if status == 'started' else None,
                'blockers':self._blockers(detail) if detail else (),
                'last_error':str((detail or {}).get('message') or '') or None,
            })
        return self.store.update(attempt_id, update)

    @staticmethod
    def recorded_profile_refusal(attempt: ExecutionPreflightAttempt) -> bool:
        if (attempt.queued_turn_snapshot is None or attempt.queued_start_receipt is not None
                or attempt.started_at is not None or not attempt.agent_profile_id
                or attempt.agent_profile_revision is None):
            return False
        snapshot = attempt.queued_turn_snapshot
        if (snapshot.get('agent_profile_id') != attempt.agent_profile_id
                or snapshot.get('agent_profile_revision') != attempt.agent_profile_revision):
            return False
        for blocker in attempt.blockers:
            detail = blocker.model_dump(mode='json')
            effective = detail.get('effectiveAgentProfile')
            if not isinstance(effective, dict):
                continue
            if (detail.get('code') == 'thread_agent_profile_immutable'
                    and detail.get('requestedAgentProfileId') == attempt.agent_profile_id
                    and detail.get('requestedAgentProfileRevision') == attempt.agent_profile_revision
                    and effective.get('profile_id') == attempt.agent_profile_id
                    and type(effective.get('profile_revision')) is int
                    and effective['profile_revision'] >= 1
                    and effective['profile_revision'] != attempt.agent_profile_revision):
                return True
        return False

    @staticmethod
    def recorded_maintenance_refusal(attempt: ExecutionPreflightAttempt) -> bool:
        # These structured controller refusals occur before native turn/start.
        # Queue absence, timeouts and stale active markers are not evidence.
        snapshot = attempt.queued_turn_snapshot
        if (snapshot is None or attempt.queued_start_receipt is not None
                or attempt.started_at is not None
                or snapshot.get('thread_id') != attempt.thread_id):
            return False
        target = attempt.replacement_thread_id or attempt.thread_id
        if attempt.status not in {'queued', 'blocked'}:
            # Historical unknown starts can retain stale blocker text. Their
            # outcome needs separate authoritative reconciliation evidence.
            return False
        return any(blocker.code in {'upgrade_maintenance', 'upgrade_maintenance_active'}
                   and blocker.model_dump(mode='json').get('threadId') == target
                   for blocker in attempt.blockers)

    @classmethod
    def recorded_prestart_refusal(cls, attempt: ExecutionPreflightAttempt) -> bool:
        return cls.recorded_profile_refusal(attempt) or cls.recorded_maintenance_refusal(attempt)

    def reconcile_prestart_refusal(self, attempt_id: str, *, actor: AuthenticationActor) -> ExecutionPreflightAttempt:
        initial = self.get(attempt_id, actor=actor)
        self._authorize(initial, actor, require_admin=True)
        def update(current):
            # The recorded controller refusal occurs before native turn/start.
            # Absence, timeout, active markers and expired claims prove nothing.
            if current.status == 'started':
                return current
            if (current.status not in {'queued', 'reconciliation_required', 'blocked'}
                    or not self.recorded_prestart_refusal(current)
                    or (current.status == 'queued' and not self.recorded_maintenance_refusal(current))):
                raise HTTPException(status_code=409, detail={
                    'code':'prestart_refusal_reconciliation_unavailable',
                    'message':'No matching authoritative pre-start controller refusal is retained.'})
            return current.model_copy(update={'status':'blocked', 'updated_at':float(self.clock())})
        return self.store.update(attempt_id, update)

    def retained_queued(self, queued: QueuedTurn) -> ExecutionPreflightAttempt | None:
        attempt = self.store.get(f'preflight-queued-{queued.id}')
        if attempt is None or attempt.queued_turn_snapshot is None:
            return None
        if attempt.queued_turn_snapshot.get('id') != queued.id:
            raise ValueError('retained queued request identity changed')
        return attempt

    def queue_retry_committed(self, attempt_id: str, *, claim_id: str) -> ExecutionPreflightAttempt:
        def update(current):
            if current.status != 'retrying' or current.retry_claim_id != claim_id:
                return current
            return current.model_copy(update={'status':'queued', 'retry_claim_id':None,
                'retry_started_at':None, 'queued_admission_claim_id':None,
                'updated_at':float(self.clock())})
        return self.store.update(attempt_id, update)

    @staticmethod
    def _blockers(detail: Any) -> tuple[ExecutionPreflightBlocker, ...]:
        if not isinstance(detail, dict):
            return (
                ExecutionPreflightBlocker(
                    code="execution_preflight_blocked",
                    message=str(detail or "execution preflight blocked"),
                ),
            )
        raw = detail.get("blockers")
        if not isinstance(raw, list):
            raw = [detail]
        blockers: list[ExecutionPreflightBlocker] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            value = dict(item)
            value.setdefault(
                "code",
                str(detail.get("code") or "execution_preflight_blocked"),
            )
            value.setdefault(
                "message",
                str(
                    detail.get("message")
                    or "execution preflight blocked"
                ),
            )
            blockers.append(
                ExecutionPreflightBlocker.model_validate(value)
            )
        return tuple(blockers) or (
            ExecutionPreflightBlocker(
                code="execution_preflight_blocked",
                message=str(
                    detail.get("message")
                    or "execution preflight blocked"
                ),
            ),
        )

    @staticmethod
    def is_preflight_http_error(exc: Exception) -> bool:
        return (
            isinstance(exc, HTTPException)
            and exc.status_code in {400, 403, 409, 422, 503}
            and isinstance(exc.detail, dict)
            and exc.detail.get("code") == "execution_preflight_blocked"
        )

    def record_blocked(
        self,
        *,
        actor: AuthenticationActor,
        thread_id: str,
        project_id: str,
        execution_id: str,
        payload: TurnCreate,
        effective: dict[str, Any],
        detail: dict[str, Any],
        source: str = "web",
    ) -> ExecutionPreflightAttempt:
        now = float(self.clock())
        blockers = self._blockers(detail)
        attempt_id = self._attempt_id(execution_id)
        existing = self.store.get(attempt_id)
        if existing is not None and (
            thread_id not in {existing.thread_id, existing.replacement_thread_id}
            or existing.project_id != project_id
            or existing.message != payload.message
            or existing.organization_id != actor.organization_id
            or existing.workspace_id != actor.workspace_id
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "execution_preflight_identity_conflict",
                    "message": (
                        "execution id is already bound to a different retained "
                        "turn identity"
                    ),
                    "correlationId": execution_id,
                },
            )
        if existing is None:
            attempt = ExecutionPreflightAttempt(
                id=attempt_id,
                correlation_id=execution_id,
                execution_id=execution_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                thread_id=thread_id,
                project_id=project_id,
                message=payload.message,
                sandbox=effective.get("sandbox"),
                approval_policy=effective.get("approval_policy"),
                model=effective.get("model"),
                reasoning_effort=effective.get("reasoning_effort"),
                repository_resource_id=effective.get(
                    "repository_resource_id"
                ),
                writable_repository_resource_ids=tuple(
                    effective.get("writable_repository_resource_ids")
                    or ()
                ),
                read_only_repository_resource_ids=tuple(
                    effective.get("read_only_repository_resource_ids")
                    or ()
                ),
                execution_profile_id=effective.get(
                    "execution_profile_id"
                ),
                agent_profile_id=effective.get(
                    "agent_profile_id"
                ),
                agent_profile_revision=effective.get(
                    "agent_profile_revision"
                ),
                agent_profile_actor_id=actor.identity_id,
                source=source,
                status="blocked",
                blockers=blockers,
                blocker_history=(
                    ExecutionPreflightBlockerSnapshot(
                        blockers=blockers,
                        recorded_at=now,
                        attempt_number=1,
                    ),
                ),
                attempt_number=1,
                created_at=now,
                updated_at=now,
                last_error=str(detail.get("message") or ""),
            )
            self.store.put(attempt)
            return attempt

        def update(
            current: ExecutionPreflightAttempt,
        ) -> ExecutionPreflightAttempt:
            history = list(current.blocker_history)
            if not history or history[-1].blockers != blockers:
                history.append(
                    ExecutionPreflightBlockerSnapshot(
                        blockers=blockers,
                        recorded_at=now,
                        attempt_number=current.attempt_number,
                    )
                )
            return current.model_copy(
                update={
                    "status": "blocked",
                    "blockers": blockers,
                    "blocker_history": tuple(
                        history[-self.MAX_HISTORY :]
                    ),
                    "retry_claim_id": None,
                    "retry_started_at": None,
                    "last_error": str(
                        detail.get("message") or ""
                    ),
                    "updated_at": now,
                }
            )

        return self.store.update(attempt_id, update)

    def _authorize(
        self,
        attempt: ExecutionPreflightAttempt,
        actor: AuthenticationActor,
        *,
        require_admin: bool = False,
    ) -> None:
        if (
            attempt.organization_id != actor.organization_id
            or attempt.workspace_id != actor.workspace_id
        ):
            raise HTTPException(status_code=404, detail="preflight attempt not found")
        if require_admin:
            try:
                IdentityService.require_admin(actor)
            except Exception as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc

    def get(
        self,
        attempt_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionPreflightAttempt:
        attempt = self.store.get(attempt_id)
        if attempt is None:
            raise HTTPException(status_code=404, detail="preflight attempt not found")
        self._authorize(attempt, actor)
        return attempt

    def for_thread(
        self,
        thread_id: str,
        *,
        actor: AuthenticationActor,
        limit: int = 50,
    ) -> list[ExecutionPreflightAttempt]:
        return [
            item
            for item in self.store.for_thread(thread_id, limit=limit)
            if item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        ]

    def authorize_retry(self, attempt_id: str, *, actor: AuthenticationActor) -> ExecutionPreflightAttempt:
        current = self.get(attempt_id, actor=actor)
        self._authorize(current, actor, require_admin=True)
        return current

    def claim_retry(
        self,
        attempt_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[ExecutionPreflightAttempt, str | None, bool]:
        current = self.get(attempt_id, actor=actor)
        self._authorize(current, actor, require_admin=True)
        if current.status == "started":
            return current, None, False

        claim_id = f"retry-{uuid.uuid4().hex}"
        updated, claimed = self.store.claim_retry(
            attempt_id,
            claim_id=claim_id,
            now=float(self.clock()),
            stale_after_seconds=self.RETRY_CLAIM_TTL_SECONDS,
        )
        self._authorize(updated, actor, require_admin=True)
        return updated, updated.retry_claim_id, claimed

    def bind_replacement(
        self,
        attempt_id: str,
        *,
        actor: AuthenticationActor,
        claim_id: str,
        previous_thread_id: str,
        replacement_thread_id: str,
    ) -> ExecutionPreflightAttempt:
        current = self.get(attempt_id, actor=actor)
        self._authorize(current, actor, require_admin=True)

        def update(value: ExecutionPreflightAttempt) -> ExecutionPreflightAttempt:
            if (
                value.status != "retrying"
                or value.retry_claim_id != claim_id
                or (value.replacement_thread_id or value.thread_id) != previous_thread_id
            ):
                raise HTTPException(status_code=409, detail="preflight retry target changed")
            return value.model_copy(update={
                "replacement_thread_id": replacement_thread_id,
                "updated_at": float(self.clock()),
            })

        return self.store.update(attempt_id, update)

    def mark_started(
        self,
        attempt_id: str,
        *,
        actor: AuthenticationActor,
        claim_id: str | None,
    ) -> ExecutionPreflightAttempt:
        current = self.get(attempt_id, actor=actor)
        self._authorize(current, actor, require_admin=True)
        now = float(self.clock())

        def update(
            value: ExecutionPreflightAttempt,
        ) -> ExecutionPreflightAttempt:
            if value.status == "started":
                return value
            if claim_id and value.retry_claim_id != claim_id:
                return value
            return value.model_copy(
                update={
                    "status": "started",
                    "blockers": (),
                    "started_at": now,
                    "retry_claim_id": None,
                    "retry_started_at": None,
                    "last_error": None,
                    "updated_at": now,
                }
            )

        return self.store.update(attempt_id, update)

    def get_for_execution(
        self,
        execution_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ExecutionPreflightAttempt | None:
        attempt = self.store.get(self._attempt_id(execution_id))
        if attempt is None:
            return None
        self._authorize(attempt, actor)
        return attempt

    def mark_started_for_execution(
        self,
        execution_id: str,
        *,
        actor: AuthenticationActor,
        claim_id: str | None,
    ) -> ExecutionPreflightAttempt | None:
        attempt = self.get_for_execution(execution_id, actor=actor)
        if attempt is None:
            return None
        return self.mark_started(
            attempt.id,
            actor=actor,
            claim_id=claim_id,
        )

    def mark_failed(
        self,
        attempt_id: str,
        *,
        actor: AuthenticationActor,
        claim_id: str | None,
        error: Exception,
    ) -> ExecutionPreflightAttempt:
        current = self.get(attempt_id, actor=actor)
        self._authorize(current, actor, require_admin=True)
        if claim_id and current.retry_claim_id not in {None, claim_id}:
            return current
        return self.store.mark_failed(
            attempt_id,
            now=float(self.clock()),
            error=str(error)[:1000],
        )

    @staticmethod
    def can_retry(actor: AuthenticationActor) -> bool:
        try:
            IdentityService.require_admin(actor)
        except Exception:
            return False
        return True

    def public(
        self,
        attempt: ExecutionPreflightAttempt,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        self._authorize(attempt, actor)
        payload = attempt.model_dump(mode="json")
        payload["can_retry"] = self.can_retry(actor) and attempt.status not in {
            "queued", "reconciliation_required", "started"}
        payload['can_reconcile_prestart'] = (self.can_retry(actor)
            and ((attempt.status == 'reconciliation_required' and self.recorded_prestart_refusal(attempt))
                 or (attempt.status == 'queued' and self.recorded_maintenance_refusal(attempt))))
        payload['reconcile_prestart_href'] = (
            f'/api/threads/{attempt.replacement_thread_id or attempt.thread_id}/preflight-attempts/{attempt.id}/reconcile-prestart')
        payload["retry_href"] = (
            f"/api/threads/{attempt.replacement_thread_id or attempt.thread_id}/preflight-attempts/"
            f"{attempt.id}/retry"
        )
        return payload

    def retry_payload(
        self,
        attempt: ExecutionPreflightAttempt,
    ) -> TurnCreate:
        return TurnCreate(
            message=attempt.message,
            project_id=attempt.project_id,
            model=attempt.model,
            reasoning_effort=attempt.reasoning_effort,
            approval_policy=attempt.approval_policy,
            sandbox=attempt.sandbox,
            repository_resource_id=attempt.repository_resource_id,
            writable_repository_resource_ids=(
                attempt.writable_repository_resource_ids
            ),
            read_only_repository_resource_ids=(
                attempt.read_only_repository_resource_ids
            ),
            execution_profile_id=attempt.execution_profile_id,
            agent_profile_id=attempt.agent_profile_id,
            agent_profile_revision=attempt.agent_profile_revision,
        )
