from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel

from codex_web.models import (
    AgentChannelPresenceProjectSettings,
    AgentChannelPresenceSettings,
    GitLabProjectRoutingSettings,
    GitLabRoutingSettings,
    IndexedThread,
)
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.operational_state import ModelListRepository
from codex_web.storage.sqlite_state import SQLiteStateStore


T = TypeVar("T", bound=BaseModel)


def normalize_agent_channel_mapping(raw_channels: dict[str, Any]) -> dict[str, list[str]]:
    channels: dict[str, list[str]] = {}
    for agent, values in raw_channels.items():
        normalized_agent = str(agent).strip().lower()
        if not normalized_agent:
            continue
        if isinstance(values, str):
            candidate_channels = [values]
        else:
            candidate_channels = list(values or [])
        normalized_channels = sorted(
            {
                str(channel).strip()
                for channel in candidate_channels
                if str(channel).strip()
            }
        )
        if normalized_channels:
            channels[normalized_agent] = normalized_channels
    return channels


def normalize_agent_channel_presence_project_settings(
    settings: AgentChannelPresenceProjectSettings,
) -> AgentChannelPresenceProjectSettings:
    return AgentChannelPresenceProjectSettings(
        agent_channels=normalize_agent_channel_mapping(settings.agent_channels),
    )


def normalize_agent_channel_presence_settings(
    settings: AgentChannelPresenceSettings,
) -> AgentChannelPresenceSettings:
    projects: dict[str, AgentChannelPresenceProjectSettings] = {}
    for project_id, project_settings in settings.projects.items():
        normalized_project_id = str(project_id).strip()
        if not normalized_project_id:
            continue
        projects[normalized_project_id] = normalize_agent_channel_presence_project_settings(project_settings)
    return AgentChannelPresenceSettings(projects=projects)


def migrate_agent_channel_presence_settings(raw: Any) -> AgentChannelPresenceSettings:
    if not isinstance(raw, dict):
        return AgentChannelPresenceSettings()
    raw_projects = raw.get("projects")
    projects: dict[str, AgentChannelPresenceProjectSettings] = {}
    if isinstance(raw_projects, dict):
        for project_id, project_settings in raw_projects.items():
            if not isinstance(project_settings, dict):
                continue
            channels = project_settings.get("agent_channels")
            if not isinstance(channels, dict):
                continue
            normalized_project_id = str(project_id).strip()
            if not normalized_project_id:
                continue
            projects[normalized_project_id] = AgentChannelPresenceProjectSettings(agent_channels=channels)
    else:
        legacy_project_id = str(raw.get("default_project_id") or "home").strip() or "home"
        channels = raw.get("agent_channels")
        if isinstance(channels, dict):
            projects[legacy_project_id] = AgentChannelPresenceProjectSettings(agent_channels=channels)
    return AgentChannelPresenceSettings(projects=projects)


def legacy_agent_channel_presence_from_gitlab_file(path: Path) -> AgentChannelPresenceSettings:
    if not path.exists():
        return AgentChannelPresenceSettings()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return AgentChannelPresenceSettings()
    if isinstance(raw, dict) and "projects" in raw:
        projects: dict[str, AgentChannelPresenceProjectSettings] = {}
        for project_id, project_settings in (raw.get("projects") or {}).items():
            if not isinstance(project_settings, dict):
                continue
            channels = project_settings.get("agent_channels")
            if not isinstance(channels, dict):
                continue
            normalized_project_id = str(project_id).strip()
            if not normalized_project_id:
                continue
            projects[normalized_project_id] = AgentChannelPresenceProjectSettings(agent_channels=channels)
        if projects:
            return AgentChannelPresenceSettings(projects=projects)
    return migrate_agent_channel_presence_settings(raw)


