from __future__ import annotations

from typing import Any, Callable
import hashlib
import json

from codex_web.models import (
    ExecutionPreflightAttempt,
    ExecutionPreflightBlocker,
    ExecutionPreflightBlockerSnapshot,
)
from codex_web.storage.state_store import StateStore


class ExecutionPreflightStore:
    """Keyed durable store for blocked/retried execution attempts.

    Attempt identity is authoritative under an `a/` key. A duplicate
    thread/time key lives in the same record collection so one record_apply()
    updates both views atomically.
    """

    NAMESPACE = "execution_preflight_attempts"
    MAX_TIMESTAMP_MICROS = 9_999_999_999_999_999

    def __init__(self, store: StateStore) -> None:
        self.store = store
        # Queue duplicate checks read this collection inside the queue's atomic
        # update. Materialize its keyed layout before that transaction: lazy
        # initialization would need a second SQLite writer and deadlock it.
        self.store.record_page(self.NAMESPACE, limit=1)

    @classmethod
    def _inverse_timestamp(cls, value: float) -> int:
        micros = max(0, int(float(value) * 1_000_000))
        return cls.MAX_TIMESTAMP_MICROS - min(
            micros,
            cls.MAX_TIMESTAMP_MICROS,
        )

    @staticmethod
    def _attempt_key(attempt_id: str) -> str:
        return f"a/{attempt_id}"

    @staticmethod
    def queued_identity(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {key:value for key,value in snapshot.items()
            if key not in {'id','execution_id','created_at','attempts'}}

    @classmethod
    def _queued_identity_key(cls, snapshot: dict[str, Any]) -> str:
        digest = hashlib.sha256(json.dumps(cls.queued_identity(snapshot), sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        return f"q/{snapshot['thread_id']}/{digest}/"

    def matching_queued(self, snapshot: dict[str, Any]) -> ExecutionPreflightAttempt | None:
        cursor = None
        while True:
            rows, cursor = self.store.record_page(self.NAMESPACE,
                key_prefix=self._queued_identity_key(snapshot), after=cursor, limit=200)
            for row in rows.values():
                attempt = self.get(row['attemptId'])
                if attempt is not None and attempt.status != 'started':
                    return attempt
            if cursor is None:
                return None

    @classmethod
    def _thread_key(cls, attempt: ExecutionPreflightAttempt) -> str:
        return (
            f"t/{attempt.thread_id}/"
            f"{cls._inverse_timestamp(attempt.created_at):016d}/"
            f"{attempt.id}"
        )

    def get(self, attempt_id: str) -> ExecutionPreflightAttempt | None:
        payload = self.store.record_get(
            self.NAMESPACE,
            self._attempt_key(attempt_id),
        )
        return (
            ExecutionPreflightAttempt.model_validate(payload)
            if isinstance(payload, dict)
            else None
        )

    def put(self, attempt: ExecutionPreflightAttempt) -> None:
        payload = attempt.model_dump(mode="json")
        self.store.record_apply(
            self.NAMESPACE,
            upserts={
                self._attempt_key(attempt.id): payload,
                self._thread_key(attempt): {
                    "attemptId": attempt.id,
                },
            },
        )

    def retain_queued(self, attempt: ExecutionPreflightAttempt) -> ExecutionPreflightAttempt:
        """Commit the full request before queue removal; retries preserve its state."""
        def retain(raw):
            if raw is not None:
                current = ExecutionPreflightAttempt.model_validate(raw)
                expected = current.queued_turn_snapshot
                if expected is not None and current.replacement_thread_id:
                    expected = dict(expected)
                    original_thread = expected.get('thread_id')
                    expected['thread_id'] = current.replacement_thread_id
                    reply = expected.get('reply_target')
                    if reply and reply.get('thread_id') == original_thread:
                        expected['reply_target'] = dict(reply, thread_id=current.replacement_thread_id)
                if (
                    current.organization_id != attempt.organization_id
                    or current.workspace_id != attempt.workspace_id
                    or current.project_id != attempt.project_id
                    or expected != attempt.queued_turn_snapshot
                ):
                    raise ValueError("retained queued request identity changed")
                return raw
            return attempt.model_dump(mode="json")

        raw = self.store.record_update(self.NAMESPACE, self._attempt_key(attempt.id),
            retain, default=None)
        retained = ExecutionPreflightAttempt.model_validate(raw)
        # Both writes complete before the runtime may remove the queue entry.
        # A crash between them leaves the original queue intact; calling this
        # again repairs the existing domain's inspection index idempotently.
        self.store.record_apply(self.NAMESPACE, upserts={
            self._thread_key(retained): {"attemptId":retained.id},
            self._queued_identity_key(attempt.queued_turn_snapshot)+retained.id:
                {"attemptId":retained.id}})
        return retained

    def update(
        self,
        attempt_id: str,
        updater: Callable[
            [ExecutionPreflightAttempt],
            ExecutionPreflightAttempt,
        ],
    ) -> ExecutionPreflightAttempt:
        key = self._attempt_key(attempt_id)

        def update_raw(payload: Any) -> dict[str, Any]:
            if not isinstance(payload, dict):
                raise KeyError(attempt_id)
            current = ExecutionPreflightAttempt.model_validate(payload)
            updated = updater(current.model_copy(deep=True))
            if (
                updated.id != current.id
                or updated.thread_id != current.thread_id
            ):
                raise ValueError(
                    "execution preflight attempt identity is immutable"
                )
            return updated.model_dump(mode="json")

        payload = self.store.record_update(
            self.NAMESPACE,
            key,
            update_raw,
            default=None,
        )
        updated = ExecutionPreflightAttempt.model_validate(payload)
        if updated.replacement_thread_id:
            target = updated.model_copy(update={"thread_id": updated.replacement_thread_id})
            indexes = {
                self._thread_key(target): {"attemptId": updated.id},
            }
            if updated.queued_turn_snapshot is not None:
                projected = dict(updated.queued_turn_snapshot, thread_id=updated.replacement_thread_id)
                reply = projected.get('reply_target')
                if reply and reply.get('thread_id') == updated.thread_id:
                    projected['reply_target'] = dict(reply, thread_id=updated.replacement_thread_id)
                indexes[self._queued_identity_key(projected)+updated.id] = {'attemptId':updated.id}
            self.store.record_apply(self.NAMESPACE, upserts=indexes)
        return updated


    def for_thread(
        self,
        thread_id: str,
        *,
        limit: int = 50,
    ) -> list[ExecutionPreflightAttempt]:
        page_size = max(1, min(int(limit), 200))
        prefix = f"t/{thread_id}/"
        rows, _cursor = self.store.record_page(
            self.NAMESPACE,
            key_prefix=prefix,
            limit=page_size,
        )
        result: list[ExecutionPreflightAttempt] = []
        for payload in rows.values():
            if not isinstance(payload, dict):
                continue
            attempt_id = str(payload.get("attemptId") or "")
            if not attempt_id:
                continue
            attempt = self.get(attempt_id)
            if attempt is not None and thread_id in {attempt.thread_id, attempt.replacement_thread_id}:
                result.append(attempt)
        return result

    def claim_retry(
        self,
        attempt_id: str,
        *,
        claim_id: str,
        now: float,
        stale_after_seconds: float = 60.0,
    ) -> tuple[ExecutionPreflightAttempt, bool]:
        key = self._attempt_key(attempt_id)
        acquired = False

        def mutate(payload):
            nonlocal acquired
            acquired = False
            if not isinstance(payload, dict):
                raise KeyError(attempt_id)
            current = ExecutionPreflightAttempt.model_validate(payload)
            if current.bootstrap_recovery is not None and current.bootstrap_recovery.state != 'created':
                return current.model_dump(mode="json")
            if current.status in {"queued", "reconciliation_required"}:
                return current.model_dump(mode="json")
            if current.status == "started":
                return current.model_dump(mode="json")
            if current.status == "retrying":
                if current.retry_claim_id == claim_id:
                    acquired = True
                    return current.model_dump(mode="json")
                retry_started_at = current.retry_started_at or current.updated_at
                if now - retry_started_at < max(1.0, stale_after_seconds):
                    return current.model_dump(mode="json")
                # Retained queued recovery never calls the native runtime in
                # retrying state. Only a later queued -> admitting -> unknown
                # claim can pass the native gate, so interrupted queue recovery
                # may acquire a new token without inferring a start outcome.
            acquired = True
            return current.model_copy(
                update={
                    "status": "retrying",
                    "attempt_number": current.attempt_number + 1,
                    "retry_claim_id": claim_id,
                    "retry_started_at": now,
                    "last_error": None,
                    **({'queued_admission_claim_id':None}
                       if current.queued_turn_snapshot is not None else {}),
                    "updated_at": now,
                }
            ).model_dump(mode="json")

        payload = self.store.record_update(
            self.NAMESPACE,
            key,
            mutate,
            default=None,
        )
        attempt = ExecutionPreflightAttempt.model_validate(payload)
        self.store.record_apply(
            self.NAMESPACE,
            upserts={self._thread_key(attempt): {"attemptId":attempt.id}},
        )
        return attempt, acquired

    def mark_blocked(
        self,
        attempt_id: str,
        *,
        blockers,
        now: float,
        last_error: str | None = None,
    ) -> ExecutionPreflightAttempt:
        normalized = tuple(
            item
            if isinstance(item, ExecutionPreflightBlocker)
            else ExecutionPreflightBlocker.model_validate(item)
            for item in blockers
        )

        def apply(current: ExecutionPreflightAttempt):
            snapshot = ExecutionPreflightBlockerSnapshot(
                blockers=normalized,
                recorded_at=now,
                attempt_number=current.attempt_number,
            )
            history = tuple(
                (*current.blocker_history, snapshot)[-20:]
            )
            return current.model_copy(
                update={
                    "status": "blocked",
                    "blockers": normalized,
                    "blocker_history": history,
                    "retry_claim_id": None,
                    "retry_started_at": None,
                    "last_error": last_error,
                    "updated_at": now,
                }
            )

        return self.update(attempt_id, apply)

    def mark_started(
        self,
        attempt_id: str,
        *,
        now: float,
    ) -> ExecutionPreflightAttempt:
        return self.update(
            attempt_id,
            lambda current: current.model_copy(
                update={
                    "status": "started",
                    "blockers": (),
                    "retry_claim_id": None,
                    "retry_started_at": None,
                    "started_at": now,
                    "last_error": None,
                    "updated_at": now,
                }
            ),
        )

    def mark_failed(
        self,
        attempt_id: str,
        *,
        now: float,
        error: str,
    ) -> ExecutionPreflightAttempt:
        return self.update(
            attempt_id,
            lambda current: current.model_copy(
                update={
                    "status": "failed",
                    "retry_claim_id": None,
                    "retry_started_at": None,
                    "last_error": error,
                    "updated_at": now,
                }
            ),
        )

    def status(self) -> dict[str, Any]:
        return {
            "namespace": self.NAMESPACE,
            "revision": self.store.namespace_revision(self.NAMESPACE),
        }
