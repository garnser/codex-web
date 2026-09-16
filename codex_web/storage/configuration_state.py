from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel

from codex_web.models import (
    AgentChannelPresenceSettings,
    GitLabRoutingSettings,
    IndexedThread,
)
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.operational_state import ModelListRepository
from codex_web.storage.sqlite_state import SQLiteStateStore


T = TypeVar("T", bound=BaseModel)


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
        host: Any,
        gitlab_routing_file: Path,
        agent_channel_presence_file: Path,
        thread_index_file: Path,
        slack_thread_icons_file: Path,
    ) -> None:
        self.gitlab_routing = NormalizedModelRepository(
            store,
            namespace="gitlab_routing",
            legacy_path=gitlab_routing_file,
            model=GitLabRoutingSettings,
            migrate=host._migrate_gitlab_routing_settings,
            normalize=host._normalize_gitlab_routing_settings,
            default=GitLabRoutingSettings,
            private=True,
        )
        self.agent_channel_presence = NormalizedModelRepository(
            store,
            namespace="agent_channel_presence",
            legacy_path=agent_channel_presence_file,
            model=AgentChannelPresenceSettings,
            migrate=host._migrate_agent_channel_presence_settings,
            normalize=host._normalize_agent_channel_presence_settings,
            default=host._legacy_agent_channel_presence_from_gitlab_file,
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
            host=host,
            gitlab_routing_file=GITLAB_ROUTING_FILE,
            agent_channel_presence_file=AGENT_CHANNEL_PRESENCE_FILE,
            thread_index_file=THREAD_INDEX_FILE,
            slack_thread_icons_file=SLACK_THREAD_ICONS_FILE,
        )
        app.state.configuration_state_repositories = repositories

    host._load_gitlab_routing_settings = repositories.gitlab_routing.load
    host._save_gitlab_routing_settings = repositories.gitlab_routing.save
    host._load_agent_channel_presence_settings = repositories.agent_channel_presence.load
    host._save_agent_channel_presence_settings = repositories.agent_channel_presence.save
    host._load_thread_index = repositories.thread_index.load
    host._save_thread_index = repositories.thread_index.save
    host._load_slack_thread_icons = repositories.slack_thread_icons.load
    host._save_slack_thread_icons = repositories.slack_thread_icons.save
    return repositories