def normalize_string_list(values: list[Any] | tuple[Any, ...] | set[Any] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def normalize_gitlab_project_settings(settings: GitLabProjectRoutingSettings) -> GitLabProjectRoutingSettings:
    channel_ids = normalize_string_list(settings.channel_ids)
    project_paths = sorted({path.strip().lower() for path in settings.project_paths if path and path.strip()})
    route_agents = sorted({str(agent).strip().lower() for agent in settings.route_agents if str(agent).strip()})
    fallbacks: dict[str, list[str]] = {}
    for kind, agents in settings.fallback_agents_by_kind.items():
        normalized_kind = str(kind).strip().lower()
        normalized_agents = sorted({str(agent).strip().lower() for agent in agents if str(agent).strip()})
        if normalized_kind and normalized_agents:
            fallbacks[normalized_kind] = normalized_agents
    return GitLabProjectRoutingSettings(
        enabled=settings.enabled,
        channel_ids=channel_ids,
        route_agents=route_agents,
        project_paths=project_paths,
        fallback_agents_by_kind=fallbacks,
    )


def normalize_gitlab_routing_settings(settings: GitLabRoutingSettings) -> GitLabRoutingSettings:
    ignored = sorted({kind.strip().lower() for kind in settings.ignored_event_kinds if kind and kind.strip()})
    projects: dict[str, GitLabProjectRoutingSettings] = {}
    for project_id, project_settings in settings.projects.items():
        normalized_project_id = str(project_id).strip()
        if not normalized_project_id:
            continue
        projects[normalized_project_id] = normalize_gitlab_project_settings(project_settings)
    return GitLabRoutingSettings(
        enabled=settings.enabled,
        ignored_event_kinds=ignored or ["note", "wiki_page"],
        projects=projects,
    )


def migrate_gitlab_routing_settings(raw: Any) -> GitLabRoutingSettings:
    if not isinstance(raw, dict):
        return GitLabRoutingSettings()
    if "projects" in raw:
        projects: dict[str, GitLabProjectRoutingSettings] = {}
        for project_id, project_settings in (raw.get("projects") or {}).items():
            if not isinstance(project_settings, dict):
                continue
            normalized_project_id = str(project_id).strip()
            if not normalized_project_id:
                continue
            migrated = dict(project_settings)
            legacy_channel_id = str(migrated.get("channel_id") or "").strip()
            if legacy_channel_id and not migrated.get("channel_ids"):
                migrated["channel_ids"] = [legacy_channel_id]
            projects[normalized_project_id] = GitLabProjectRoutingSettings.model_validate(migrated)
        return GitLabRoutingSettings(
            enabled=bool(raw.get("enabled", True)),
            ignored_event_kinds=raw.get("ignored_event_kinds") or ["note", "wiki_page"],
            projects=projects or GitLabRoutingSettings().projects,
        )
    project_settings_by_id: dict[str, GitLabProjectRoutingSettings] = {}
    for mapping in raw.get("project_mappings") or []:
        if not isinstance(mapping, dict):
            continue
        project_id = str(mapping.get("project_id") or "").strip()
        namespace = str(mapping.get("namespace") or "").strip().lower()
        if not project_id:
            continue
        project_settings = project_settings_by_id.setdefault(project_id, GitLabProjectRoutingSettings())
        if namespace:
            project_settings.project_paths.append(namespace)
    if not project_settings_by_id:
        legacy_project_id = str(raw.get("default_project_id") or "home").strip() or "home"
        project_settings_by_id[legacy_project_id] = GitLabProjectRoutingSettings()
    fallback_agents = raw.get("fallback_agents_by_kind")
    enabled = bool(raw.get("enabled", True))
    for project_settings in project_settings_by_id.values():
        project_settings.enabled = enabled
        channel_id = str(raw.get("channel_id") or "").strip()
        if channel_id:
            project_settings.channel_ids = [channel_id]
        if isinstance(fallback_agents, dict):
            project_settings.fallback_agents_by_kind = fallback_agents
    return GitLabRoutingSettings(
        enabled=enabled,
        ignored_event_kinds=raw.get("ignored_event_kinds") or ["note", "wiki_page"],
        projects=project_settings_by_id,
    )


class NormalizedModelRepository(Generic[T]):
    """SQLite-primary singleton model with legacy JSON import/mirroring."""

    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        namespace: str,
        legacy_path: Path,
        model: type[T],
        normalize: Callable[[T], T],
        migrate: Callable[[Any], T],
        default: Callable[[], T],
        private: bool = True,
    ) -> None:
        self.store = store
        self.namespace = namespace
        self.legacy_path = legacy_path
        self.model = model
        self.normalize = normalize
        self.migrate = migrate
        self.default = default
        self.private = private

    def _legacy_value(self) -> T:
        if self.legacy_path.exists():
            try:
                raw = json.loads(self.legacy_path.read_text())
            except (OSError, json.JSONDecodeError):
                raw = None
            if raw is not None:
                return self.normalize(self.migrate(raw))
        return self.normalize(self.default())

    def load(self) -> T:
        payload = self.store.get(self.namespace)
        if payload is None:
            value = self._legacy_value()
            self.store.put(self.namespace, value.model_dump())
            return value
        return self.normalize(self.model.model_validate(payload))

    def save(self, value: T) -> T:
        normalized = self.normalize(value)
        payload = normalized.model_dump()
        self.store.put(self.namespace, payload)
        atomic_write_text(
            self.legacy_path,
            json.dumps(payload, indent=2) + "\n",
            private=self.private,
        )
        return normalized


