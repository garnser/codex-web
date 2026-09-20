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
                read_only_repository_resource_ids=tuple(
                    effective.get("read_only_repository_resource_ids")
                    or ()
                ),
                execution_profile_id=effective.get(
                    "execution_profile_id"
                ),
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
            read_only_repository_resource_ids=(
                attempt.read_only_repository_resource_ids
            ),
            execution_profile_id=attempt.execution_profile_id,
        )
