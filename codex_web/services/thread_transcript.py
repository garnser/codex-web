from __future__ import annotations

import time
from typing import Any

from codex_web.agent_runtime import AgentRuntimeEvent
from codex_web.storage.state_store import StateStore
from codex_web.storage.thread_history import ThreadHistoryRepository


class ThreadTranscriptService:
    """Compatibility adapter over the canonical provider-neutral thread history."""

    NAMESPACE = ThreadHistoryRepository.NAMESPACE

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
        self.history = ThreadHistoryRepository(store)

    def read(self, thread_id: str) -> dict[str, Any]:
        return self.history.get(thread_id) or self.history._empty(thread_id)

    def record_user(self, thread_id: str, turn_id: str, text: str) -> None:
        current = self.read(thread_id)
        self.history.start_turn(thread_id, turn_id=turn_id, message=text,
            provider_id=current.get("providerId") or "openai",
            runtime_id=current.get("runtimeId") or "codex", created_at=self.clock())

    def record_event(self, thread_id: str, event: AgentRuntimeEvent) -> None:
        params = dict(event.payload or {})
        if event.provider_native_turn_id:
            params["turnId"] = event.provider_native_turn_id
        self.history.project_message(thread_id,
            {"method": event.event_type, "params": params}, recorded_at=self.clock())

    def fail_pending(self, thread_id: str, turn_id: str) -> None:
        self.reconcile_terminal(thread_id, turn_id, "failed", "runtime_start_failed")

    def reconcile_terminal(self, thread_id: str, turn_id: str, outcome: str,
                           reason_code: str, *, completed_at: float | None = None) -> None:
        now = self.clock() if completed_at is None else completed_at
        def update(value):
            state = self.history._normalize(value, thread_id)
            turn = next((item for item in reversed(state["turns"])
                         if turn_id in {item.get("id"), item.get("providerTurnId")}), None)
            if turn is None or turn.get("status") in {"completed", "failed"}:
                return state
            turn["status"] = "completed" if outcome == "completed" else "failed"
            turn["completedAt"] = now
            if outcome != "completed":
                turn["failure"] = {"code": reason_code, "retryable": True}
                text = self._RECOVERY_MESSAGES.get(reason_code)
                if text:
                    turn.setdefault("items", []).append({
                        "id": f"recovery-{turn_id}", "type": "agentMessage", "text": text,
                        "status": "completed", "createdAt": now, "completedAt": now})
            state["updatedAt"] = now
            return state
        self.store.record_update(self.NAMESPACE, thread_id, update,
                                 default=self.history._empty(thread_id))
