from __future__ import annotations

import contextlib
from typing import Any

from fastapi import HTTPException

from codex_web.models import (
    ThreadPrimaryChannelUpdate,
    ThreadPrimaryUpdate,
    ThreadRunSettings,
)


class ThreadService:
    """Thread/query operations that do not own turn queue orchestration yet."""

    def __init__(self, host: Any) -> None:
        self.host = host

    async def list(
        self,
        project_id: str | None = None,
        archived: bool = False,
        search: str | None = None,
    ) -> dict[str, Any]:
        project_path = self.host._project(project_id).path if project_id else None
        params: dict[str, Any] = {
            "limit": 100,
            "archived": archived,
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "sourceKinds": ["appServer", "cli", "vscode", "exec"],
        }
        if project_path:
            params["cwd"] = project_path
        if search:
            params["searchTerm"] = search
        try:
            result = await self.host.codex.request("thread/list", params)
        except Exception as exc:
            if archived:
                raise HTTPException(status_code=504, detail=str(exc)) from exc
            result = {"data": []}
        if archived:
            return result

        items = result.get("data") or result.get("threads") or []
        indexed_threads = self.host._load_thread_index()
        indexed_by_id = {indexed.id: indexed for indexed in indexed_threads}
        active_turns = self.host._load_active_turns()
        for item in items:
            if not isinstance(item, dict):
                continue
            indexed = indexed_by_id.get(item.get("id"))
            if indexed:
                item["name"] = indexed.name
                item["updatedAt"] = max(
                    item.get("updatedAt") or 0,
                    indexed.updatedAt or 0,
                    active_turns.get(indexed.id).updated_at if indexed.id in active_turns else 0,
                )
        existing_ids = {item.get("id") for item in items if isinstance(item, dict)}
        search_term = search.casefold() if search else None
        for indexed in indexed_threads:
            if indexed.id in existing_ids:
                continue
            if project_path and indexed.cwd and indexed.cwd != project_path:
                continue
            if search_term and search_term not in indexed.name.casefold():
                continue
            item: dict[str, Any] = {
                "id": indexed.id,
                "sessionId": indexed.id,
                "preview": "",
                "name": indexed.name,
                "cwd": indexed.cwd,
                "path": indexed.path,
                "updatedAt": max(
                    indexed.updatedAt or 0,
                    active_turns.get(indexed.id).updated_at if indexed.id in active_turns else 0,
                ),
                "status": {"type": "notLoaded"},
                "turns": [],
            }
            with contextlib.suppress(Exception):
                thread_response = await self.host.codex.request(
                    "thread/read",
                    {"threadId": indexed.id, "includeTurns": False},
                )
                thread = thread_response.get("thread", thread_response) if isinstance(thread_response, dict) else {}
                item.update(thread)
                item["name"] = indexed.name
                item["updatedAt"] = max(
                    item.get("updatedAt") or 0,
                    indexed.updatedAt or 0,
                    active_turns.get(indexed.id).updated_at if indexed.id in active_turns else 0,
                )
            items.append(item)
            existing_ids.add(indexed.id)

        items.sort(key=lambda item: item.get("updatedAt") or 0, reverse=True)
        if "data" in result:
            result["data"] = items
        elif "threads" in result:
            result["threads"] = items
        else:
            result["data"] = items
        return result

    async def create(
        self,
        project_id: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        project = self.host._project(project_id)
        response = await self.host.codex.request(
            "thread/start",
            self.host._project_params(
                project,
                {
                    "sessionStartSource": "startup",
                    "sandbox": sandbox,
                    "approvalPolicy": approval_policy,
                    "model": model,
                },
            ),
        )
        thread = response.get("thread", response)
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if thread_id:
            self.host._remember_thread_run_settings(
                thread_id,
                sandbox=sandbox or project.sandbox,
                approval_policy=approval_policy or project.approval_policy,
                model=model or project.model,
                reasoning_effort=reasoning_effort,
                developer_instructions=None,
            )
        return response

    async def read(
        self,
        thread_id: str,
        message_limit: int | None = None,
        turn_limit: int | None = None,
    ) -> dict[str, Any]:
        self.host._raise_if_thread_replaced(thread_id)
        limit = self.host._coerce_thread_message_limit(
            message_limit if message_limit is not None else turn_limit
        )
        resume_task = self.host.WEB_THREAD_RESUME_TASKS.get(thread_id)
        if resume_task and not resume_task.done():
            return self.host._thread_read_timeout_response(
                thread_id,
                limit,
                "thread/resume still in progress",
                event_type="web_read_deferred_for_resume",
            )
        try:
            response = await self.host.codex.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
            )
        except Exception as exc:
            if self.host._is_codex_timeout_error(exc):
                return self.host._thread_read_timeout_response(thread_id, limit, exc)
            raise
        return self.host._trim_thread_messages(response, limit)

    async def rename(self, thread_id: str, name: str) -> dict[str, Any]:
        self.host._raise_if_thread_replaced(thread_id)
        await self.host._set_thread_name(thread_id, name)
        return {"ok": True, "threadId": thread_id, "name": name}

    def update_settings(self, thread_id: str, payload: ThreadRunSettings) -> dict[str, Any]:
        self.host._raise_if_thread_replaced(thread_id)
        settings = self.host._remember_thread_run_settings(
            thread_id,
            sandbox=payload.sandbox,
            approval_policy=payload.approval_policy,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            developer_instructions=payload.developer_instructions,
        )
        return {"ok": True, "threadId": thread_id, **settings.model_dump()}

    def list_settings(self) -> dict[str, Any]:
        return {
            thread_id: settings.model_dump()
            for thread_id, settings in self.host._load_thread_settings().items()
        }

    def get_settings(self, thread_id: str) -> dict[str, Any]:
        self.host._raise_if_thread_replaced(thread_id)
        return {"threadId": thread_id, **self.host._thread_run_settings(thread_id).model_dump()}

    async def update_primary(self, thread_id: str, payload: ThreadPrimaryUpdate) -> dict[str, Any]:
        bindings = await self.host._set_thread_primary(thread_id, payload.project_id, payload.primary)
        return {
            "ok": True,
            "threadId": thread_id,
            "projectId": payload.project_id,
            "primary": payload.primary,
            "bindings": [binding.model_dump() for binding in bindings],
        }

    async def update_primary_channel(
        self,
        thread_id: str,
        payload: ThreadPrimaryChannelUpdate,
    ) -> dict[str, Any]:
        bindings = await self.host._set_thread_primary_channel(
            thread_id,
            payload.project_id,
            payload.provider,
            payload.external_conversation_id,
        )
        return {
            "ok": True,
            "threadId": thread_id,
            "projectId": payload.project_id,
            "provider": payload.provider,
            "externalConversationId": payload.external_conversation_id,
            "bindings": [binding.model_dump() for binding in bindings],
        }

    async def archive(self, thread_id: str) -> dict[str, Any]:
        return await self.host.codex.request("thread/archive", {"threadId": thread_id})

    async def unarchive(self, thread_id: str) -> dict[str, Any]:
        return await self.host.codex.request("thread/unarchive", {"threadId": thread_id})

    async def interrupt(self, thread_id: str) -> dict[str, Any]:
        return await self.host.codex.request("turn/interrupt", {"threadId": thread_id})
