from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from codex_web.models import BotBinding, IndexedThread, Project
from codex_web.services.project_runtime import ProjectRuntimeService
from codex_web.services.thread_execution_settings import ThreadExecutionSettingsService
from codex_web.services.thread_naming import ThreadNamingService
from codex_web.storage.thread_index import ThreadIndexRepository


class ThreadRecoveryService:
    """Own stale-thread replacement and state retargeting.

    The compatibility host still stores replacement/failure maps while the
    migration is in progress, but all replacement coordination lives here so
    callers share one implementation. Subdomain mutations such as bot details
    remain delegated through their host compatibility entrypoints.
    """

    def __init__(
        self,
        host: Any,
        *,
        projects: ProjectRuntimeService,
        settings: ThreadExecutionSettingsService,
        naming: ThreadNamingService,
        thread_index: ThreadIndexRepository,
        runtime_request: Callable[
            [str, dict[str, Any]],
            Awaitable[dict[str, Any]],
        ],
        event_sink: Callable[[dict[str, Any]], None],
        truncate_text: Callable[[str, int], str],
    ) -> None:
        self.host = host
        self.projects = projects
        self.settings = settings
        self.naming = naming
        self.thread_index = thread_index
        self.runtime_request = runtime_request
        self.event_sink = event_sink
        self.truncate_text = truncate_text

    def logical_binding_name(self, binding: BotBinding) -> str:
        h = self.host
        name = h._binding_report_name(binding) or binding.thread_name or h._binding_prefix(binding)
        return name.strip().lower()

    def same_logical_binding(self, candidate: BotBinding, source: BotBinding) -> bool:
        return (
            candidate.provider == source.provider
            and candidate.project_id == source.project_id
            and self.logical_binding_name(candidate) == self.logical_binding_name(source)
        )

    @staticmethod
    def preferred_binding_for_replacement(source: BotBinding, bindings: list[BotBinding]) -> BotBinding:
        for binding in bindings:
            if binding.external_conversation_id == source.external_conversation_id:
                return binding
        return bindings[0] if bindings else source

    def retarget_logical_bot_bindings(self, source: BotBinding, new_thread_id: str) -> BotBinding:
        h = self.host
        bindings = h._load_bot_bindings()
        now = time.time()
        changed: list[BotBinding] = []
        canonical_thread_name = source.thread_name or source.route_prefix or h._binding_prefix(source)
        for binding in bindings:
            if binding.thread_id != source.thread_id and not self.same_logical_binding(binding, source):
                continue
            binding.thread_id = new_thread_id
            if not binding.thread_name and canonical_thread_name:
                binding.thread_name = canonical_thread_name
            binding.sandbox = source.sandbox
            binding.approval_policy = source.approval_policy
            binding.updated_at = now
            changed.append(binding)
        if not changed:
            source.thread_id = new_thread_id
            source.updated_at = now
            changed.append(source)
            bindings.append(source)
        h._save_bot_bindings(bindings)
        h._dedupe_bot_integrations()
        return self.preferred_binding_for_replacement(source, changed)

    def retarget_thread_settings(self, old_thread_id: str, new_thread_id: str) -> None:
        self.settings.retarget(old_thread_id, new_thread_id)

    def retarget_active_turn(self, old_thread_id: str, new_thread_id: str) -> None:
        del new_thread_id
        h = self.host
        active_turns = h._load_active_turns()
        if active_turns.pop(old_thread_id, None) is not None:
            # A replacement is a new Codex session. Migrating the old active
            # marker would make the replacement look permanently busy.
            h._save_active_turns(active_turns)

    def retarget_turn_queue(self, old_thread_id: str, new_thread_id: str) -> None:
        h = self.host
        queues = h._load_turn_queues()
        queued = queues.pop(old_thread_id, [])
        if not queued:
            return
        for item in queued:
            item.thread_id = new_thread_id
            if item.reply_target and item.reply_target.thread_id == old_thread_id:
                item.reply_target = item.reply_target.model_copy(update={"thread_id": new_thread_id})
        queues.setdefault(new_thread_id, []).extend(queued)
        h._save_turn_queues(queues)

    def retarget_slack_thread_icon(self, old_thread_id: str, new_thread_id: str) -> None:
        h = self.host
        icons = h._load_slack_thread_icons()
        icon = icons.pop(old_thread_id, None)
        if icon and new_thread_id not in icons:
            icons[new_thread_id] = icon
        if icon:
            h._save_slack_thread_icons(icons)

    def retarget_bot_thread_state(self, old_thread_id: str, new_thread_id: str) -> None:
        h = self.host
        h._retarget_bot_targets(old_thread_id, new_thread_id)
        self.retarget_thread_settings(old_thread_id, new_thread_id)
        self.retarget_active_turn(old_thread_id, new_thread_id)
        self.retarget_turn_queue(old_thread_id, new_thread_id)
        h._retarget_bot_details(old_thread_id, new_thread_id)
        self.retarget_slack_thread_icon(old_thread_id, new_thread_id)

    async def archive_replaced_bot_thread(self, old_thread_id: str, new_thread_id: str) -> bool:
        h = self.host
        self.thread_index.remove(old_thread_id)
        try:
            await self.runtime_request("thread/archive", {"threadId": old_thread_id})
        except Exception as exc:
            self.event_sink(
                {
                    "type": "stale_bot_thread_archive_failed",
                    "old_thread_id": old_thread_id,
                    "new_thread_id": new_thread_id,
                    "error": self.truncate_text(str(exc), 500),
                }
            )
            return False
        self.event_sink(
            {
                "type": "stale_bot_thread_archived",
                "old_thread_id": old_thread_id,
                "new_thread_id": new_thread_id,
            }
        )
        return True

    def _remember_replacement(self, old_thread_id: str, new_thread_id: str) -> None:
        h = self.host
        for prior_thread_id, replacement_thread_id in list(h.THREAD_REPLACEMENTS.items()):
            if replacement_thread_id == old_thread_id:
                h.THREAD_REPLACEMENTS[prior_thread_id] = new_thread_id
        h.THREAD_REPLACEMENTS[old_thread_id] = new_thread_id
        h.THREAD_TERMINAL_FAILURES.pop(old_thread_id, None)

    async def replace_stale_bot_thread(self, binding: BotBinding, error: str) -> BotBinding:
        h = self.host
        old_thread_id = binding.thread_id
        project = self.projects.get(binding.project_id)
        settings = self.settings.get(old_thread_id)
        sandbox = binding.sandbox or settings.sandbox or project.sandbox
        approval_policy = binding.approval_policy or settings.approval_policy or project.approval_policy
        thread_name = binding.thread_name or binding.route_prefix or h._binding_prefix(binding)

        replacement_params = self.projects.params(
            project,
            {
                "sandbox": sandbox,
                "approvalPolicy": approval_policy,
                "sessionStartSource": "bot-thread-replacement",
            },
        )
        try:
            response = await self.runtime_request("thread/start", replacement_params)
        except Exception as exc:
            text = str(exc).lower()
            if "unknown variant `bot-thread-replacement`" not in text and "sessionstartsource" not in text:
                raise
            replacement_params = self.projects.params(
                project,
                {
                    "sandbox": sandbox,
                    "approvalPolicy": approval_policy,
                    "sessionStartSource": "startup",
                },
            )
            response = await self.runtime_request("thread/start", replacement_params)

        new_thread_id = response["thread"]["id"]
        self.settings.remember(
            new_thread_id,
            sandbox=sandbox,
            approval_policy=approval_policy,
            model=settings.model,
            reasoning_effort=settings.reasoning_effort,
            developer_instructions=settings.developer_instructions,
        )
        if thread_name:
            with contextlib.suppress(Exception):
                await self.naming.set_name(new_thread_id, thread_name)
            self.thread_index.upsert(
                IndexedThread(
                    id=new_thread_id,
                    name=thread_name,
                    cwd=project.path,
                    path=project.path,
                    updatedAt=time.time(),
                )
            )

        # Use host entrypoints deliberately: they are the compatibility seams
        # used by existing tests/consumers while each subdomain is extracted.
        replacement = h._retarget_logical_bot_bindings(
            binding.model_copy(update={"sandbox": sandbox, "approval_policy": approval_policy}),
            new_thread_id,
        )
        self._remember_replacement(old_thread_id, new_thread_id)
        h._retarget_bot_thread_state(old_thread_id, new_thread_id)
        archived_old_thread = await h._archive_replaced_bot_thread(old_thread_id, new_thread_id)
        self.event_sink(
            {
                "type": "stale_bot_thread_replaced",
                "provider": binding.provider,
                "project_id": binding.project_id,
                "logical_name": h._logical_binding_name(binding),
                "old_thread_id": old_thread_id,
                "new_thread_id": new_thread_id,
                "archived_old_thread": archived_old_thread,
                "error": self.truncate_text(error, 500),
            }
        )
        await h.hub.publish(
            {
                "type": "bot.thread.replaced",
                "provider": binding.provider,
                "projectId": binding.project_id,
                "oldThreadId": old_thread_id,
                "newThreadId": new_thread_id,
                "name": thread_name,
            }
        )
        return replacement

    async def replace_stale_web_thread(self, thread_id: str, project: Project, error: str) -> str:
        h = self.host
        settings = self.settings.get(thread_id)
        response = await self.runtime_request(
            "thread/start",
            self.projects.params(
                project,
                {
                    "sandbox": settings.sandbox or project.sandbox,
                    "approvalPolicy": settings.approval_policy or project.approval_policy,
                    "sessionStartSource": "startup",
                },
            ),
        )
        new_thread_id = response["thread"]["id"]
        self.settings.remember(
            new_thread_id,
            sandbox=settings.sandbox or project.sandbox,
            approval_policy=settings.approval_policy or project.approval_policy,
            model=settings.model,
            reasoning_effort=settings.reasoning_effort,
            developer_instructions=settings.developer_instructions,
        )
        indexed = next((item for item in self.thread_index.load() if item.id == thread_id), None)
        thread_name = indexed.name if indexed else None
        if thread_name:
            with contextlib.suppress(Exception):
                await self.naming.set_name(new_thread_id, thread_name)
            self.thread_index.upsert(
                IndexedThread(
                    id=new_thread_id,
                    name=thread_name,
                    cwd=project.path,
                    path=project.path,
                    updatedAt=time.time(),
                )
            )
        self._remember_replacement(thread_id, new_thread_id)
        h._retarget_bot_thread_state(thread_id, new_thread_id)
        archived_old_thread = await h._archive_replaced_bot_thread(thread_id, new_thread_id)
        self.event_sink(
            {
                "type": "stale_web_thread_replaced",
                "project_id": project.id,
                "old_thread_id": thread_id,
                "new_thread_id": new_thread_id,
                "archived_old_thread": archived_old_thread,
                "error": self.truncate_text(error, 500),
            }
        )
        await h.hub.publish(
            {
                "type": "bot.thread.replaced",
                "projectId": project.id,
                "oldThreadId": thread_id,
                "newThreadId": new_thread_id,
                "name": thread_name,
            }
        )
        return new_thread_id

    def active_turn_stale_seconds(self) -> float:
        try:
            seconds = float(os.environ.get("CODEX_WEB_ACTIVE_TURN_STALE_SECONDS") or "120")
        except ValueError:
            return 120.0
        return max(30.0, seconds)

    def active_turn_is_stale(self, thread_id: str | None, max_age: float | None = None) -> bool:
        if not thread_id:
            return False
        effective_max_age = self.active_turn_stale_seconds() if max_age is None else max_age
        active = self.host._load_active_turns().get(thread_id)
        return bool(active and time.time() - active.updated_at > effective_max_age)

    def release_stale_active_turn(self, thread_id: str | None, reason: str) -> None:
        if not thread_id or not self.active_turn_is_stale(thread_id):
            return
        self.host._append_bot_event(
            {"type": "stale_active_turn_released", "thread_id": thread_id, "reason": reason}
        )
        self.host._clear_thread_active(thread_id)

    def replacement_thread_id(self, thread_id: str) -> str | None:
        replacement = self.host.THREAD_REPLACEMENTS.get(thread_id)
        seen = {thread_id}
        while replacement and replacement not in seen:
            seen.add(replacement)
            next_replacement = self.host.THREAD_REPLACEMENTS.get(replacement)
            if not next_replacement:
                return replacement
            replacement = next_replacement
        return replacement

    def raise_if_thread_replaced(self, thread_id: str) -> None:
        replacement = self.replacement_thread_id(thread_id)
        if not replacement:
            return
        raise HTTPException(
            status_code=409,
            detail={
                "code": "thread_replaced",
                "staleThreadReplaced": True,
                "oldThreadId": thread_id,
                "newThreadId": replacement,
                "threadId": replacement,
            },
        )


