from __future__ import annotations

import copy
import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel

from codex_web.models import ApprovalSlackMessage, BotThreadDetail
from codex_web.storage.json_files import atomic_write_text
from codex_web.storage.state_store import StateStore


T = TypeVar("T", bound=BaseModel)


class NestedModelListMapRepository(Generic[T]):
    """StateStore-primary mapping of IDs to model lists with rollback-safe JSON mirroring.

    Each top-level key is merged independently against the latest committed
    document. This prevents concurrent updates for different thread/request IDs
    from replacing one another between load() and save().
    """

    def __init__(
        self,
        store: StateStore,
        *,
        namespace: str,
        legacy_path: Path,
        model: type[T],
        private: bool = True,
    ) -> None:
        self.store = store
        self.namespace = namespace
        self.legacy_path = legacy_path
        self.model = model
        self.private = private
        self._snapshot: ContextVar[dict[str, Any] | None] = ContextVar(
            f"codex_web_{namespace}_snapshot",
            default=None,
        )

    def _legacy_payload(self) -> dict[str, Any]:
        if not self.legacy_path.exists():
            return {}
        try:
            payload = json.loads(self.legacy_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _raw(self) -> dict[str, Any]:
        payload = self.store.get(self.namespace)
        if payload is None:
            payload = self._legacy_payload()
            self.store.put(self.namespace, payload)
        return payload if isinstance(payload, dict) else {}

    def load(self) -> dict[str, list[T]]:
        raw = self._raw()
        self._snapshot.set(copy.deepcopy(raw))
        result: dict[str, list[T]] = {}
        for key, values in raw.items():
            if not isinstance(key, str) or not isinstance(values, list):
                continue
            result[key] = [self.model.model_validate(item) for item in values]
        return result

    def save(self, values: dict[str, list[T]]) -> None:
        payload = {
            str(key): [item.model_dump() for item in items]
            for key, items in sorted(values.items())
        }
        base = self._snapshot.get()
        if base is None:
            self.store.put(self.namespace, payload)
            merged = payload
        else:
            changed = {
                key: value
                for key, value in payload.items()
                if key not in base or base.get(key) != value
            }
            deleted = set(base) - set(payload)

            def merge(current: Any) -> dict[str, Any]:
                latest = dict(current) if isinstance(current, dict) else {}
                for key in deleted:
                    latest.pop(key, None)
                latest.update(changed)
                return latest

            merged = self.store.update(self.namespace, merge, default={})

        self._snapshot.set(copy.deepcopy(merged))
        atomic_write_text(
            self.legacy_path,
            json.dumps(merged, indent=2, sort_keys=True) + "\n",
            private=self.private,
        )


class JsonMapRepository:
    """Transactional top-level mapping with normalization and JSON mirroring."""

    def __init__(
        self,
        store: StateStore,
        *,
        namespace: str,
        legacy_path: Path,
        default: Callable[[], dict[str, Any]] = dict,
        normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        private: bool = True,
    ) -> None:
        self.store = store
        self.namespace = namespace
        self.legacy_path = legacy_path
        self.default = default
        self.normalize = normalize or (lambda value: value)
        self.private = private
        self._snapshot: ContextVar[dict[str, Any] | None] = ContextVar(
            f"codex_web_{namespace}_snapshot",
            default=None,
        )

    def _legacy_payload(self) -> dict[str, Any]:
        if not self.legacy_path.exists():
            return self.normalize(self.default())
        try:
            payload = json.loads(self.legacy_path.read_text())
        except (OSError, json.JSONDecodeError):
            return self.normalize(self.default())
        if not isinstance(payload, dict):
            return self.normalize(self.default())
        return self.normalize(payload)

    def _raw(self) -> dict[str, Any]:
        payload = self.store.get(self.namespace)
        if payload is None:
            payload = self._legacy_payload()
            self.store.put(self.namespace, payload)
        if not isinstance(payload, dict):
            return self.normalize(self.default())
        return self.normalize(payload)

    def load(self) -> dict[str, Any]:
        raw = self._raw()
        self._snapshot.set(copy.deepcopy(raw))
        return copy.deepcopy(raw)

    def save(self, values: dict[str, Any]) -> None:
        payload = self.normalize(copy.deepcopy(values))
        base = self._snapshot.get()
        if base is None:
            self.store.put(self.namespace, payload)
            merged = payload
        else:
            changed = {
                key: value
                for key, value in payload.items()
                if key not in base or base.get(key) != value
            }
            deleted = set(base) - set(payload)

            def merge(current: Any) -> dict[str, Any]:
                latest = dict(current) if isinstance(current, dict) else {}
                for key in deleted:
                    latest.pop(key, None)
                latest.update(changed)
                return self.normalize(latest)

            merged = self.store.update(self.namespace, merge, default=self.default())

        self._snapshot.set(copy.deepcopy(merged))
        atomic_write_text(
            self.legacy_path,
            json.dumps(merged, indent=2, sort_keys=True) + "\n",
            private=self.private,
        )


def _normalize_servicedesk_state(value: dict[str, Any]) -> dict[str, Any]:
    tickets = value.get("tickets")
    return {
        **value,
        "tickets": copy.deepcopy(tickets) if isinstance(tickets, dict) else {},
        "last_sweep_at": value.get("last_sweep_at"),
    }


class ServiceDeskStateRepository:
    """Merge ServiceDesk tickets independently instead of replacing the ticket map."""

    namespace = "support_servicedesk_state"

    def __init__(self, store: StateStore, legacy_path: Path) -> None:
        self.store = store
        self.legacy_path = legacy_path
        self._snapshot: ContextVar[dict[str, Any] | None] = ContextVar(
            "codex_web_support_servicedesk_state_snapshot",
            default=None,
        )

    @staticmethod
    def _default() -> dict[str, Any]:
        return {"tickets": {}, "last_sweep_at": None}

    def _legacy_payload(self) -> dict[str, Any]:
        if not self.legacy_path.exists():
            return self._default()
        try:
            payload = json.loads(self.legacy_path.read_text())
        except (OSError, json.JSONDecodeError):
            return self._default()
        return _normalize_servicedesk_state(payload) if isinstance(payload, dict) else self._default()

    def load(self) -> dict[str, Any]:
        payload = self.store.get(self.namespace)
        if payload is None:
            payload = self._legacy_payload()
            self.store.put(self.namespace, payload)
        normalized = _normalize_servicedesk_state(payload) if isinstance(payload, dict) else self._default()
        self._snapshot.set(copy.deepcopy(normalized))
        return copy.deepcopy(normalized)

    def save(self, values: dict[str, Any]) -> None:
        payload = _normalize_servicedesk_state(copy.deepcopy(values))
        base = self._snapshot.get()
        if base is None:
            self.store.put(self.namespace, payload)
            merged = payload
        else:
            base_normalized = _normalize_servicedesk_state(base)
            base_tickets = base_normalized["tickets"]
            payload_tickets = payload["tickets"]
            changed_tickets = {
                key: value
                for key, value in payload_tickets.items()
                if key not in base_tickets or base_tickets.get(key) != value
            }
            deleted_tickets = set(base_tickets) - set(payload_tickets)
            changed_metadata = {
                key: value
                for key, value in payload.items()
                if key != "tickets" and base_normalized.get(key) != value
            }
            deleted_metadata = (set(base_normalized) - {"tickets"}) - set(payload)

            def merge(current: Any) -> dict[str, Any]:
                latest = _normalize_servicedesk_state(current) if isinstance(current, dict) else self._default()
                latest_tickets = dict(latest.get("tickets") or {})
                for key in deleted_tickets:
                    latest_tickets.pop(key, None)
                latest_tickets.update(changed_tickets)
                latest["tickets"] = latest_tickets
                for key in deleted_metadata:
                    latest.pop(key, None)
                latest.update(changed_metadata)
                return _normalize_servicedesk_state(latest)

            merged = self.store.update(self.namespace, merge, default=self._default())

        self._snapshot.set(copy.deepcopy(merged))
        atomic_write_text(
            self.legacy_path,
            json.dumps(merged, indent=2, sort_keys=True) + "\n",
            private=True,
        )


def _normalize_semantic_events(value: dict[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, raw in value.items():
        try:
            result[str(key)] = float(raw)
        except (TypeError, ValueError):
            continue
    return result


class AuxiliaryStateRepositories:
    def __init__(
        self,
        store: StateStore,
        *,
        bot_details_file: Path,
        approval_messages_file: Path,
        support_servicedesk_state_file: Path,
        gitlab_semantic_events_file: Path,
    ) -> None:
        self.bot_details = NestedModelListMapRepository(
            store,
            namespace="bot_details",
            legacy_path=bot_details_file,
            model=BotThreadDetail,
            private=True,
        )
        self.approval_messages = NestedModelListMapRepository(
            store,
            namespace="approval_messages",
            legacy_path=approval_messages_file,
            model=ApprovalSlackMessage,
            private=True,
        )
        self.support_servicedesk = ServiceDeskStateRepository(
            store,
            support_servicedesk_state_file,
        )
        self.gitlab_semantic_events = JsonMapRepository(
            store,
            namespace="gitlab_semantic_events",
            legacy_path=gitlab_semantic_events_file,
            default=dict,
            normalize=_normalize_semantic_events,
            private=True,
        )


def install_auxiliary_state(app: Any, host: Any) -> AuxiliaryStateRepositories:
    """Wire the remaining mutable JSON documents to the configured canonical StateStore."""

    from codex_web.paths import (
        APPROVAL_MESSAGES_FILE,
        BOT_DETAILS_FILE,
        GITLAB_SEMANTIC_EVENTS_FILE,
        SUPPORT_SERVICEDESK_STATE_FILE,
    )

    existing = getattr(app.state, "auxiliary_state_repositories", None)
    if existing is not None:
        repositories = existing
    else:
        repositories = AuxiliaryStateRepositories(
            getattr(app.state, "state_store", app.state.sqlite_state_store),
            bot_details_file=BOT_DETAILS_FILE,
            approval_messages_file=APPROVAL_MESSAGES_FILE,
            support_servicedesk_state_file=SUPPORT_SERVICEDESK_STATE_FILE,
            gitlab_semantic_events_file=GITLAB_SEMANTIC_EVENTS_FILE,
        )
        app.state.auxiliary_state_repositories = repositories

    host._load_bot_details = repositories.bot_details.load
    host._save_bot_details = repositories.bot_details.save
    host._load_approval_messages = repositories.approval_messages.load
    host._save_approval_messages = repositories.approval_messages.save
    host._load_support_servicedesk_state = repositories.support_servicedesk.load
    host._save_support_servicedesk_state = repositories.support_servicedesk.save
    host._load_gitlab_semantic_events = repositories.gitlab_semantic_events.load
    host._save_gitlab_semantic_events = repositories.gitlab_semantic_events.save
    return repositories
