from __future__ import annotations

import copy
import time
from typing import Any

from codex_web.storage.state_store import StateStore


class ThreadHistoryRepository:
    """Durable provider-neutral user/assistant history for CLI threads.

    Provider runtimes remain the authority for their native session history.
    This projection preserves the small, user-visible transcript that codex-web
    already emits on the canonical thread event bus so a CLI process is not
    required merely to render a thread.
    """

    NAMESPACE = "thread_history"
    SCHEMA_VERSION = "1.0"
    MAX_TURNS = 1000

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @classmethod
    def _empty(cls, thread_id: str) -> dict[str, Any]:
        return {
            "schemaVersion": cls.SCHEMA_VERSION,
            "threadId": thread_id,
            "providerId": None,
            "runtimeId": None,
            "turns": [],
            "updatedAt": 0.0,
        }

    @classmethod
    def _normalize(cls, value: Any, thread_id: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            return cls._empty(thread_id)
        state = copy.deepcopy(value)
        if state.get("schemaVersion") != cls.SCHEMA_VERSION:
            raise RuntimeError(
                "unsupported canonical thread history schema version: "
                f"{state.get('schemaVersion')!r}"
            )
        state["threadId"] = thread_id
        if not isinstance(state.get("turns"), list):
            state["turns"] = []
        return state

    @staticmethod
    def _turn_id(params: dict[str, Any]) -> str | None:
        turn = params.get("turn")
        value = params.get("turnId") or (
            turn.get("id") if isinstance(turn, dict) else None
        )
        normalized = str(value or "").strip()
        return normalized or None

    @staticmethod
    def _find_turn(
        state: dict[str, Any],
        provider_turn_id: str | None,
        *,
        now: float,
    ) -> dict[str, Any]:
        turns = state["turns"]
        if provider_turn_id:
            for turn in reversed(turns):
                if provider_turn_id in {
                    str(turn.get("id") or ""),
                    str(turn.get("providerTurnId") or ""),
                }:
                    return turn
        for turn in reversed(turns):
            if turn.get("status") == "inProgress":
                if provider_turn_id and not turn.get("providerTurnId"):
                    turn["providerTurnId"] = provider_turn_id
                return turn
        turn_id = provider_turn_id or f"projected-{int(now * 1_000_000)}"
        turn = {
            "id": turn_id,
            "providerTurnId": provider_turn_id,
            "status": "inProgress",
            "items": [],
            "startedAt": now,
            "completedAt": None,
        }
        turns.append(turn)
        return turn

    @staticmethod
    def _agent_item(
        turn: dict[str, Any],
        *,
        item_id: str | None,
        text: str | None = None,
    ) -> dict[str, Any] | None:
        items = turn.setdefault("items", [])
        if item_id:
            for item in reversed(items):
                if (
                    item.get("type") == "agentMessage"
                    and str(item.get("id") or "") == item_id
                ):
                    return item
        for item in reversed(items):
            if item.get("type") != "agentMessage":
                continue
            if item.get("status") == "inProgress" or (
                text is not None and str(item.get("text") or "") == text
            ):
                if item_id and not item.get("providerItemId"):
                    item["providerItemId"] = item_id
                return item
        return None

    def start_turn(
        self,
        thread_id: str,
        *,
        turn_id: str,
        message: str,
        provider_id: str,
        runtime_id: str,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        now = float(created_at if created_at is not None else time.time())

        def apply(value: Any) -> dict[str, Any]:
            state = self._normalize(value, thread_id)
            state["providerId"] = provider_id
            state["runtimeId"] = runtime_id
            turn = next(
                (
                    item
                    for item in state["turns"]
                    if str(item.get("id") or "") == turn_id
                ),
                None,
            )
            if turn is None:
                turn = {
                    "id": turn_id,
                    "providerTurnId": None,
                    "status": "inProgress",
                    "items": [],
                    "startedAt": now,
                    "completedAt": None,
                }
                state["turns"].append(turn)
            user_id = f"user-{turn_id}"
            if not any(
                str(item.get("id") or "") == user_id
                for item in turn.setdefault("items", [])
            ):
                turn["items"].append(
                    {
                        "id": user_id,
                        "type": "userMessage",
                        "content": [
                            {
                                "type": "inputText",
                                "text": message,
                            }
                        ],
                        "createdAt": now,
                    }
                )
            state["turns"] = state["turns"][-self.MAX_TURNS :]
            state["updatedAt"] = now
            return state

        return self.store.record_update(
            self.NAMESPACE,
            thread_id,
            apply,
            default=self._empty(thread_id),
        )

    def project_message(
        self,
        thread_id: str,
        message: dict[str, Any],
        *,
        recorded_at: float | None = None,
    ) -> dict[str, Any] | None:
        method = str(message.get("method") or "").strip()
        params = message.get("params")
        if not isinstance(params, dict) or method not in {
            "turn/started",
            "item/agentMessage/delta",
            "item/completed",
            "turn/completed",
            "turn/failed",
        }:
            return self.get(thread_id)
        item = params.get("item")
        if method == "item/completed" and (
            not isinstance(item, dict) or item.get("type") != "agentMessage"
        ):
            return self.get(thread_id)

        now = float(recorded_at if recorded_at is not None else time.time())

        def apply(value: Any) -> dict[str, Any]:
            state = self._normalize(value, thread_id)
            turn = self._find_turn(
                state,
                self._turn_id(params),
                now=now,
            )
            if method == "turn/started":
                turn["status"] = "inProgress"
                turn.setdefault("startedAt", now)
            elif method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                if delta:
                    item_id = str(params.get("itemId") or "").strip() or None
                    agent = self._agent_item(turn, item_id=item_id)
                    if agent is None:
                        items = turn.setdefault("items", [])
                        agent = {
                            "id": item_id or f"agent-{turn['id']}-{len(items)}",
                            "type": "agentMessage",
                            "text": "",
                            "status": "inProgress",
                            "createdAt": now,
                        }
                        items.append(agent)
                    agent["text"] = str(agent.get("text") or "") + delta
            elif method == "item/completed":
                completed = dict(item)
                item_id = str(
                    completed.get("id") or params.get("itemId") or ""
                ).strip() or None
                text = str(completed.get("text") or "")
                agent = self._agent_item(
                    turn,
                    item_id=item_id,
                    text=text,
                )
                if agent is None:
                    items = turn.setdefault("items", [])
                    agent = {
                        "id": item_id or f"agent-{turn['id']}-{len(items)}",
                        "type": "agentMessage",
                        "text": text,
                        "createdAt": now,
                    }
                    items.append(agent)
                elif text:
                    agent["text"] = text
                agent["status"] = "completed"
                agent["completedAt"] = now
            else:
                turn["status"] = (
                    "completed" if method == "turn/completed" else "failed"
                )
                turn["completedAt"] = now
            state["turns"] = state["turns"][-self.MAX_TURNS :]
            state["updatedAt"] = now
            return state

        return self.store.record_update(
            self.NAMESPACE,
            thread_id,
            apply,
            default=self._empty(thread_id),
        )

    def get(self, thread_id: str) -> dict[str, Any] | None:
        value = self.store.record_get(self.NAMESPACE, thread_id)
        if not isinstance(value, dict):
            return None
        return self._normalize(value, thread_id)

    def thread(self, thread_id: str) -> dict[str, Any] | None:
        state = self.get(thread_id)
        if state is None or not state["turns"]:
            return None
        return {
            "id": thread_id,
            "status": {"type": "idle"},
            "turns": copy.deepcopy(state["turns"]),
            "historySource": "canonical",
            "providerId": state.get("providerId"),
            "runtimeId": state.get("runtimeId"),
            "updatedAt": state.get("updatedAt"),
        }
