from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import HTTPException

from codex_web.models import BotConnection, BotConnectionCreate


class BotConnectionService:
    """Own bot connection lookup, mutation, deduplication, and public projection."""

    def __init__(self, host: Any) -> None:
        self.host = host

    def get(self, connection_id: str) -> BotConnection:
        for connection in self.host._load_bot_connections():
            if connection.id == connection_id:
                return connection
        raise HTTPException(status_code=404, detail="Bot connection not found")

    def for_conversation(self, provider: str, external_conversation_id: str) -> BotConnection | None:
        for connection in self.host._load_bot_connections():
            if (
                connection.provider == provider
                and connection.default_external_conversation_id == external_conversation_id
            ):
                return connection
        return None

    @staticmethod
    def mask_secret(value: str | None) -> str | None:
        if not value:
            return None
        if len(value) <= 8:
            return "********"
        return f"{value[:4]}...{value[-4:]}"

    def public(self, connection: BotConnection) -> dict[str, Any]:
        data = connection.model_dump()
        data["bot_token"] = self.mask_secret(connection.bot_token)
        data["slack_app_token"] = self.mask_secret(connection.slack_app_token)
        data["signing_secret"] = self.mask_secret(connection.signing_secret)
        data["webhook_secret"] = self.mask_secret(connection.webhook_secret)
        return data

    @staticmethod
    def identity(connection: BotConnection | BotConnectionCreate) -> tuple[Any, ...]:
        return (
            connection.provider.lower(),
            connection.project_id,
            (connection.default_external_conversation_id or "").strip(),
            (connection.name or "").strip().lower(),
            connection.bot_token or "",
            connection.slack_app_token or "",
            connection.signing_secret or "",
            connection.webhook_secret or "",
        )

    def matches_payload(self, connection: BotConnection, payload: BotConnectionCreate) -> bool:
        if payload.id and connection.id == payload.id:
            return True
        payload_identity = self.identity(
            BotConnection(
                id=connection.id,
                provider=payload.provider,
                name=payload.name,
                project_id=payload.project_id,
                bot_token=payload.bot_token or connection.bot_token,
                slack_app_token=payload.slack_app_token or connection.slack_app_token,
                signing_secret=payload.signing_secret or connection.signing_secret,
                webhook_secret=payload.webhook_secret or connection.webhook_secret,
                default_external_conversation_id=payload.default_external_conversation_id,
                default_external_name=payload.default_external_name,
                telegram_update_offset=connection.telegram_update_offset,
                created_at=connection.created_at,
                updated_at=connection.updated_at,
            )
        )
        return self.identity(connection) == payload_identity

    def upsert(self, payload: BotConnectionCreate) -> BotConnection:
        now = time.time()
        provider = payload.provider.lower()
        if provider not in {"slack", "telegram"}:
            raise HTTPException(status_code=400, detail="Provider must be slack or telegram")
        self.host._project(payload.project_id)
        connections = self.host._load_bot_connections()
        for index, connection in enumerate(connections):
            if self.matches_payload(connection, payload):
                current = connection.model_dump()
                updates = payload.model_dump(exclude={"id"})
                for secret in ("bot_token", "slack_app_token", "signing_secret", "webhook_secret"):
                    if updates.get(secret) in {None, "", "********"}:
                        updates[secret] = current.get(secret)
                current.update({key: value for key, value in updates.items() if value is not None})
                current["provider"] = provider
                current["updated_at"] = now
                updated = BotConnection.model_validate(current)
                connections[index] = updated
                self.host._save_bot_connections(connections)
                self.dedupe_integrations()
                return updated

        connection = BotConnection(
            id=uuid.uuid4().hex[:12],
            provider=provider,
            name=payload.name,
            project_id=payload.project_id,
            bot_token=payload.bot_token or None,
            slack_app_token=payload.slack_app_token or None,
            signing_secret=payload.signing_secret or None,
            webhook_secret=payload.webhook_secret or None,
            default_external_conversation_id=payload.default_external_conversation_id or None,
            default_external_name=payload.default_external_name or None,
            created_at=now,
            updated_at=now,
        )
        connections.append(connection)
        self.host._save_bot_connections(connections)
        self.dedupe_integrations()
        return connection

    def dedupe_integrations(self) -> None:
        connections = sorted(self.host._load_bot_connections(), key=lambda item: item.created_at)
        canonical_by_key: dict[tuple[Any, ...], BotConnection] = {}
        connection_rewrites: dict[str, str] = {}
        kept_connections: list[BotConnection] = []
        for connection in connections:
            key = self.identity(connection)
            canonical = canonical_by_key.get(key)
            if canonical:
                connection_rewrites[connection.id] = canonical.id
                continue
            canonical_by_key[key] = connection
            kept_connections.append(connection)

        bindings = sorted(self.host._load_bot_bindings(), key=lambda item: item.created_at)
        seen_binding_routes: set[tuple[str, str, str, str]] = set()
        kept_bindings = []
        for binding in bindings:
            if binding.connection_id in connection_rewrites:
                binding.connection_id = connection_rewrites[binding.connection_id]
            route_key = (
                binding.provider,
                binding.external_conversation_id,
                binding.thread_id,
                (self.host._binding_prefix(binding) or "").lower(),
            )
            if route_key in seen_binding_routes:
                continue
            seen_binding_routes.add(route_key)
            kept_bindings.append(binding)

        if len(kept_connections) != len(connections):
            self.host._save_bot_connections(kept_connections)
        if len(kept_bindings) != len(bindings) or connection_rewrites:
            self.host._save_bot_bindings(kept_bindings)

    def update(self, connection_id: str, **updates: Any) -> None:
        connections = self.host._load_bot_connections()
        changed = False
        for index, connection in enumerate(connections):
            if connection.id != connection_id:
                continue
            data = connection.model_dump()
            data.update(updates)
            data["updated_at"] = time.time()
            connections[index] = BotConnection.model_validate(data)
            changed = True
            break
        if changed:
            self.host._save_bot_connections(connections)


def install_bot_connection_service(app: Any, host: Any) -> BotConnectionService:
    existing = getattr(app.state, "bot_connection_service", None)
    if isinstance(existing, BotConnectionService) and existing.host is host:
        service = existing
    else:
        service = BotConnectionService(host)
        app.state.bot_connection_service = service

    host._bot_connection = service.get
    host._bot_connection_for_conversation = service.for_conversation
    host._mask_secret = service.mask_secret
    host._bot_connection_public = service.public
    host._connection_identity = service.identity
    host._connection_matches_payload = service.matches_payload
    host._upsert_bot_connection = service.upsert
    host._dedupe_bot_integrations = service.dedupe_integrations
    host._update_bot_connection = service.update
    return service
