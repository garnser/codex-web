from __future__ import annotations

from typing import Any, Callable

from codex_web.models import ExecutionPreflightAttempt
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
        return ExecutionPreflightAttempt.model_validate(payload)


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
            if attempt is not None:
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
            if not isinstance(payload, dict):
                raise KeyError(attempt_id)
            current = ExecutionPreflightAttempt.model_validate(payload)
            if current.status == "started":
                return current.model_dump(mode="json")
            if current.status == "retrying":
                if current.retry_claim_id == claim_id:
                    acquired = True
                    return current.model_dump(mode="json")
                retry_started_at = current.retry_started_at or current.updated_at
                if now - retry_started_at < max(1.0, stale_after_seconds):
                    return current.model_dump(mode="json")
            acquired = True
            return current.model_copy(
                update={
                    "status": "retrying",
                    "attempt_number": current.attempt_number + 1,
                    "retry_claim_id": claim_id,
                    "retry_started_at": now,
                    "last_error": None,
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
            upserts={self._thread_key(attempt): attempt.model_dump(mode="json")},
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
        normalized = tuple(blockers)

        def apply(current: ExecutionPreflightAttempt):
            history = tuple(
                (
                    *current.blocker_history,
                    {
                        "blockers": normalized,
                        "recorded_at": now,
                        "attempt_number": current.attempt_number,
                    },
                )[-20:]
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
