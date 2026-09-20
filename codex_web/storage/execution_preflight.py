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

    def status(self) -> dict[str, Any]:
        return {
            "namespace": self.NAMESPACE,
            "revision": self.store.namespace_revision(self.NAMESPACE),
        }
