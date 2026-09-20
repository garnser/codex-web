from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import HTTPException

from codex_web.models import BotBinding, BotBindingCreate, BotInboundMessage
from codex_web.services.bot_binding_selection import BotBindingSelectionService
from codex_web.services.bot_connections import BotConnectionService
from codex_web.services.bot_presentation import BotPresentationService
from codex_web.services.bot_targets import BotTargetService
from codex_web.services.project_runtime import ProjectRuntimeService


class BotBindingLifecycleService:
    """Own bot binding mutation and deterministic inbound route selection."""

    def __init__(
        self,
        *,
        load_bindings: Callable[[], list[BotBinding]],
        save_bindings: Callable[[list[BotBinding]], None],
        connections: BotConnectionService,
        selection: BotBindingSelectionService,
        targets: BotTargetService,
        presentation: BotPresentationService,
        projects: ProjectRuntimeService,
        runtime_request: Callable[[str, dict], Awaitable[dict]],
        set_thread_name: Callable[[str, str], Awaitable[object]],
        on_change: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.load_bindings = load_bindings
        self.save_bindings = save_bindings
        self.connections = connections
        self.selection = selection
        self.targets = targets
        self.presentation = presentation
        self.projects = projects
        self.runtime_request = runtime_request
        self.set_thread_name = set_thread_name
        self.on_change = on_change

    def upsert(self, new_binding: BotBinding) -> BotBinding:
        bindings = self.load_bindings()
        updated = False
        for index, binding in enumerate(bindings):
            if (
                new_binding.is_master
                and binding.provider == new_binding.provider
                and binding.project_id == new_binding.project_id
                and binding.thread_id != new_binding.thread_id
            ):
                binding.is_master = False
            if (
                binding.provider == new_binding.provider
                and binding.external_conversation_id
                == new_binding.external_conversation_id
                and binding.thread_id == new_binding.thread_id
            ):
                new_binding.id = binding.id
                new_binding.created_at = binding.created_at
                bindings[index] = new_binding
                updated = True
                break
        if not updated:
            bindings.append(new_binding)
        self.save_bindings(bindings)
        self.connections.dedupe_integrations()
        if self.on_change is not None:
            self.on_change(
                {
                    "type": "binding.updated",
                    "projectId": new_binding.project_id,
                    "threadId": new_binding.thread_id,
                    "bindingId": new_binding.id,
                    "updatedAt": new_binding.updated_at,
                }
            )
        return new_binding

    def remove(self, binding_id: str) -> None:
        bindings = self.load_bindings()
        removed = next(
            (binding for binding in bindings if binding.id == binding_id),
            None,
        )
        self.save_bindings(
            [binding for binding in bindings if binding.id != binding_id]
        )
        if removed is not None and self.on_change is not None:
            self.on_change(
                {
                    "type": "binding.updated",
                    "projectId": removed.project_id,
                    "threadId": removed.thread_id,
                    "bindingId": removed.id,
                    "removed": True,
                    "updatedAt": time.time(),
                }
            )

    async def start(self, payload: BotBindingCreate) -> BotBinding:
        connection = (
            self.connections.get(payload.connection_id)
            if payload.connection_id
            else None
        )
        provider = (
            payload.provider or (connection.provider if connection else "")
        ).lower()
        if provider not in {"slack", "telegram"}:
            raise HTTPException(
                status_code=400,
                detail="Provider must be slack or telegram",
            )
        project_id = (
            payload.project_id
            or (connection.project_id if connection else payload.project_id)
        )
        external_conversation_id = (
            payload.external_conversation_id
            or (
                connection.default_external_conversation_id
                if connection
                else None
            )
        )
        if not external_conversation_id:
            raise HTTPException(
                status_code=400,
                detail="External conversation id is required",
            )
        project = self.projects.get(project_id)
        now = time.time()
        if payload.thread_id:
            thread_id = payload.thread_id
        else:
            response = await self.runtime_request(
                "thread/start",
                self.projects.params(
                    project,
                    {
                        "sandbox": payload.sandbox,
                        "approvalPolicy": payload.approval_policy,
                        "sessionStartSource": "startup",
                    },
                ),
            )
            thread_id = response["thread"]["id"]

        thread_name = payload.thread_name or payload.route_prefix
        if thread_name:
            await self.set_thread_name(thread_id, thread_name)
        return self.upsert(
            BotBinding(
                id=uuid.uuid4().hex[:12],
                connection_id=(
                    connection.id if connection else payload.connection_id
                ),
                provider=provider,
                external_conversation_id=external_conversation_id,
                external_name=(
                    payload.external_name
                    or (
                        connection.default_external_name
                        if connection
                        else None
                    )
                ),
                thread_name=thread_name,
                route_prefix=payload.route_prefix or thread_name,
                is_master=payload.is_master,
                is_primary_channel=payload.is_primary_channel,
                post_in_thread=payload.post_in_thread,
                thread_id=thread_id,
                project_id=project.id,
                sandbox=payload.sandbox,
                approval_policy=payload.approval_policy,
                created_at=now,
                updated_at=now,
            )
        )

    def clone_to_conversation(
        self,
        source: BotBinding,
        external_conversation_id: str,
        *,
        connection_id: str | None = None,
        external_name: str | None = None,
        is_primary_channel: bool = False,
    ) -> BotBinding:
        now = time.time()
        return self.upsert(
            BotBinding(
                id=uuid.uuid4().hex[:12],
                connection_id=connection_id or source.connection_id,
                provider=source.provider,
                external_conversation_id=external_conversation_id,
                thread_id=source.thread_id,
                project_id=source.project_id,
                external_name=external_name or external_conversation_id,
                thread_name=source.thread_name,
                route_prefix=source.route_prefix,
                is_master=False,
                is_primary_channel=is_primary_channel,
                post_in_thread=source.post_in_thread,
                sandbox=source.sandbox,
                approval_policy=source.approval_policy,
                created_at=now,
                updated_at=now,
            )
        )

    def clone_for_conversation(
        self,
        source: BotBinding,
        message: BotInboundMessage,
    ) -> BotBinding:
        return self.clone_to_conversation(
            source,
            message.external_conversation_id,
            connection_id=message.connection_id or source.connection_id,
            external_name=message.external_name or source.external_name,
            is_primary_channel=False,
        )

    def for_external_target(
        self,
        provider: str,
        project_id: str,
        external_conversation_id: str,
        external_thread_id: str | None,
    ) -> BotBinding | None:
        target = self.targets.target_for_external_thread(
            provider,
            external_conversation_id,
            external_thread_id,
        )
        if target is None:
            return None
        candidates = [
            binding
            for binding in self.selection.for_project(provider, project_id)
            if binding.thread_id == target.thread_id
        ]
        for binding in candidates:
            if binding.external_conversation_id == external_conversation_id:
                return binding
        return candidates[0] if candidates else None

    @staticmethod
    def is_top_level_external_message(message: BotInboundMessage) -> bool:
        return bool(
            message.message_id
            and message.external_thread_id
            and message.external_thread_id == message.message_id
        )

    @staticmethod
    def has_single_master_binding(bindings: list[BotBinding]) -> bool:
        return len([binding for binding in bindings if binding.is_master]) == 1

    def resolve(
        self,
        bindings: list[BotBinding],
        message: BotInboundMessage,
        *,
        prefer_external_thread: bool = True,
        allow_master_fallback: bool = True,
        allow_bare_prefix: bool = False,
    ) -> tuple[BotBinding | None, str, bool]:
        text = message.text
        if not bindings:
            return None, text, False
        if prefer_external_thread:
            target = self.targets.target_for_external_thread(
                message.provider,
                message.external_conversation_id,
                message.external_thread_id,
            )
            if target is not None:
                by_thread_id = {
                    binding.thread_id: binding for binding in bindings
                }
                binding = by_thread_id.get(target.thread_id)
                if binding is not None:
                    return binding, text, False
        for binding in sorted(
            bindings,
            key=lambda item: (
                len(self.presentation.binding_prefix(item) or ""),
                item.updated_at,
            ),
            reverse=True,
        ):
            for prefix in self.presentation.binding_prefix_candidates(binding):
                stripped = self.presentation.strip_prefix(
                    text,
                    prefix,
                    allow_bare_word=allow_bare_prefix,
                )
                if stripped is not None:
                    return binding, stripped, False
        if allow_master_fallback:
            masters = [binding for binding in bindings if binding.is_master]
            if len(masters) == 1:
                return masters[0], text, False
        return None, text, allow_master_fallback

    def cross_channel_for_message(
        self,
        provider: str,
        project_id: str,
        current_bindings: list[BotBinding],
        message: BotInboundMessage,
        *,
        allow_bare_prefix: bool = False,
    ) -> tuple[BotBinding | None, str, bool]:
        current_by_thread_id = {
            binding.thread_id: binding for binding in current_bindings
        }
        candidates = [
            binding
            for binding in self.selection.for_project(provider, project_id)
            if binding.external_conversation_id
            != message.external_conversation_id
        ]
        matches: dict[str, tuple[BotBinding, str]] = {}
        for binding in sorted(
            candidates,
            key=lambda item: (
                len(self.presentation.binding_prefix(item) or ""),
                item.updated_at,
            ),
            reverse=True,
        ):
            for prefix in self.presentation.binding_prefix_candidates(binding):
                stripped = self.presentation.strip_prefix(
                    message.text,
                    prefix,
                    allow_bare_word=allow_bare_prefix,
                )
                if stripped is None:
                    continue
                if binding.thread_id in current_by_thread_id:
                    return (
                        current_by_thread_id[binding.thread_id],
                        stripped,
                        False,
                    )
                matches.setdefault(binding.thread_id, (binding, stripped))
                break
        if len(matches) == 1:
            return next(iter(matches.values()))[0], next(
                iter(matches.values())
            )[1], False
        if len(matches) > 1:
            return None, message.text, True
        primary = self.selection.primary_for_project(
            provider,
            project_id,
            message.external_conversation_id,
        )
        if primary:
            return primary, message.text, False
        if len(current_bindings) == 1:
            return current_bindings[0], message.text, False
        return None, message.text, False

    def fallback_for_stale(
        self,
        binding: BotBinding,
        message: BotInboundMessage,
    ) -> BotBinding | None:
        candidates = [
            candidate
            for candidate in self.selection.for_project(
                binding.provider,
                binding.project_id,
            )
            if candidate.thread_id != binding.thread_id
        ]
        prefix = self.presentation.binding_prefix(binding)
        if prefix:
            for candidate in candidates:
                if (
                    self.presentation.binding_prefix(candidate).lower()
                    == prefix.lower()
                ):
                    return candidate
        masters = [candidate for candidate in candidates if candidate.is_master]
        if len(masters) == 1:
            return masters[0]
        return None


def install_bot_binding_lifecycle_service(
    app,
    host,
    *,
    load_bindings=None,
    save_bindings=None,
    connections=None,
    selection=None,
    targets=None,
    presentation=None,
    projects=None,
    runtime_request=None,
    set_thread_name=None,
    on_change=None,
) -> BotBindingLifecycleService:
    service = BotBindingLifecycleService(
        load_bindings=load_bindings or host._load_bot_bindings,
        save_bindings=save_bindings or host._save_bot_bindings,
        connections=connections or app.state.bot_connection_service,
        selection=selection or app.state.bot_binding_selection_service,
        targets=targets or app.state.bot_target_service,
        presentation=presentation or app.state.bot_presentation_service,
        projects=projects or app.state.project_runtime_service,
        runtime_request=runtime_request or host.codex.request,
        set_thread_name=set_thread_name or host._set_thread_name,
        on_change=on_change,
    )
    app.state.bot_binding_lifecycle_service = service

    # Compatibility aliases for verified historical consumers.
    host._upsert_bot_binding = service.upsert
    host._remove_bot_binding = service.remove
    host._start_bot_thread = service.start
    host._clone_binding_to_conversation = service.clone_to_conversation
    host._clone_binding_for_conversation = service.clone_for_conversation
    host._binding_for_external_target = service.for_external_target
    host._cross_channel_binding_for_message = service.cross_channel_for_message
    host._is_top_level_external_message = service.is_top_level_external_message
    host._has_single_master_binding = service.has_single_master_binding
    host._resolve_bot_binding = service.resolve
    host._fallback_binding_for_stale = service.fallback_for_stale
    return service
