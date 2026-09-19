from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import HTTPException

from codex_web.identity import AuthenticationActor, TenantScope
from codex_web.models import BotConnection, BotConnectionCreate
from codex_web.secrets import SecretCreate, SecretRotate
from codex_web.services.identity import IdentityService
from codex_web.services.secrets import SecretBroker


CREDENTIAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("bot_token", "bot_token_secret_id"),
    ("slack_app_token", "slack_app_token_secret_id"),
    ("signing_secret", "signing_secret_secret_id"),
    ("webhook_secret", "webhook_secret_secret_id"),
)
RUNTIME_IDENTITY_ID = "system-bot-runtime"


class BotConnectionService:
    """Own bot connection lookup, mutation, deduplication, and public projection."""

    def __init__(
        self,
        host: Any,
        *,
        secret_broker: SecretBroker | None = None,
        identity_service: IdentityService | None = None,
    ) -> None:
        self.host = host
        self.secret_broker = secret_broker
        self.identity_service = identity_service

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
        for raw_field, ref_field in CREDENTIAL_FIELDS:
            raw = getattr(connection, raw_field)
            reference = getattr(connection, ref_field)
            data[raw_field] = self.mask_secret(raw) if raw else ("stored" if reference else None)
        return data

    @staticmethod
    def identity(connection: BotConnection | BotConnectionCreate) -> tuple[Any, ...]:
        credential_identity = tuple(
            getattr(connection, ref_field, None) or getattr(connection, raw_field, None) or ""
            for raw_field, ref_field in CREDENTIAL_FIELDS
        )
        return (
            connection.provider.lower(),
            connection.project_id,
            (connection.default_external_conversation_id or "").strip(),
            (connection.name or "").strip().lower(),
            *credential_identity,
        )

    def _project_scope(self, project_id: str) -> TenantScope:
        project = self.host._project(project_id)
        return TenantScope(
            organization_id=getattr(project, "organization_id", "local"),
            workspace_id=getattr(project, "workspace_id", "default"),
        )

    def runtime_actor(self, project_id: str) -> AuthenticationActor:
        if self.identity_service is None:
            raise RuntimeError("identity service unavailable for credential resolution")
        return self.identity_service.bootstrap_service_actor(
            identity_id=RUNTIME_IDENTITY_ID,
            name="Bot Provider Runtime",
            scope=self._project_scope(project_id),
            service_scopes=("secret:use",),
        )

    def _store_credential(
        self,
        *,
        connection_id: str,
        project_id: str,
        field: str,
        value: str,
        existing_secret_id: str | None,
        actor: AuthenticationActor,
    ) -> str:
        if self.secret_broker is None:
            raise RuntimeError("secret broker unavailable")
        runtime_actor = self.runtime_actor(project_id)
        if existing_secret_id:
            self.secret_broker.rotate(
                existing_secret_id,
                SecretRotate(value=value),
                actor=actor,
            )
            return existing_secret_id
        reference = self.secret_broker.create(
            SecretCreate(
                name=f"bot:{connection_id}:{field}",
                value=value,
                provider="bot",
                purpose=field,
                allowed_identity_ids=[runtime_actor.identity_id],
            ),
            actor=actor,
            scope=self._project_scope(project_id),
        )
        return reference.id

    def _credential_values(
        self,
        *,
        connection_id: str,
        project_id: str,
        payload: BotConnectionCreate,
        existing: BotConnection | None,
        actor: AuthenticationActor | None,
    ) -> dict[str, str | None]:
        values: dict[str, str | None] = {}
        for raw_field, ref_field in CREDENTIAL_FIELDS:
            raw = getattr(payload, raw_field)
            explicit_ref = getattr(payload, ref_field)
            current_raw = getattr(existing, raw_field) if existing else None
            current_ref = getattr(existing, ref_field) if existing else None

            if explicit_ref:
                if actor is None or self.secret_broker is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Explicit secret references require an authenticated secret broker",
                    )
                self.secret_broker.metadata(
                    explicit_ref,
                    actor=actor,
                    require_use=True,
                )
                values[raw_field] = None
                values[ref_field] = explicit_ref
                continue
            if raw in {None, "", "********", "stored"}:
                values[raw_field] = current_raw
                values[ref_field] = current_ref
                continue
            if actor is not None and self.secret_broker is not None:
                values[ref_field] = self._store_credential(
                    connection_id=connection_id,
                    project_id=project_id,
                    field=raw_field,
                    value=raw,
                    existing_secret_id=current_ref,
                    actor=actor,
                )
                values[raw_field] = None
                continue
            # Compatibility path for direct/internal callers during migration.
            values[raw_field] = raw
            values[ref_field] = current_ref
        return values

    def matches_payload(self, connection: BotConnection, payload: BotConnectionCreate) -> bool:
        if payload.id and connection.id == payload.id:
            return True
        return (
            connection.provider == payload.provider
            and connection.project_id == payload.project_id
            and (connection.default_external_conversation_id or "")
            == (payload.default_external_conversation_id or "")
            and connection.name.strip().casefold() == payload.name.strip().casefold()
        )

    def upsert(
        self,
        payload: BotConnectionCreate,
        *,
        actor: AuthenticationActor | None = None,
    ) -> BotConnection:
        now = time.time()
        provider = payload.provider.lower()
        if provider not in {"slack", "telegram", "teams"}:
            raise HTTPException(
                status_code=400,
                detail="Provider must be slack, telegram, or teams",
            )
        project = self.host._project(payload.project_id)
        if actor is not None:
            scope = self._project_scope(payload.project_id)
            if actor.tenant != scope:
                raise HTTPException(status_code=404, detail="Project not found")
        connections = self.host._load_bot_connections()
        for index, connection in enumerate(connections):
            if self.matches_payload(connection, payload):
                current = connection.model_dump()
                updates = payload.model_dump(exclude={"id"})
                credentials = self._credential_values(
                    connection_id=connection.id,
                    project_id=payload.project_id,
                    payload=payload,
                    existing=connection,
                    actor=actor,
                )
                for raw_field, ref_field in CREDENTIAL_FIELDS:
                    updates.pop(raw_field, None)
                    updates.pop(ref_field, None)
                current.update({key: value for key, value in updates.items() if value is not None})
                current.update(credentials)
                current["provider"] = provider
                current["updated_at"] = now
                updated = BotConnection.model_validate(current)
                connections[index] = updated
                self.host._save_bot_connections(connections)
                self.dedupe_integrations()
                return updated

        connection_id = payload.id or uuid.uuid4().hex[:12]
        credentials = self._credential_values(
            connection_id=connection_id,
            project_id=payload.project_id,
            payload=payload,
            existing=None,
            actor=actor,
        )
        connection = BotConnection(
            id=connection_id,
            provider=provider,
            name=payload.name,
            project_id=payload.project_id,
            **credentials,
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


def install_bot_connection_service(
    app: Any,
    host: Any,
    *,
    secret_broker: SecretBroker | None = None,
    identity_service: IdentityService | None = None,
) -> BotConnectionService:
    existing = getattr(app.state, "bot_connection_service", None)
    if isinstance(existing, BotConnectionService) and existing.host is host:
        service = existing
        if secret_broker is not None:
            service.secret_broker = secret_broker
        if identity_service is not None:
            service.identity_service = identity_service
    else:
        service = BotConnectionService(
            host,
            secret_broker=secret_broker,
            identity_service=identity_service,
        )
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
    host._bot_runtime_actor = service.runtime_actor
    return service
