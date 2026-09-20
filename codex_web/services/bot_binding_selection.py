from __future__ import annotations

from typing import Any, Callable

from codex_web.models import BotBinding


class BotBindingSelectionService:
    """Own deterministic bot binding lookup and primary/master selection."""

    def __init__(
        self,
        load_bindings: Callable[[], list[BotBinding]],
        *,
        binding_report_name: Callable[[BotBinding], str],
        binding_prefix: Callable[[BotBinding], str],
        lookup_by_id: Callable[[str], BotBinding | None] | None = None,
        indexed_for_connection: Callable[
            [str, str],
            list[BotBinding],
        ] | None = None,
        indexed_for_thread: Callable[[str], list[BotBinding]] | None = None,
        indexed_for_project: Callable[
            [str, str],
            list[BotBinding],
        ] | None = None,
        indexed_masters: Callable[[str], list[BotBinding]] | None = None,
    ) -> None:
        self.load_bindings = load_bindings
        self.binding_report_name = binding_report_name
        self.binding_prefix = binding_prefix
        self.lookup_by_id = lookup_by_id
        self.indexed_for_connection = indexed_for_connection
        self.indexed_for_thread = indexed_for_thread
        self.indexed_for_project = indexed_for_project
        self.indexed_masters = indexed_masters

    def by_id(self, binding_id: str) -> BotBinding | None:
        if self.lookup_by_id is not None:
            return self.lookup_by_id(binding_id)
        return next(
            (
                binding
                for binding in self.load_bindings()
                if binding.id == binding_id
            ),
            None,
        )

    def for_connection(self, provider: str, external_conversation_id: str) -> list[BotBinding]:
        normalized_provider = provider.lower()
        if self.indexed_for_connection is not None:
            return self.indexed_for_connection(
                normalized_provider,
                external_conversation_id,
            )
        return [
            binding
            for binding in self.load_bindings()
            if binding.provider == normalized_provider
            and binding.external_conversation_id == external_conversation_id
        ]

    def find_unique(self, provider: str, external_conversation_id: str) -> BotBinding | None:
        bindings = self.for_connection(provider, external_conversation_id)
        return bindings[0] if len(bindings) == 1 else None

    def first_for_connection(
        self,
        provider: str,
        external_conversation_id: str | None,
    ) -> BotBinding | None:
        if not external_conversation_id:
            return None
        bindings = self.for_connection(provider, external_conversation_id)
        return bindings[0] if bindings else None

    def for_thread(self, thread_id: str) -> list[BotBinding]:
        if self.indexed_for_thread is not None:
            return self.indexed_for_thread(thread_id)
        return [
            binding
            for binding in self.load_bindings()
            if binding.thread_id == thread_id
        ]

    def for_project(self, provider: str, project_id: str) -> list[BotBinding]:
        normalized_provider = provider.lower()
        if self.indexed_for_project is not None:
            return self.indexed_for_project(
                normalized_provider,
                project_id,
            )
        return [
            binding
            for binding in self.load_bindings()
            if binding.provider == normalized_provider and binding.project_id == project_id
        ]

    def master(self, project_id: str) -> BotBinding | None:
        masters = (
            self.indexed_masters(project_id)
            if self.indexed_masters is not None
            else [
                binding
                for binding in self.load_bindings()
                if binding.project_id == project_id and binding.is_master
            ]
        )
        if not masters:
            return None
        return max(masters, key=lambda binding: binding.updated_at)

    def orchestrator(self, project_id: str) -> BotBinding | None:
        masters = sorted(
            (
                self.indexed_masters(project_id)
                if self.indexed_masters is not None
                else [
                    binding
                    for binding in self.load_bindings()
                    if binding.project_id == project_id
                    and binding.is_master
                ]
            ),
            key=lambda binding: binding.updated_at,
            reverse=True,
        )
        for binding in masters:
            if (self.binding_report_name(binding) or "").strip().lower() in {
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
        project_bindings = self.for_project(provider, project_id)
        masters = [binding for binding in project_bindings if binding.is_master]
        if not masters:
            return None
        preferred_thread = next(
            (
                binding.thread_id
                for binding in masters
                if (self.binding_prefix(binding) or "").strip().lower()
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


def install_bot_binding_selection_service(
    app: Any,
    host: Any,
    *,
    load_bindings: Callable[[], list[BotBinding]] | None = None,
    binding_report_name: Callable[[BotBinding], str] | None = None,
    binding_prefix: Callable[[BotBinding], str] | None = None,
) -> BotBindingSelectionService:
    service = BotBindingSelectionService(
        load_bindings or host._load_bot_bindings,
        binding_report_name=binding_report_name or host._binding_report_name,
        binding_prefix=binding_prefix or host._binding_prefix,
        lookup_by_id=lookup_by_id,
        indexed_for_connection=indexed_for_connection,
        indexed_for_thread=indexed_for_thread,
        indexed_for_project=indexed_for_project,
        indexed_masters=indexed_masters,
    )
    app.state.bot_binding_selection_service = service

    host._bot_binding_by_id = service.by_id
    host._find_bot_binding = service.find_unique
    host._first_binding_for_connection = service.first_for_connection
    host._bindings_for_connection = service.for_connection
    host._bindings_for_thread = service.for_thread
    host._bindings_for_project = service.for_project
    host._master_binding = service.master
    host._orchestrator_binding = service.orchestrator
    host._primary_binding_for_project = service.primary_for_project
    return service
