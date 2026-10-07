from __future__ import annotations

import threading
import time
from typing import Any

from codex_web.agent_runtime import AgentRuntimeEvent
from codex_web.storage.state_store import StateStore


class ThreadTranscriptService:
    """Durable provider-neutral user/assistant history for CLI runtimes."""

    NAMESPACE = "thread_transcripts"
    MAX_ITEMS = 1000

    _RECOVERY_MESSAGES = {
        "assignment_lease_expired": (
            "This turn was interrupted because its execution lease expired. "
            "Please retry your message."
        ),
        "assignment_worker_not_trusted": (
            "This turn was interrupted because its execution worker became "
            "unavailable. Please retry your message."
        ),
        "active_assignment_missing_lease": (
            "This turn was interrupted because its execution lost its lease. "
            "Please retry your message."
        ),
        "operator_interrupted": "This turn was interrupted by an operator.",
    }

    def __init__(self, store: StateStore, *, clock=time.time) -> None:
        self.store = store
        self.clock = clock
        self._lock = threading.RLock()

    def _update(self, thread_id: str, updater) -> dict[str, Any]:
        with self._lock:
            return self.store.record_update(
                self.NAMESPACE,
                thread_id,
                updater,
                default={"threadId": thread_id, "turns": []},
            )

    @staticmethod
    def _turn_for_event(
        turns: list[dict[str, Any]],
        turn_id: str | None,
    ) -> dict[str, Any] | None:
        if turn_id:
            for turn in reversed(turns):
                if turn.get("runtimeTurnId") == turn_id or turn.get("id") == turn_id:
                    return turn
        return next(
            (
                turn
                for turn in reversed(turns)
                if turn.get("status") == "inProgress"
            ),
            None,
        )

    @classmethod
    def _bound_items(cls, turns: list[dict[str, Any]]) -> None:
        total = sum(len(turn.get("items") or []) for turn in turns)
        while turns and total > cls.MAX_ITEMS:
            total -= len(turns[0].get("items") or [])
            turns.pop(0)

    def record_user(
        self,
        thread_id: str,
        turn_id: str,
        text: str,
        *,
        created_at: float | None = None,
    ) -> None:
        value = str(text or "").strip()
        if not thread_id or not turn_id or not value:
            return
        timestamp = float(created_at or self.clock())

        def update(raw: Any) -> dict[str, Any]:
            payload = dict(raw) if isinstance(raw, dict) else {}
            turns = list(payload.get("turns") or [])
            if any(turn.get("id") == turn_id for turn in turns):
                return {**payload, "threadId": thread_id, "turns": turns}
            turns.append(
                {
                    "id": turn_id,
                    "runtimeTurnId": None,
                    "status": "inProgress",
                    "startedAt": timestamp,
                    "completedAt": None,
                    "items": [
                        {
                            "id": f"user-{turn_id}",
                            "type": "userMessage",
                            "content": [{"type": "text", "text": value}],
                            "createdAt": timestamp,
                        }
                    ],
                }
            )
            self._bound_items(turns)
            return {**payload, "threadId": thread_id, "turns": turns}

        self._update(thread_id, update)

    def fail_pending(self, thread_id: str, turn_id: str) -> None:
        if not thread_id or not turn_id:
            return
        now = float(self.clock())

        def update(raw: Any) -> dict[str, Any]:
            payload = dict(raw) if isinstance(raw, dict) else {}
            turns = list(payload.get("turns") or [])
            for turn in reversed(turns):
                if turn.get("id") != turn_id:
                    continue
                if turn.get("status") == "inProgress":
                    turn["status"] = "failed"
                    turn["completedAt"] = now
                break
            return {**payload, "threadId": thread_id, "turns": turns}

        self._update(thread_id, update)

    def reconcile_terminal(
        self,
        thread_id: str,
        turn_id: str | None,
        outcome: str,
        reason_code: str,
        *,
        completed_at: float | None = None,
    ) -> None:
        """Project canonical stale-turn recovery into durable chat history."""
        if not thread_id:
            return
        now = float(completed_at or self.clock())
        successful = outcome == "terminal"
        message = self._RECOVERY_MESSAGES.get(
            reason_code,
            (
                "This turn was interrupted after its execution stopped "
                "responding. Please retry your message."
            ),
        )
        if outcome == "requeued":
            message = (
                "The previous execution stopped responding. Your queued "
                "message will continue automatically."
            )

        def update(raw: Any) -> dict[str, Any]:
            payload = dict(raw) if isinstance(raw, dict) else {}
            turns = list(payload.get("turns") or [])
            turn = self._turn_for_event(turns, turn_id)
            if turn is None:
                return {**payload, "threadId": thread_id, "turns": turns}
            if turn.get("status") != "inProgress":
                return {**payload, "threadId": thread_id, "turns": turns}
            turn["status"] = "completed" if successful else "failed"
            turn["completedAt"] = now
            if not successful:
                turn["error"] = message
                turn["failure"] = {
                    "code": reason_code,
                    "message": message,
                    "retryable": outcome != "terminal",
                }
                items = list(turn.get("items") or [])
                recovery_item_id = f"recovery-{turn.get('id') or thread_id}"
                if not any(item.get("id") == recovery_item_id for item in items):
                    items.append(
                        {
                            "id": recovery_item_id,
                            "type": "agentMessage",
                            "text": message,
                            "createdAt": now,
                        }
                    )
                turn["items"] = items
            self._bound_items(turns)
            return {**payload, "threadId": thread_id, "turns": turns}

        self._update(thread_id, update)

    def record_event(self, thread_id: str, event: AgentRuntimeEvent) -> None:
        method = str(event.event_type or "").replace(".", "/")
        turn_id = str(event.provider_native_turn_id or "").strip() or None
        params = dict(event.payload or {})
        item_id = str(params.get("itemId") or "").strip() or None
        now = float(self.clock())

        def update(raw: Any) -> dict[str, Any]:
            payload = dict(raw) if isinstance(raw, dict) else {}
            turns = list(payload.get("turns") or [])
            turn = self._turn_for_event(turns, turn_id)
            if turn is None:
                turn = {
                    "id": turn_id or f"runtime-{int(now * 1_000_000)}",
                    "runtimeTurnId": turn_id,
                    "status": "inProgress",
                    "startedAt": now,
                    "completedAt": None,
                    "items": [],
                }
                turns.append(turn)
            elif turn_id and not turn.get("runtimeTurnId"):
                turn["runtimeTurnId"] = turn_id

            items = list(turn.get("items") or [])
            if method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                if delta:
                    effective_id = item_id or f"agent-{turn.get('runtimeTurnId') or turn['id']}"
                    item = next(
                        (
                            candidate
                            for candidate in reversed(items)
                            if candidate.get("id") == effective_id
                        ),
                        None,
                    )
                    if item is None:
                        item = {
                            "id": effective_id,
                            "type": "agentMessage",
                            "text": "",
                            "createdAt": now,
                        }
                        items.append(item)
                    item["text"] = f"{item.get('text') or ''}{delta}"
            elif method == "item/completed":
                completed = params.get("item")
                if isinstance(completed, dict):
                    completed = dict(completed)
                    completed_type = completed.get("type")
                    if completed_type == "agentMessage" and completed.get("text"):
                        effective_id = str(completed.get("id") or item_id or "")
                        existing = next(
                            (
                                candidate
                                for candidate in reversed(items)
                                if effective_id and candidate.get("id") == effective_id
                            ),
                            None,
                        )
                        if existing is None:
                            completed.setdefault("createdAt", now)
                            items.append(completed)
                        else:
                            existing.update(completed)
            elif method in {"turn/completed", "turn/failed", "turn/interrupted"}:
                turn["status"] = (
                    "completed" if method == "turn/completed" else "failed"
                )
                turn["completedAt"] = now

            turn["items"] = items
            self._bound_items(turns)
            return {**payload, "threadId": thread_id, "turns": turns}

        self._update(thread_id, update)

    def read(self, thread_id: str) -> dict[str, Any]:
        raw = self.store.record_get(self.NAMESPACE, thread_id)
        if not isinstance(raw, dict):
            return {"threadId": thread_id, "turns": []}
        return {
            "threadId": thread_id,
            "turns": list(raw.get("turns") or []),
        }