class StringMapRepository:
    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        namespace: str,
        legacy_path: Path,
        private: bool = False,
    ) -> None:
        self.store = store
        self.namespace = namespace
        self.legacy_path = legacy_path
        self.private = private

    def _legacy_payload(self) -> dict[str, str]:
        if not self.legacy_path.exists():
            return {}
        try:
            payload = json.loads(self.legacy_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items()}

    def load(self) -> dict[str, str]:
        payload = self.store.get(self.namespace)
        if payload is None:
            payload = self._legacy_payload()
            self.store.put(self.namespace, payload)
        if not isinstance(payload, dict):
            return {}
        return {str(key): str(value) for key, value in payload.items()}

    def save(self, values: dict[str, str]) -> None:
        payload = {str(key): str(value) for key, value in sorted(values.items())}
        self.store.put(self.namespace, payload)
        atomic_write_text(
            self.legacy_path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            private=self.private,
        )


class ConfigurationStateRepositories:
    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        host: Any | None = None,
        gitlab_routing_file: Path,
        agent_channel_presence_file: Path,
        thread_index_file: Path,
        slack_thread_icons_file: Path,
    ) -> None:
        # `host` remains accepted for source compatibility while normalization
        # ownership lives in this storage module rather than the legacy runtime.
        _ = host
        self.gitlab_routing = NormalizedModelRepository(
            store,
            namespace="gitlab_routing",
            legacy_path=gitlab_routing_file,
            model=GitLabRoutingSettings,
            migrate=migrate_gitlab_routing_settings,
            normalize=normalize_gitlab_routing_settings,
            default=GitLabRoutingSettings,
            private=True,
        )
        self.agent_channel_presence = NormalizedModelRepository(
            store,
            namespace="agent_channel_presence",
            legacy_path=agent_channel_presence_file,
            model=AgentChannelPresenceSettings,
            migrate=migrate_agent_channel_presence_settings,
            normalize=normalize_agent_channel_presence_settings,
            default=lambda: legacy_agent_channel_presence_from_gitlab_file(gitlab_routing_file),
            private=True,
        )
        self.thread_index = ModelListRepository(
            store,
            namespace="thread_index",
            legacy_path=thread_index_file,
            model=IndexedThread,
            private=False,
        )
        self.slack_thread_icons = StringMapRepository(
            store,
            namespace="slack_thread_icons",
            legacy_path=slack_thread_icons_file,
            private=False,
        )


def install_configuration_state(app: Any, host: Any) -> ConfigurationStateRepositories:
    """Wire lower-churn configuration/index documents to SQLite."""

    from codex_web.paths import (
        AGENT_CHANNEL_PRESENCE_FILE,
        GITLAB_ROUTING_FILE,
        SLACK_THREAD_ICONS_FILE,
        THREAD_INDEX_FILE,
    )

    existing = getattr(app.state, "configuration_state_repositories", None)
    if existing is not None:
        repositories = existing
    else:
        repositories = ConfigurationStateRepositories(
            app.state.sqlite_state_store,
            gitlab_routing_file=GITLAB_ROUTING_FILE,
            agent_channel_presence_file=AGENT_CHANNEL_PRESENCE_FILE,
            thread_index_file=THREAD_INDEX_FILE,
            slack_thread_icons_file=SLACK_THREAD_ICONS_FILE,
        )
        app.state.configuration_state_repositories = repositories

    # Preserve historical compatibility names while the implementation owner
    # is this storage module.
    host._normalize_agent_channel_mapping = normalize_agent_channel_mapping
    host._normalize_agent_channel_presence_project_settings = normalize_agent_channel_presence_project_settings
    host._normalize_agent_channel_presence_settings = normalize_agent_channel_presence_settings
    host._migrate_agent_channel_presence_settings = migrate_agent_channel_presence_settings
    host._legacy_agent_channel_presence_from_gitlab_file = lambda: legacy_agent_channel_presence_from_gitlab_file(
        GITLAB_ROUTING_FILE
    )
    host._normalize_string_list = normalize_string_list
    host._normalize_gitlab_project_settings = normalize_gitlab_project_settings
    host._normalize_gitlab_routing_settings = normalize_gitlab_routing_settings
    host._migrate_gitlab_routing_settings = migrate_gitlab_routing_settings

    host._load_gitlab_routing_settings = repositories.gitlab_routing.load
    host._save_gitlab_routing_settings = repositories.gitlab_routing.save
    host._load_agent_channel_presence_settings = repositories.agent_channel_presence.load
    host._save_agent_channel_presence_settings = repositories.agent_channel_presence.save
    host._load_thread_index = repositories.thread_index.load
    host._save_thread_index = repositories.thread_index.save
    host._load_slack_thread_icons = repositories.slack_thread_icons.load
    host._save_slack_thread_icons = repositories.slack_thread_icons.save
    return repositories
