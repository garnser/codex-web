from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from codex_web.models import BotBinding, BotConnection
from codex_web.services.project_runtime import ProjectRuntimeService


RuntimeRequest = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
BindingLoader = Callable[[], list[BotBinding]]
BindingSaver = Callable[[list[BotBinding]], None]
ConnectionLoader = Callable[[], list[BotConnection]]
BindingUpserter = Callable[[BotBinding], BotBinding]


class ThreadBotCollaborationService:
    """Own thread primary/master and primary-channel collaboration semantics."""

    def __init__(
        self,
        projects: ProjectRuntimeService,
        runtime_request: RuntimeRequest,
        *,
        load_bindings: BindingLoader,
        save_bindings: BindingSaver,
        load_connections: ConnectionLoader,
        upsert_binding: BindingUpserter,
    ) -> None:
        self.projects = projects
        self.runtime_request = runtime_request
        self.load_bindings = load_bindings
        self.save_bindings = save_bindings
        self.load_connections = load_connections
        self.upsert_binding = upsert_binding

    async def project_scoped_bindings_for_thread(
        self,
        thread_id: str,
    ) -> list[BotBinding]:
        try:
            thread_response = await self.runtime_request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": False},
            )
        except Exception:
            return []
        thread = (
            thread_response.get("thread", thread_response)
            if isinstance(thread_response, dict)
            else {}
        )
        project = self.projects.find_by_cwd(thread.get("cwd"))
        if project is None:
            return []
        thread_name = (
            thread.get("name")
            or thread.get("agentNickname")
            or thread_id
        )
        now = time.time()
        rows: list[BotBinding] = []
        for connection in self.load_connections():
            if (
                connection.project_id != project.id
                or not connection.default_external_conversation_id
            ):
                continue
            rows.append(
                self.upsert_binding(
                    BotBinding(
                        id=uuid.uuid4().hex[:12],
                        connection_id=connection.id,
                        provider=connection.provider,
                        external_conversation_id=(
                            connection.default_external_conversation_id
                        ),
                        thread_id=thread_id,
                        project_id=project.id,
                        external_name=connection.default_external_name,
                        thread_name=thread_name,
                        route_prefix=thread_name,
                        is_primary_channel=False,
                        post_in_thread=False,
                        sandbox=project.sandbox,
                        approval_policy=project.approval_policy,
                        created_at=now,
                        updated_at=now,
                    )
                )
            )
        return rows

    async def set_primary(
        self,
        thread_id: str,
        project_id: str,
        primary: bool,
    ) -> list[BotBinding]:
        project = self.projects.get(project_id)
        bindings = self.load_bindings()
        if primary and not any(
            binding.thread_id == thread_id
            and binding.project_id == project.id
            for binding in bindings
        ):
            await self.project_scoped_bindings_for_thread(thread_id)
            bindings = self.load_bindings()

        changed = False
        now = time.time()
        for binding in bindings:
            if binding.project_id != project.id:
                continue
            next_master = bool(primary and binding.thread_id == thread_id)
            if binding.is_master != next_master:
                binding.is_master = next_master
                binding.updated_at = now
                changed = True
        if changed:
            self.save_bindings(bindings)
        return [
            binding
            for binding in bindings
            if binding.project_id == project.id
        ]

    async def set_primary_channel(
        self,
        thread_id: str,
        project_id: str,
        provider: str,
        external_conversation_id: str | None,
    ) -> list[BotBinding]:
        project = self.projects.get(project_id)
        normalized_provider = provider.lower()
        if normalized_provider not in {"slack", "telegram"}:
            raise HTTPException(
                status_code=400,
                detail="Provider must be slack or telegram",
            )

        bindings = self.load_bindings()
        if external_conversation_id and not any(
            binding.thread_id == thread_id
            and binding.project_id == project.id
            and binding.provider == normalized_provider
            and binding.external_conversation_id == external_conversation_id
            for binding in bindings
        ):
            candidates = [
                binding
                for binding in bindings
                if binding.thread_id == thread_id
                and binding.project_id == project.id
                and binding.provider == normalized_provider
            ]
            if not candidates:
                candidates = await self.project_scoped_bindings_for_thread(
                    thread_id
                )
            source = next(
                (
                    binding
                    for binding in candidates
                    if binding.provider == normalized_provider
                ),
                None,
            )
            if source is None:
                connection = next(
                    (
                        item
                        for item in self.load_connections()
                        if item.project_id == project.id
                        and item.provider == normalized_provider
                    ),
                    None,
                )
                if connection is None:
                    raise HTTPException(
                        status_code=400,
                        detail="No bot connection is configured for this project",
                    )
                source = self.upsert_binding(
                    BotBinding(
                        id=uuid.uuid4().hex[:12],
                        connection_id=connection.id,
                        provider=normalized_provider,
                        external_conversation_id=external_conversation_id,
                        thread_id=thread_id,
                        project_id=project.id,
                        external_name=external_conversation_id,
                        is_master=False,
                        is_primary_channel=False,
                        post_in_thread=False,
                        sandbox=project.sandbox,
                        approval_policy=project.approval_policy,
                        created_at=time.time(),
                        updated_at=time.time(),
                    )
                )
            else:
                source = self.upsert_binding(
                    BotBinding(
                        **{
                            **source.model_dump(),
                            "id": uuid.uuid4().hex[:12],
                            "external_conversation_id": external_conversation_id,
                            "external_name": external_conversation_id,
                            "is_master": False,
                            "is_primary_channel": False,
                            "created_at": time.time(),
                            "updated_at": time.time(),
                        }
                    )
                )
            bindings = self.load_bindings()

        changed = False
        now = time.time()
        for binding in bindings:
            if (
                binding.thread_id != thread_id
                or binding.project_id != project.id
                or binding.provider != normalized_provider
            ):
                continue
            next_primary = bool(
                external_conversation_id
                and binding.external_conversation_id
                == external_conversation_id
            )
            if binding.is_primary_channel != next_primary:
                binding.is_primary_channel = next_primary
                binding.updated_at = now
                changed = True
        if changed:
            self.save_bindings(bindings)
        return [
            binding
            for binding in bindings
            if binding.thread_id == thread_id
            and binding.project_id == project.id
        ]