def install_thread_recovery_service(
    app: Any,
    host: Any,
    *,
    projects: ProjectRuntimeService,
    settings: ThreadExecutionSettingsService,
    naming: ThreadNamingService,
    thread_index: ThreadIndexRepository,
    runtime_request: Callable[
        [str, dict[str, Any]],
        Awaitable[dict[str, Any]],
    ],
) -> ThreadRecoveryService:
    service = ThreadRecoveryService(
        host,
        projects=projects,
        settings=settings,
        naming=naming,
        thread_index=thread_index,
        runtime_request=runtime_request,
        event_sink=host._append_bot_event,
        truncate_text=host._truncate_text,
    )
    app.state.thread_recovery_service = service

    # Compatibility aliases for direct import-server callers.
    host._logical_binding_name = service.logical_binding_name
    host._same_logical_binding = service.same_logical_binding
    host._preferred_binding_for_replacement = service.preferred_binding_for_replacement
    host._retarget_logical_bot_bindings = service.retarget_logical_bot_bindings
    host._retarget_thread_settings = service.retarget_thread_settings
    host._retarget_active_turn = service.retarget_active_turn
    host._retarget_turn_queue = service.retarget_turn_queue
    host._retarget_slack_thread_icon = service.retarget_slack_thread_icon
    host._retarget_bot_thread_state = service.retarget_bot_thread_state
    host._archive_replaced_bot_thread = service.archive_replaced_bot_thread
    host._replace_stale_bot_thread = service.replace_stale_bot_thread
    host._replace_stale_web_thread = service.replace_stale_web_thread
    host._active_turn_stale_seconds = service.active_turn_stale_seconds
    host._active_turn_is_stale = service.active_turn_is_stale
    host._release_stale_active_turn = service.release_stale_active_turn
    host._replacement_thread_id = service.replacement_thread_id
    host._raise_if_thread_replaced = service.raise_if_thread_replaced
    return service
