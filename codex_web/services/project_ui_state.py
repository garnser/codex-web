from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from codex_web.identity import AuthenticationActor
from codex_web.models import BotBinding
from codex_web.services.bot_binding_selection import BotBindingSelectionService
from codex_web.services.bot_channels import BotChannelDiscoveryService
from codex_web.services.projects import ProjectService
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.thread_execution_settings import (
    ThreadExecutionSettingsService,
)
from codex_web.services.threads import ThreadService


class ProjectUiStateService:
    """Build the bounded Project workspace projection consumed by the UI.

    This is a read model over canonical services, not an independent source of
    truth. Every section is Project/tenant scoped before serialization and has
    an explicit size bound.
    """

    MAX_RESOURCES = 200
    MAX_BINDINGS = 500
    MAX_CHANNELS = 250
    DEFAULT_THREAD_LIMIT = 50
    MAX_THREAD_LIMIT = 100

    def __init__(
        self,
        *,
        projects: ProjectService,
        resources: ResourceCatalogService,
        threads: ThreadService,
        bindings: BotBindingSelectionService,
        settings: ThreadExecutionSettingsService,
        channels: BotChannelDiscoveryService,
        execution_profiles: Any,
        binding_public: Callable[[BotBinding], dict[str, Any]],
    ) -> None:
        self.projects = projects
        self.resources = resources
        self.threads = threads
        self.bindings = bindings
        self.settings = settings
        self.channels = channels
        self.execution_profiles = execution_profiles
        self.binding_public = binding_public

    @staticmethod
    def _enum_value(value: Any) -> str:
        return str(getattr(value, "value", value))

    @staticmethod
    def _version(value: Any) -> str:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()[:20]

    @classmethod
    def _repository_resources(
        cls,
        values: list[Any],
    ) -> tuple[list[dict[str, Any]], bool]:
        repositories = [
            item
            for item in values
            if cls._enum_value(getattr(item, "resource_type", ""))
            == "repository"
            and cls._enum_value(getattr(item, "lifecycle", ""))
            == "active"
        ]
        truncated = len(repositories) > cls.MAX_RESOURCES
        return (
            [
                item.model_dump(mode="json")
                for item in repositories[: cls.MAX_RESOURCES]
            ],
            truncated,
        )

    def _visible_bindings(
        self,
        project_id: str,
        visible_thread_ids: set[str],
    ) -> tuple[list[dict[str, Any]], bool]:
        project_bindings = self.bindings.for_project_all(project_id)
        relevant = [
            item
            for item in project_bindings
            if item.is_master or item.thread_id in visible_thread_ids
        ]
        relevant.sort(
            key=lambda item: (
                not item.is_master,
                -float(item.updated_at or 0),
                item.id,
            )
        )
        truncated = len(relevant) > self.MAX_BINDINGS
        return (
            [
                self.binding_public(item)
                for item in relevant[: self.MAX_BINDINGS]
            ],
            truncated,
        )

    def _visible_settings(
        self,
        thread_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        return {
            thread_id: self.settings.get(thread_id).model_dump(
                mode="json"
            )
            for thread_id in thread_ids
        }

    @classmethod
    def _bounded_channels(
        cls,
        discovered: list[dict[str, str]],
        bindings: list[dict[str, Any]],
    ) -> tuple[list[dict[str, str]], bool]:
        by_key = {
            (
                str(item.get("provider") or ""),
                str(item.get("id") or ""),
            ): dict(item)
            for item in discovered
            if item.get("provider") and item.get("id")
        }
        priority: list[tuple[str, str]] = []
        for binding in bindings:
            key = (
                str(binding.get("provider") or ""),
                str(
                    binding.get("external_conversation_id")
                    or ""
                ),
            )
            if not all(key):
                continue
            if key not in priority:
                priority.append(key)
            by_key.setdefault(
                key,
                {
                    "provider": key[0],
                    "id": key[1],
                    "name": str(
                        binding.get("external_name") or ""
                    ),
                    "label": str(
                        binding.get("external_name")
                        or key[1]
                    ),
                },
            )

        ordered: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for key in priority:
            item = by_key.get(key)
            if item is not None and key not in seen:
                ordered.append(item)
                seen.add(key)
        for key, item in sorted(
            by_key.items(),
            key=lambda pair: (
                str(pair[1].get("provider") or ""),
                str(
                    pair[1].get("label")
                    or pair[1].get("name")
                    or pair[1].get("id")
                    or ""
                ).casefold(),
                pair[0],
            ),
        ):
            if key in seen:
                continue
            ordered.append(item)
            seen.add(key)

        truncated = len(ordered) > cls.MAX_CHANNELS
        return ordered[: cls.MAX_CHANNELS], truncated

    def binding_state(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
        thread_id: str,
    ) -> dict[str, Any]:
        project = self.projects.get(project_id, actor.tenant)
        bindings, truncated = self._visible_bindings(
            project.id,
            {thread_id},
        )
        channels, channels_truncated = self._bounded_channels(
            [],
            bindings,
        )
        return {
            "projectId": project.id,
            "threadId": thread_id,
            "bindings": {
                "items": bindings,
                "truncated": truncated,
                "limit": self.MAX_BINDINGS,
                "version": self._version(bindings),
            },
            "channels": {
                "items": channels,
                "truncated": channels_truncated,
                "limit": self.MAX_CHANNELS,
                "version": self._version(channels),
            },
        }

    async def state(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
        search: str | None = None,
        thread_limit: int | None = None,
        thread_cursor: str | None = None,
        include_static: bool = True,
    ) -> dict[str, Any]:
        project = self.projects.get(project_id, actor.tenant)
        limit = max(
            1,
            min(
                int(thread_limit or self.DEFAULT_THREAD_LIMIT),
                self.MAX_THREAD_LIMIT,
            ),
        )
        thread_page = await self.threads.list(
            project.id,
            archived=False,
            search=search,
            limit=limit,
            cursor=thread_cursor,
        )
        thread_rows = [
            item
            for item in thread_page.get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]
        thread_ids = [str(item["id"]) for item in thread_rows]
        visible_ids = set(thread_ids)

        resource_values = self.resources.project_resources(
            project,
            actor=actor,
        )
        resources, resources_truncated = self._repository_resources(
            resource_values
        )
        bindings, bindings_truncated = self._visible_bindings(
            project.id,
            visible_ids,
        )
        settings = self._visible_settings(thread_ids)

        discovered_channels = await self.channels.list(project.id)
        channels, channels_truncated = self._bounded_channels(
            discovered_channels,
            bindings,
        )

        project_summary = (
            project.model_dump(mode="json")
            if include_static
            else None
        )
        execution_profiles = (
            self.execution_profiles.public(project_id=project.id)
            if include_static
            else None
        )
        section_versions = {
            "resources": self._version(resources),
            "threads": str(thread_page.get("revision") or ""),
            "bindings": self._version(bindings),
            "threadSettings": self._version(settings),
            "channels": self._version(channels),
        }
        if include_static:
            section_versions["project"] = self._version(project_summary)
            section_versions["executionProfiles"] = self._version(
                execution_profiles
            )

        return {
            "project": project_summary,
            "resources": {
                "items": resources,
                "truncated": resources_truncated,
                "limit": self.MAX_RESOURCES,
            },
            "threads": thread_page,
            "bindings": {
                "items": bindings,
                "truncated": bindings_truncated,
                "limit": self.MAX_BINDINGS,
            },
            "threadSettings": settings,
            "channels": {
                "items": channels,
                "truncated": channels_truncated,
                "limit": self.MAX_CHANNELS,
                "discovery": self.channels.status(project.id),
            },
            "executionProfiles": execution_profiles,
            "meta": {
                "projectId": project.id,
                "threadCount": len(thread_rows),
                "bindingCount": len(bindings),
                "settingCount": len(settings),
                "channelCount": len(channels),
                "resourceCount": len(resources),
                "staticIncluded": include_static,
                "contractVersion": 1,
                "sectionVersions": section_versions,
            },
        }

    @staticmethod
    def etag(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return f'"{hashlib.sha256(encoded).hexdigest()}"'
