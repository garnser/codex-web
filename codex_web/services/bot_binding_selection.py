from __future__ import annotations

from typing import Any, Callable

from codex_web.models import BotBinding


class BotBindingSelectionService:
    """Own deterministic bot binding lookup and primary/master selection."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def _override(self, name: str, fallback: Callable[..., Any]) -> Callable[..., Any]:
        candidate = getattr(self.host, name, None)
        return candidate if callable(candidate) else fallback

    def for_connection(self, provider: str, external_conversation_id: str) -> list[BotBinding]:
        normalized_provider = provider.lower()
        return [
            binding
            for binding in self.host._load_bot_bindings()
            if binding.provider == normalized_provider
            and binding.external_conversation_id == external_conversation_id
        ]

    def find_unique(self, provider: str, external_conversation_id: str) -> BotBinding | None:
        bindings = self._override("_bindings_for_connection", self.for_connection)(
            provider,
            external_conversation_id,
        )
        return bindings[0] if len(bindings) == 1 else None

    def first_for_connection(
        self,
        provider: str,
        external_conversation_id: str | None,
    ) -> BotBinding | None:
        if not external_conversation_id:
            return None
        bindings = self._override("_bindings_for_connection", self.for_connection)(
            provider,
            external_conversation_id,
        )
        return bindings[0] if bindings else None

    def for_thread(self, thread_id: str) -> list[BotBinding]:
        return [
            binding
            for binding in self.host._load_bot_bindings()
            if binding.thread_id == thread_id
        ]

    def for_project(self, provider: str, project_id: str) -> list[BotBinding]:
        normalized_provider = provider.lower()
        return [
            binding
            for binding in self.host._load_bot_bindings()
            if binding.provider == normalized_provider and binding.project_id == project_id
        ]

    def master(self, project_id: str) -> BotBinding | None:
        masters = [
            binding
            for binding in self.host._load_bot_bindings()
            if binding.project_id == project_id and binding.is_master
        ]
        if not masters:
            return None
        return max(masters, key=lambda binding: binding.updated_at)

    def orchestrator(self, project_id: str) -> BotBinding | None:
        masters = sorted(
            [
                binding
                for binding in self.host._load_bot_bindings()
                if binding.project_id == project_id and binding.is_master
            ],
            key=lambda binding: binding.updated_at,
            reverse=True,
        )
        for binding in masters:
            if (self.host._binding_report_name(binding) or "").strip().lower() in {
                "orchestrator",
                "codex",
            }:
                return binding
        return masters[0] if masters else None

    def primary_for_project(
        self,
        provider: str,
        project_id: str,
        external_conversation_id: str | None = None,
    ) -> BotBinding | None:
        bindings_for_project = self._override("_bindings_for_project", self.for_project)
        project_bindings = bindings_for_project(provider, project_id)
        masters = [binding for binding in project_bindings if binding.is_master]
        if not masters:
            return None
        preferred_thread = next(
            (
                binding.thread_id
                for binding in masters
                if (self.host._binding_prefix(binding) or "").strip().lower()
                in {"orchestrator", "codex"}
            ),
            None,
        )
        if not preferred_thread:
            thread_ids = {binding.thread_id for binding in masters}
            if len(thread_ids) == 1:
                preferred_thread = next(iter(thread_ids))
        if not preferred_thread:
            preferred_thread = max(masters, key=lambda binding: binding.updated_at).thread_id
        same_channel = [
            binding
            for binding in project_bindings
            if binding.thread_id == preferred_thread
            and binding.external_conversation_id == external_conversation_id
        ]
        if same_channel:
            return same_channel[0]
        for binding in masters:
            if binding.thread_id == preferred_thread:
                return binding
        return None


def install_bot_binding_selection_service(app: Any, host: Any) -> BotBindingSelectionService:
    existing = getattr(app.state, "bot_binding_selection_service", None)
    if isinstance(existing, BotBindingSelectionService) and existing.host is host:
        service = existing
    else:
        service = BotBindingSelectionService(host)
        app.state.bot_binding_selection_service = service

    host._find_bot_binding = service.find_unique
    host._first_binding_for_connection = service.first_for_connection
    host._bindings_for_connection = service.for_connection
    host._bindings_for_thread = service.for_thread
    host._bindings_for_project = service.for_project
    host._master_binding = service.master
    host._orchestrator_binding = service.orchestrator
    host._primary_binding_for_project = service.primary_for_project
    return service
