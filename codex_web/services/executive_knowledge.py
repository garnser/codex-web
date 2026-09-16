from __future__ import annotations

import re
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from codex_web.paths import STATE_DB_FILE
from codex_web.storage.sqlite_state import SQLiteStateStore


class ExecutiveKnowledgeEntry(BaseModel):
    id: str
    scope: Literal["company", "project"] = "company"
    project_id: str | None = None
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=20000)
    tags: list[str] = Field(default_factory=list)
    source: str = "manual"
    priority: int = Field(default=50, ge=0, le=100)
    created_at: float
    updated_at: float


class ExecutiveKnowledgeUpsert(BaseModel):
    id: str | None = None
    scope: Literal["company", "project"] = "company"
    project_id: str | None = None
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=20000)
    tags: list[str] = Field(default_factory=list)
    source: str = Field(default="manual", max_length=300)
    priority: int = Field(default=50, ge=0, le=100)


class ExecutiveKnowledgeStore:
    NAMESPACE = "executive_knowledge"

    def __init__(self, host: Any) -> None:
        app_state = getattr(getattr(getattr(host, "app", None), "state", None), "sqlite_state_store", None)
        self.store = app_state or SQLiteStateStore(STATE_DB_FILE)

    def _rows(self) -> dict[str, dict[str, Any]]:
        payload = self.store.get(self.NAMESPACE)
        return dict(payload) if isinstance(payload, dict) else {}

    @staticmethod
    def _normalize_tags(tags: list[str]) -> list[str]:
        return sorted({tag.strip().lower() for tag in tags if tag.strip()})[:30]

    def upsert(self, payload: ExecutiveKnowledgeUpsert) -> ExecutiveKnowledgeEntry:
        if payload.scope == "project" and not (payload.project_id or "").strip():
            raise ValueError("project_id is required for project-scoped knowledge")
        entry_id = (payload.id or str(uuid.uuid4())).strip()
        now = time.time()

        def update(current: Any) -> dict[str, dict[str, Any]]:
            rows = dict(current) if isinstance(current, dict) else {}
            existing = rows.get(entry_id) if isinstance(rows.get(entry_id), dict) else {}
            entry = ExecutiveKnowledgeEntry(
                id=entry_id,
                scope=payload.scope,
                project_id=(payload.project_id or "").strip() or None,
                title=payload.title.strip(),
                content=payload.content.strip(),
                tags=self._normalize_tags(payload.tags),
                source=payload.source.strip() or "manual",
                priority=payload.priority,
                created_at=float(existing.get("created_at") or now),
                updated_at=now,
            )
            rows[entry_id] = entry.model_dump()
            return rows

        updated = self.store.update(self.NAMESPACE, update, default={})
        return ExecutiveKnowledgeEntry.model_validate(updated[entry_id])

    def delete(self, entry_id: str) -> bool:
        removed = False

        def update(current: Any) -> dict[str, dict[str, Any]]:
            nonlocal removed
            rows = dict(current) if isinstance(current, dict) else {}
            removed = rows.pop(entry_id, None) is not None
            return rows

        self.store.update(self.NAMESPACE, update, default={})
        return removed

    def list(
        self,
        *,
        scope: str | None = None,
        project_id: str | None = None,
    ) -> list[ExecutiveKnowledgeEntry]:
        entries = [ExecutiveKnowledgeEntry.model_validate(row) for row in self._rows().values()]
        if scope:
            entries = [entry for entry in entries if entry.scope == scope]
        if project_id:
            entries = [
                entry
                for entry in entries
                if entry.scope == "company" or entry.project_id == project_id
            ]
        entries.sort(key=lambda entry: (-entry.priority, -entry.updated_at, entry.title.lower()))
        return entries

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9][a-z0-9_.-]{2,}", text.lower())
            if token not in {"the", "and", "for", "with", "this", "that", "from", "into", "our", "your"}
        }

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        limit: int = 8,
    ) -> list[ExecutiveKnowledgeEntry]:
        query_terms = self._terms(query)
        entries = self.list(project_id=project_id)
        if not query_terms:
            return entries[: max(1, min(limit, 20))]

        scored: list[tuple[float, ExecutiveKnowledgeEntry]] = []
        for entry in entries:
            title_terms = self._terms(entry.title)
            tag_terms = set(entry.tags)
            body_terms = self._terms(entry.content)
            overlap = (
                len(query_terms & title_terms) * 5
                + len(query_terms & tag_terms) * 4
                + len(query_terms & body_terms)
            )
            if overlap <= 0:
                continue
            scope_bonus = 2 if project_id and entry.project_id == project_id else 0
            score = float(overlap + scope_bonus) + (entry.priority / 100.0)
            scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], -item[1].updated_at))
        return [entry for _, entry in scored[: max(1, min(limit, 20))]]

    def prompt_for(self, query: str, *, project_id: str | None = None, limit: int = 8) -> str:
        entries = self.search(query, project_id=project_id, limit=limit)
        if not entries:
            return ""
        rows = []
        for entry in entries:
            scope = f"project:{entry.project_id}" if entry.scope == "project" else "company"
            tags = f" tags={','.join(entry.tags)}" if entry.tags else ""
            rows.append(
                f"- [{scope}] {entry.title} (source={entry.source}{tags})\n  {entry.content}"
            )
        return "\n".join(rows)
