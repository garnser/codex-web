from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

from codex_web.executive import CompanyContext
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.sqlite_state import SQLiteStateStore


class ExecutiveStateStore:
    """SQLite-primary Executive state with token-aware history reads."""

    def __init__(self, host: Any) -> None:
        self.host = host
        self.data_dir = Path(getattr(host, "DATA_DIR", Path("data")))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        shared = getattr(getattr(getattr(host, "app", None), "state", None), "sqlite_state_store", None)
        self.store = shared or SQLiteStateStore(self.data_dir / "codex-web.db")
        self.context_file = self.data_dir / "executive_company.json"
        self.sessions_file = self.data_dir / "executive_sessions.json"
        self.thread_map_file = self.data_dir / "executive_codex_threads.json"
        self.max_history = max(8, int(os.environ.get("CODEX_WEB_EXECUTIVE_MAX_HISTORY", "200")))
        self.max_context_tokens = max(1000, int(os.environ.get("CODEX_WEB_EXECUTIVE_MAX_CONTEXT_TOKENS", "12000")))
        self.compact_target_tokens = max(
            500,
            min(
                self.max_context_tokens - 250,
                int(os.environ.get("CODEX_WEB_EXECUTIVE_COMPACT_TARGET_TOKENS", str(self.max_context_tokens // 2))),
            ),
        )
        self._lock = threading.RLock()

    @staticmethod
    def estimate_tokens(text: str) -> int:
        # Provider-neutral conservative approximation. Exact tokenizers vary by
        # model, but this keeps context growth bounded for all supported backends.
        return max(1, math.ceil(len(text) / 4))

    @classmethod
    def history_tokens(cls, rows: list[dict[str, Any]]) -> int:
        return sum(cls.estimate_tokens(str(row.get("content") or "")) + 8 for row in rows)

    def _legacy(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return default

    def _load(self, namespace: str, path: Path, default: Any) -> Any:
        payload = self.store.get(namespace)
        if payload is None:
            payload = self._legacy(path, default)
            self.store.put(namespace, payload)
        return payload

    def _mirror(self, path: Path, payload: Any) -> None:
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        writer = getattr(self.host, "_atomic_write_text", None)
        if writer is not None:
            writer(path, text, private=True)
        else:
            atomic_write_text(path, text, private=True)

    def _save(self, namespace: str, path: Path, payload: Any) -> None:
        self.store.put(namespace, payload)
        self._mirror(path, payload)

    def company(self) -> CompanyContext:
        with self._lock:
            payload = self._load("executive_company", self.context_file, {})
        return CompanyContext.model_validate(payload if isinstance(payload, dict) else {})

    def save_company(self, company: CompanyContext) -> CompanyContext:
        with self._lock:
            self._save("executive_company", self.context_file, company.model_dump())
        return company

    def _sessions(self) -> dict[str, list[dict[str, Any]]]:
        payload = self._load("executive_sessions", self.sessions_file, {})
        return payload if isinstance(payload, dict) else {}

    def _bounded(self, rows: list[dict[str, Any]], budget: int | None = None) -> list[dict[str, Any]]:
        budget = budget or self.max_context_tokens
        selected: list[dict[str, Any]] = []
        used = 0
        for row in reversed(rows[-self.max_history :]):
            if not isinstance(row, dict) or not row.get("role") or not row.get("content"):
                continue
            cost = self.estimate_tokens(str(row["content"])) + 8
            if selected and used + cost > budget:
                break
            selected.append(row)
            used += cost
        selected.reverse()
        return selected

    def history(self, session_id: str) -> list[dict[str, str]]:
        with self._lock:
            rows = self._sessions().get(session_id, [])
            if not isinstance(rows, list):
                return []
            bounded = self._bounded(rows)
        return [
            {"role": str(row["role"]), "content": str(row["content"])}
            for row in bounded
        ]

    def append_history(self, session_id: str, role: str, content: str) -> None:
        with self._lock:
            sessions = self._sessions()
            rows = sessions.get(session_id, [])
            if not isinstance(rows, list):
                rows = []
            rows.append({"role": role, "content": content, "at": time.time()})
            sessions[session_id] = rows[-self.max_history :]
            self._save("executive_sessions", self.sessions_file, sessions)

    def compaction_plan(self, session_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        with self._lock:
            rows = self._sessions().get(session_id, [])
            if not isinstance(rows, list) or self.history_tokens(rows) <= self.max_context_tokens:
                return None
            recent = self._bounded(rows, self.compact_target_tokens)
            old_count = max(0, len(rows) - len(recent))
            if old_count <= 0:
                return None
            return rows[:old_count], recent

    def replace_history(self, session_id: str, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            sessions = self._sessions()
            sessions[session_id] = rows[-self.max_history :]
            self._save("executive_sessions", self.sessions_file, sessions)

    def thread_for(self, project_id: str, agent_id: str) -> str | None:
        with self._lock:
            mapping = self._load("executive_thread_map", self.thread_map_file, {})
        value = mapping.get(f"{project_id}:{agent_id}") if isinstance(mapping, dict) else None
        return str(value) if value else None

    def remember_thread(self, project_id: str, agent_id: str, thread_id: str) -> None:
        with self._lock:
            mapping = self._load("executive_thread_map", self.thread_map_file, {})
            mapping = dict(mapping) if isinstance(mapping, dict) else {}
            mapping[f"{project_id}:{agent_id}"] = thread_id
            self._save("executive_thread_map", self.thread_map_file, mapping)

    def thread_map(self) -> dict[str, str]:
        with self._lock:
            payload = self._load("executive_thread_map", self.thread_map_file, {})
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items()}
