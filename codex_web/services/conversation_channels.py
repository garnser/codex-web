from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from codex_web.canonical_events import CanonicalEventType
from codex_web.compatibility import ContractCompatibilityError
from codex_web.conversation_channels import (
    CONVERSATION_CHANNEL_CONTRACT,
    ConversationChannel,
    ConversationChannelCapabilities,
    ConversationChannelEvent,
    ConversationChannelState,
    ConversationEventKind,
    ConversationMessageState,
    ConversationProjectionOutcome,
    ConversationProjectionReceipt,
)
from codex_web.identity import AuthenticationActor
from codex_web.crypto import EncryptionContext, KeyPurpose, KeyStatus, KeyVersionStatus, CryptoKeyState
from codex_web.data_governance import DataGovernanceState
from codex_web.data_governance import GovernedDataCreate, DataCategory, DataClassification, GovernedDataLifecycle
from codex_web.models import BotInboundMessage
from codex_web.services.canonical_events import CanonicalEventIngestionService
from codex_web.storage.conversation_channels import ConversationChannelStore


def capture_repository_selection(settings, project, execution_profile, resources, actor):
    """Freeze existing canonical repository selection; never resolve from payload/path."""
    from codex_web.resources import ResourceType, ResourceLifecycle
    from codex_web.services.identity import TenantIsolationError
    if (project.organization_id, project.workspace_id) != (actor.organization_id, actor.workspace_id):
        raise TenantIsolationError("cross-tenant repository capture denied")
    if execution_profile.repository_access == "none":
        return settings.model_copy(update={"repository_resource_id": None,
            "writable_repository_resource_ids": (), "read_only_repository_resource_ids": ()})
    writes = settings.writable_repository_resource_ids
    if not writes and project.repository_selection_policy == "coordinated":
        writes = tuple(row.id for row in resources.project_resources(project, actor=actor)
            if row.resource_type == ResourceType.REPOSITORY and row.lifecycle == ResourceLifecycle.ACTIVE)
    if writes:
        if settings.repository_resource_id and settings.repository_resource_id not in writes:
            raise ValueError("captured repository selectors conflict")
        for repository in writes:
            resources.resolve_repository_target(project, actor=actor, explicit_repository_id=repository,
                read_only_repository_ids=settings.read_only_repository_resource_ids)
        selected = settings.repository_resource_id or (writes[0] if len(writes) == 1 else None)
    else:
        target = resources.resolve_repository_target(project, actor=actor,
            explicit_repository_id=settings.repository_resource_id,
            read_only_repository_ids=settings.read_only_repository_resource_ids)
        selected = target.mutable_repository_id
        writes = (selected,) if selected else ()
    return settings.model_copy(update={"repository_resource_id": selected,
        "writable_repository_resource_ids": tuple(writes)})


ConversationRouteHandler = Callable[[BotInboundMessage], Awaitable[dict[str, Any]]]
ConversationChannelFactory = Callable[[], ConversationChannel]
ConversationChannelProviderFactory = Callable[[str], ConversationChannel]
ConversationChannelAvailability = Callable[[], bool]


class ConversationChannelError(RuntimeError):
    pass


class ConversationChannelNotFoundError(ConversationChannelError):
    pass


class ConversationChannelConflictError(ConversationChannelError):
    pass


class ConversationChannelUnavailableError(ConversationChannelError):
    pass


class ConversationChannelReconciliationError(ConversationChannelError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class ConversationChannelValidationError(ConversationChannelError):
    pass


class ConversationChannelRegistry:
    """Code-owned adapter registry with an extension-lifecycle availability gate."""

    def __init__(self) -> None:
        self._entries: dict[
            tuple[str, str],
            tuple[
                ConversationChannelFactory,
                ConversationChannelAvailability | None,
                str | None,
            ],
        ] = {}
        self._provider_factories: dict[str, ConversationChannelProviderFactory] = {}

    @staticmethod
    def _key(provider_type: str, provider_instance: str) -> tuple[str, str]:
        provider = str(provider_type or "").strip().casefold()
        instance = str(provider_instance or "").strip().casefold()
        if not provider or not instance:
            raise ValueError("conversation channel provider and instance are required")
        return provider, instance

    def register(
        self,
        provider_type: str,
        provider_instance: str,
        factory: ConversationChannelFactory,
        *,
        available: ConversationChannelAvailability | None = None,
        installation_id: str | None = None,
    ) -> None:
        self._entries[self._key(provider_type, provider_instance)] = (
            factory,
            available,
            installation_id,
        )

    def register_provider(
        self,
        provider_type: str,
        factory: ConversationChannelProviderFactory,
    ) -> None:
        provider = str(provider_type or "").strip().casefold()
        if not provider:
            raise ValueError("conversation channel provider type is required")
        self._provider_factories[provider] = factory

    def unregister(self, provider_type: str, provider_instance: str) -> None:
        self._entries.pop(self._key(provider_type, provider_instance), None)

    def resolve(
        self,
        provider_type: str,
        provider_instance: str,
    ) -> ConversationChannel:
        key = self._key(provider_type, provider_instance)
        entry = self._entries.get(key)
        if entry is None:
            provider_factory = self._provider_factories.get(key[0])
            if provider_factory is None:
                raise ConversationChannelNotFoundError(
                    f"conversation channel adapter is not registered: {provider_type}/{provider_instance}"
                )
            adapter = provider_factory(provider_instance)
        else:
            factory, available, _installation_id = entry
            if available is not None and not available():
                raise ConversationChannelUnavailableError(
                    f"conversation channel adapter is not enabled: {provider_type}/{provider_instance}"
                )
            adapter = factory()
        try:
            CONVERSATION_CHANNEL_CONTRACT.require(adapter.contract_version)
        except (ContractCompatibilityError, ValueError) as exc:
            raise ConversationChannelValidationError(
                f"incompatible ConversationChannel contract version: {adapter.contract_version!r}"
            ) from exc
        if adapter.provider_type.casefold() != key[0]:
            raise ConversationChannelValidationError(
                "ConversationChannel provider_type does not match registry key"
            )
        if adapter.provider_instance.casefold() != key[1]:
            raise ConversationChannelValidationError(
                "ConversationChannel provider_instance does not match registry key"
            )
        return adapter

    def describe(self) -> tuple[dict[str, Any], ...]:
        rows: list[dict[str, Any]] = []
        for (provider, instance), (factory, available, installation_id) in sorted(
            self._entries.items()
        ):
            enabled = available() if available is not None else True
            capabilities: tuple[str, ...] = ()
            contract_version: str | None = None
            error: str | None = None
            if enabled:
                try:
                    adapter = factory()
                    capabilities = tuple(
                        sorted(item.value for item in adapter.capabilities.supported)
                    )
                    contract_version = adapter.contract_version
                except Exception as exc:
                    enabled = False
                    error = f"{type(exc).__name__}: {exc}"
            rows.append(
                {
                    "provider_type": provider,
                    "provider_instance": instance,
                    "enabled": enabled,
                    "installation_id": installation_id,
                    "contract_version": contract_version,
                    "capabilities": capabilities,
                    "error": error,
                }
            )
        return tuple(rows)

    def provider_contracts(self) -> tuple[dict[str, Any], ...]:
        rows: list[dict[str, Any]] = []
        providers = set(self._provider_factories)
        providers.update(provider for provider, _instance in self._entries)
        described = self.describe()
        for provider in sorted(providers):
            provider_factory = self._provider_factories.get(provider)
            if provider_factory is not None:
                try:
                    adapter = provider_factory("capability-probe")
                    rows.append(
                        {
                            "provider_type": provider,
                            "contract_version": adapter.contract_version,
                            "capabilities": tuple(
                                sorted(
                                    item.value
                                    for item in adapter.capabilities.supported
                                )
                            ),
                            "available": True,
                            "error": None,
                        }
                    )
                except Exception as exc:
                    rows.append(
                        {
                            "provider_type": provider,
                            "contract_version": None,
                            "capabilities": (),
                            "available": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                continue

            entries = [
                row
                for row in described
                if row["provider_type"] == provider
            ]
            capabilities = sorted(
                {
                    capability
                    for row in entries
                    if row.get("enabled")
                    for capability in row.get("capabilities", ())
                }
            )
            rows.append(
                {
                    "provider_type": provider,
                    "contract_version": next(
                        (
                            row.get("contract_version")
                            for row in entries
                            if row.get("contract_version")
                        ),
                        None,
                    ),
                    "capabilities": tuple(capabilities),
                    "available": any(
                        bool(row.get("enabled")) for row in entries
                    ),
                    "error": next(
                        (
                            row.get("error")
                            for row in entries
                            if row.get("error")
                        ),
                        None,
                    ),
                }
            )
        return tuple(rows)


class ConversationChannelService:
    """Canonical, deterministic inbound channel normalization and routing boundary."""

    MAX_RECEIPTS = 20_000
    MAX_MESSAGES = 20_000
    MAX_INPUT_BYTES = 2 * 1024 * 1024
    MAX_PROTECTED_BYTES = 64 * 1024 * 1024

    def __init__(
        self,
        store: ConversationChannelStore,
        registry: ConversationChannelRegistry,
        canonical_events: CanonicalEventIngestionService,
        route_handler: ConversationRouteHandler,
        *,
        clock=time.time,
        crypto=None,
        governance=None,
        authority_snapshot=None,
        source_authority_snapshot=None,
        routing_gate=None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.canonical_events = canonical_events
        self.route_handler = route_handler
        self.clock = clock
        self.crypto = crypto
        self.governance = governance
        self.authority_snapshot = authority_snapshot
        self.source_authority_snapshot = (
            source_authority_snapshot if source_authority_snapshot is not None else authority_snapshot
        )
        self.routing_gate = routing_gate

    def _context(self, receipt):
        return EncryptionContext(organization_id=receipt.organization_id,
            workspace_id=receipt.workspace_id, project_id=receipt.project_id,
            object_type="conversation_ingress", object_id=receipt.canonical_event_id)

    def _capture_current_authority(self, event, actor):
        if self.authority_snapshot is None or event.message.conversation.provider_type != "slack":
            return None
        return self.authority_snapshot(event, actor)

    def _authority(self, event, actor):
        if self.authority_snapshot is None or event.message.conversation.provider_type != "slack":
            return None
        snapshot = self.authority_snapshot(event, actor)
        if self._source_authority_digest(self.source_authority_snapshot(event, actor)) != self._source_authority_digest(snapshot):
            raise ConversationChannelConflictError("authenticated source changed during authority verification")
        return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    async def _capture_authority_snapshot(self, event, actor):
        if self.authority_snapshot is None or event.message.conversation.provider_type != "slack":
            return None
        # Pin authenticated source metadata before yielding. The production
        # callback omits the expensive routing/Profile inventory; a credential
        # rotation during that awaited inventory cannot authorize an old socket
        # as its new source. Direct callers retain the conservative full fallback.
        expected_source = self._source_authority_digest(self.source_authority_snapshot(event, actor))
        snapshot = await asyncio.to_thread(self.authority_snapshot, event, actor)
        if (self._source_authority_digest(snapshot) != expected_source
            or self._source_authority_digest(self.source_authority_snapshot(event, actor)) != expected_source):
            raise ConversationChannelConflictError("authenticated source changed during authority capture; no acknowledgement")
        return snapshot

    def retained_input(self, receipt, actor):
        if not self._same_scope(receipt, actor) or receipt.ingest_actor_id != actor.identity_id:
            raise ConversationChannelConflictError("retained conversation actor/scope changed")
        if receipt.protected_input is None:
            return {"event": receipt.retained_event.model_dump(mode="json")} if receipt.retained_event else None
        record = self.governance.get_record(receipt.governance_record_id, actor=actor)
        if record.lifecycle != GovernedDataLifecycle.ACTIVE or (
            record.retention_expires_at is not None and record.retention_expires_at <= self.clock()
        ):
            raise ConversationChannelUnavailableError("retained input is unavailable under data policy")
        return json.loads(self.crypto.decrypt(receipt.protected_input, self._context(receipt), actor=actor))

    def governance_action(self, record, action):
        def apply(state):
            receipt = state.receipts.get(record.object_id)
            if receipt is None or (receipt.organization_id, receipt.workspace_id) != (record.organization_id, record.workspace_id):
                raise ConversationChannelNotFoundError("retained input not found")
            state.receipts[record.object_id] = receipt.model_copy(update={
                "retained_event": None, "protected_input": None,
                "outcome": ConversationProjectionOutcome.FAILED,
                "reason": "input removed by canonical data-governance action",
            })
            return state
        self.store.update(apply)
        return record.object_id

    @staticmethod
    def _same_scope(item: Any, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _event_payload(event: ConversationChannelEvent) -> dict[str, Any]:
        return event.model_dump(mode="json")

    @staticmethod
    def _retention_identity(event: ConversationChannelEvent, actor: AuthenticationActor) -> tuple[str, str]:
        conversation = event.message.conversation
        source = f"conversation-channel:{conversation.provider_type}:{conversation.provider_instance}"
        # Canonical connection/Project values are supplied by authenticated
        # ingress, never by Slack's team/project claims. JSON avoids delimiter
        # collisions. Thread/message/sender changes under this delivery identity
        # are conflicts, rather than opportunities to create a second receipt.
        key = json.dumps([
            actor.organization_id, actor.workspace_id, source,
            event.connection_id, event.project_id, conversation.conversation_id,
            event.delivery_id,
        ], separators=(",", ":"), ensure_ascii=False)
        return source, key

    @classmethod
    def _input_digest(cls, event: ConversationChannelEvent, original_payload: dict[str, Any] | None) -> str:
        content = {"event": cls._event_payload(event)}
        if original_payload is not None:
            # Socket Mode delivery diagnostics may change on a legitimate
            # resend. Only these TOP-LEVEL transport fields are excluded from
            # compatibility; similarly named fields inside a message are data.
            transport_fields = {"envelope_id", "retry_attempt", "retry_reason", "accepts_response_payload"}
            content["original_payload"] = {key: value for key, value in original_payload.items()
                if key not in transport_fields}
        return hashlib.sha256(json.dumps(content, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _source_authority_digest(snapshot):
        if snapshot is None:
            return None
        # Source authentication is independent of routing state that this very
        # delivery can legitimately create/update. Routing pins stay immutable
        # and are revalidated separately before any retained routing resumes.
        source = {key:value for key,value in snapshot.items() if key not in {"routing","route_selection","new_binding","new_binding_error"}}
        return hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _validate_retained(self, receipt, event, actor, *, input_digest=None,
            authority_digest=None, source_authority_digest=None, check_authority=False, persisted_event=None):
        incompatible = (receipt is None or not self._same_scope(receipt, actor)
            or receipt.ingest_actor_id != actor.identity_id
            or receipt.connection_id != event.connection_id
            or receipt.project_id != event.project_id
            or receipt.message_key != event.message.stable_key()
            or (input_digest is not None and receipt.input_digest != input_digest))
        if check_authority and not incompatible:
            proof = persisted_event.payload if persisted_event is not None else None
            incompatible = (proof is None or any(proof.get(field) != getattr(receipt,field)
                for field in ("connection_id","project_id","message_key","ingest_actor_id",
                    "input_digest","authority_digest","source_authority_digest")))
            if receipt.source_authority_digest is None:
                # Older protected readers did not distinguish authentication
                # from mutable routing state. Do not guess their source pins.
                incompatible = incompatible or receipt.authority_digest != authority_digest
            else:
                incompatible = incompatible or receipt.source_authority_digest != source_authority_digest
        if incompatible:
            raise ConversationChannelConflictError("retained original input/provenance is incompatible; no acknowledgement")

    @staticmethod
    def _position_key(organization_id, workspace_id, connection_id, project_id, message):
        if connection_id is None and project_id is None:
            return "::".join((organization_id, workspace_id, message.stable_key()))
        return json.dumps([organization_id, workspace_id, connection_id, project_id,
            message.stable_key()], separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def _message_state_key(cls, actor, event):
        return cls._position_key(actor.organization_id, actor.workspace_id,
            event.connection_id, event.project_id, event.message)

    @staticmethod
    def _message_text(event: ConversationChannelEvent) -> str:
        parts: list[str] = []
        if (event.text or "").strip():
            parts.append((event.text or "").strip())
        if event.attachments:
            attachments = ", ".join(
                item.name or item.external_id
                for item in event.attachments
            )
            parts.append(f"Attachments: {attachments}")
        if event.references and parts:
            refs = ", ".join(
                item.label or item.value
                for item in event.references
            )
            parts.append(f"References: {refs}")
        return "\n\n".join(parts).strip()

    @staticmethod
    def _bounded_routing_prefixes(value: object) -> list[str]:
        if not isinstance(value, list) or len(value) > 256:
            return []
        if any(not isinstance(item, str) or not item or len(item) > 512 for item in value):
            return []
        try:
            encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return []
        return value if len(encoded.encode("utf-8")) <= 65536 else []

    @staticmethod
    def _routing_projection(result: dict[str, Any]) -> dict[str, str | int | float | bool | None]:
        allowed = (
            "ok",
            "queued",
            "ambiguous",
            "empty",
            "timedOut",
            "steered",
            "details",
            "actionIntentId",
            "actionIntentStatus",
            "threadId",
            "queuedId",
            "queueDepth",
        )
        projection = {
            key: value
            for key in allowed
            if (value := result.get(key)) is None
            or isinstance(value, (str, int, float, bool))
        }
        if result.get("ambiguous") is True:
            # Keep the persisted scalar-map contract readable by 3.0 readers.
            # Never trust an incoming value for this reserved internal key.
            projection["availablePrefixesJson"] = json.dumps(
                ConversationChannelService._bounded_routing_prefixes(result.get("availablePrefixes")),
                ensure_ascii=True, separators=(",", ":"))
        return projection

    @staticmethod
    def _is_stale(
        current: ConversationMessageState,
        event: ConversationChannelEvent,
    ) -> bool:
        if current.provider_sequence is not None:
            if event.provider_sequence is None:
                return True
            return event.provider_sequence <= current.provider_sequence
        if event.provider_sequence is not None:
            return False
        if event.occurred_at < current.occurred_at:
            return True
        if event.occurred_at > current.occurred_at:
            return False
        if (
            current.provider_revision is not None
            and event.provider_revision is not None
        ):
            return current.provider_revision == event.provider_revision
        return True

    def _claim(
        self,
        *,
        canonical_event_id: str,
        event: ConversationChannelEvent,
        actor: AuthenticationActor,
    ) -> tuple[ConversationProjectionReceipt, bool]:
        claimed: list[ConversationProjectionReceipt] = []
        created = False
        now = float(self.clock())

        def apply(state: ConversationChannelState) -> ConversationChannelState:
            nonlocal created
            existing = state.receipts.get(canonical_event_id)
            if existing is not None:
                if not self._same_scope(existing, actor):
                    raise ConversationChannelConflictError("retained conversation scope changed")
                if existing.outcome == ConversationProjectionOutcome.PENDING:
                    if existing.ingest_actor_id != actor.identity_id:
                        raise ConversationChannelConflictError("retained conversation actor changed")
                    existing = existing.model_copy(update={
                        "outcome": ConversationProjectionOutcome.ROUTING,
                        "updated_at": now,
                    })
                    state.receipts[canonical_event_id] = existing
                    created = True
                claimed.append(existing)
                return state
            receipt = ConversationProjectionReceipt(
                canonical_event_id=canonical_event_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                message_key=event.message.stable_key(),
                outcome=ConversationProjectionOutcome.ROUTING,
                created_at=now,
                updated_at=now,
            )
            state.receipts[canonical_event_id] = receipt
            claimed.append(receipt)
            created = True
            return state

        self.store.update(apply)
        return claimed[0], created

    def _finish(
        self,
        canonical_event_id: str,
        *,
        actor: AuthenticationActor,
        outcome: ConversationProjectionOutcome,
        thread_id: str | None = None,
        queued_id: str | None = None,
        reason: str | None = None,
        routing_result: dict[str, str | int | float | bool | None] | None = None,
        message_state: ConversationMessageState | None = None,
    ) -> ConversationProjectionReceipt:
        saved: list[ConversationProjectionReceipt] = []
        now = float(self.clock())

        def apply(state: ConversationChannelState) -> ConversationChannelState:
            nonlocal outcome, thread_id, queued_id, routing_result
            current = state.receipts.get(canonical_event_id)
            if current is None or not self._same_scope(current, actor):
                raise ConversationChannelNotFoundError(
                    "conversation projection receipt not found"
                )
            if current.outcome == ConversationProjectionOutcome.QUEUE_ADMITTED:
                # Successful atomic queue admission is proven even if a later
                # publication or projection failed. Preserve that evidence.
                thread_id = current.thread_id
                queued_id = current.queued_id
                routing_result = current.routing_result
                outcome = ConversationProjectionOutcome.ROUTED
            updated = current.model_copy(
                update={
                    "outcome": outcome,
                    "thread_id": thread_id,
                    "queued_id": queued_id,
                    "reason": reason,
                    "routing_result": routing_result or {},
                    "updated_at": now,
                    "retained_event": (
                        None if outcome in {
                            ConversationProjectionOutcome.STALE,
                            ConversationProjectionOutcome.UPDATED,
                            ConversationProjectionOutcome.DELETED,
                            ConversationProjectionOutcome.REACTION,
                            ConversationProjectionOutcome.IGNORED,
                        } or (
                            outcome == ConversationProjectionOutcome.ROUTED
                            and (routing_result or {}).get("ok") is True
                            and not (routing_result or {}).get("ambiguous")
                        ) else current.retained_event
                    ),
                }
            )
            state.receipts[canonical_event_id] = updated
            if message_state is not None:
                state.messages[self._position_key(message_state.organization_id,
                    message_state.workspace_id, message_state.connection_id,
                    message_state.project_id, message_state.message)] = message_state
            if len(state.receipts) > self.MAX_RECEIPTS:
                protected = {
                    key: item for key, item in state.receipts.items()
                    if item.retained_event is not None or item.protected_input is not None or item.input_digest is not None
                    or item.outcome in {
                        ConversationProjectionOutcome.PENDING,
                        ConversationProjectionOutcome.ROUTING,
                        ConversationProjectionOutcome.REQUIRES_RECONCILIATION,
                    }
                }
                rows = sorted(
                    (item for key, item in state.receipts.items() if key not in protected),
                    key=lambda item: (item.updated_at, item.canonical_event_id),
                    reverse=True,
                )[: self.MAX_RECEIPTS]
                state.receipts = {**protected, **{item.canonical_event_id: item for item in rows}}
            if len(state.messages) > self.MAX_MESSAGES:
                rows = sorted(
                    state.messages.values(),
                    key=lambda item: (item.updated_at, item.last_canonical_event_id),
                    reverse=True,
                )[: self.MAX_MESSAGES]
                state.messages = {self._position_key(item.organization_id, item.workspace_id,
                    item.connection_id, item.project_id, item.message): item for item in rows}
            saved.append(updated)
            return state

        self.store.update(apply)
        return saved[0]

    def _state_for_event(
        self,
        event: ConversationChannelEvent,
        *,
        actor: AuthenticationActor,
        canonical_event_id: str,
        deleted: bool = False,
    ) -> ConversationMessageState:
        return ConversationMessageState(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            message=event.message,
            connection_id=event.connection_id, project_id=event.project_id,
            event_kind=event.event_kind,
            occurred_at=event.occurred_at,
            provider_sequence=event.provider_sequence,
            provider_revision=event.provider_revision,
            provider_cursor=event.provider_cursor,
            deleted=deleted,
            last_canonical_event_id=canonical_event_id,
            updated_at=float(self.clock()),
        )

    async def retain(
        self,
        event: ConversationChannelEvent,
        *,
        actor: AuthenticationActor,
        original_payload: dict[str, Any] | None = None,
    ) -> ConversationProjectionReceipt:
        source, key = self._retention_identity(event, actor)
        canonical_id = self.canonical_events._event_id(source, key)
        authority_snapshot = await self._capture_authority_snapshot(event, actor)
        authority_digest = (hashlib.sha256(json.dumps(authority_snapshot, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest() if authority_snapshot is not None else None)
        source_authority_digest = self._source_authority_digest(authority_snapshot)
        legacy_key = "::".join((actor.organization_id, actor.workspace_id, source, event.delivery_id))
        legacy_id = self.canonical_events._event_id(source, legacy_key)
        if (legacy_id in self.store.load().receipts
            or self.canonical_events.bus.store.event(legacy_id) is not None):
            # A new identity format is not evidence that a legacy delivery never
            # routed. Old receipts cannot prove exact connection/authority
            # compatibility, so leave them for governed reconciliation.
            raise ConversationChannelConflictError("legacy delivery identity requires reconciliation; no acknowledgement")
        content = {"event": self._event_payload(event)}
        if authority_snapshot is not None:
            content["authority_snapshot"] = authority_snapshot
        if original_payload is not None:
            content["original_payload"] = original_payload
        serialized = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        if len(serialized) > self.MAX_INPUT_BYTES:
            raise ConversationChannelUnavailableError("original input exceeds protected retention capacity; no acknowledgement")
        input_digest = self._input_digest(event, original_payload)
        existing = self.store.load().receipts.get(canonical_id)
        if existing is not None:
            # A committed compatible receipt is the durability proof. Do not
            # re-encrypt, register governance metadata or append audit writes
            # for each transport resend. A concurrent uncommitted original is
            # invisible here and still goes through the canonical transaction.
            self._validate_retained(existing, event, actor, input_digest=input_digest,
                authority_digest=authority_digest, source_authority_digest=source_authority_digest,
                check_authority=True, persisted_event=self.canonical_events.bus.store.event(canonical_id))
            return existing
        candidate = ConversationProjectionReceipt(
            canonical_event_id=canonical_id, organization_id=actor.organization_id,
            workspace_id=actor.workspace_id, message_key=event.message.stable_key(),
            outcome=ConversationProjectionOutcome.PENDING, ingest_actor_id=actor.identity_id,
            connection_id=event.connection_id, project_id=event.project_id,
            input_digest=input_digest, authority_digest=authority_digest,
            source_authority_digest=source_authority_digest,
            reason="durably retained; routing has not started",
        )
        protect_input = self.crypto is not None and event.message.conversation.provider_type == "slack"
        if protect_input:
            def protect():
                keys = [item for item in self.crypto.store.load().keys
                    if self.crypto._same_scope(item, actor)
                    if item.purpose == KeyPurpose.INTEGRATION_PAYLOAD and item.status == KeyStatus.ACTIVE
                    and item.scope.project_id == event.project_id and item.scope.resource_id is None]
                if len(keys) != 1:
                    raise ConversationChannelUnavailableError("exact Project integration-payload key required")
                return self.crypto.encrypt(keys[0].id, serialized, self._context(candidate), actor=actor)
            candidate.protected_input = await asyncio.to_thread(protect)
        else:
            candidate.retained_event = event

        def apply(raw):
            state = self.store._decode(raw)
            existing = state.receipts.get(canonical_id)
            if existing is not None:
                raise ConversationChannelConflictError("receipt exists without its atomic canonical event")
            if candidate.protected_input is not None:
                protected_bytes = sum(len(row.protected_input.ciphertext_b64) for row in state.receipts.values() if row.protected_input is not None)
                if protected_bytes + len(candidate.protected_input.ciphertext_b64) > self.MAX_PROTECTED_BYTES:
                    raise ConversationChannelUnavailableError("protected retention is full; no acknowledgement")
            state.next_retention_sequence += 1
            candidate.retention_sequence = state.next_retention_sequence - 1
            state.receipts[canonical_id] = candidate
            return state.model_dump(mode="json")

        def validate_duplicate(raw, persisted):
            receipt = self.store._decode(raw).receipts.get(persisted.event_id)
            self._validate_retained(receipt, event, actor, input_digest=input_digest,
                authority_digest=authority_digest, source_authority_digest=source_authority_digest,
                check_authority=True, persisted_event=persisted)

        def register_protection(documents):
            keys = self.crypto.store._decode(documents[self.crypto.store.namespace])
            protected = candidate.protected_input
            key_record = self.crypto._key(keys, protected.key_id, actor)
            self.crypto._ensure_key_context(key_record, self._context(candidate))
            if key_record.status != KeyStatus.ACTIVE or self.crypto._version(key_record, protected.key_version).status != KeyVersionStatus.ACTIVE:
                raise ConversationChannelUnavailableError("retention key changed before canonical commit")
            governance = self.governance.store._decode(documents[self.governance.store.namespace])
            record = self.governance.register_domain_record(GovernedDataCreate(
                project_id=event.project_id, object_type="conversation_ingress", object_id=canonical_id,
                category=DataCategory.INTEGRATION_PAYLOAD, classification=DataClassification.CONFIDENTIAL,
            ), actor=actor, state=governance)
            candidate.governance_record_id = record.id
            documents[self.governance.store.namespace] = governance.model_dump(mode="json")
            return documents

        # Event metadata, inbox/outbox identity and retained input share the
        # existing canonical document/event transaction. A duplicate waits for
        # that transaction; there is no accepted-in-memory dedupe authority.
        raw, delivery = await self.canonical_events.ingest_with_mutation(
            namespace=self.store.namespace, default=ConversationChannelState().model_dump(mode="json"),
            updater=apply, event_type=CanonicalEventType.CONVERSATION_CHANNEL,
            duplicate_validator=validate_duplicate,
            related_defaults=({self.crypto.store.namespace: CryptoKeyState().model_dump(mode="json"),
                self.governance.store.namespace: DataGovernanceState().model_dump(mode="json")} if protect_input else None),
            related_mutation=register_protection if protect_input else None,
            source=source, idempotency_key=key, occurred_at=event.occurred_at,
            tenant_id=actor.organization_id, workspace_id=actor.workspace_id,
            payload={"message_key": event.message.stable_key(), "input_digest": input_digest,
                "connection_id": event.connection_id, "project_id": event.project_id,
                "ingest_actor_id": actor.identity_id, "authority_digest": authority_digest,
                "source_authority_digest": source_authority_digest},
        )
        receipt = self.store._decode(raw).receipts.get(delivery.event.event_id)
        self._validate_retained(receipt, event, actor, input_digest=input_digest,
            authority_digest=authority_digest, source_authority_digest=source_authority_digest,
            check_authority=True, persisted_event=delivery.event)
        return receipt

    def _refuse_authority_before_routing(self, retained, actor, reason):
        """CAS only an untouched PENDING input; never overwrite a routing claim."""
        saved = []
        def apply(state):
            current = state.receipts.get(retained.canonical_event_id)
            if current is None or not self._same_scope(current, actor):
                raise ConversationChannelNotFoundError("conversation projection receipt not found")
            if (current.outcome == ConversationProjectionOutcome.PENDING
                and current.ingest_actor_id == actor.identity_id
                and current.thread_id is None and current.queued_id is None
                and current.model_dump(mode="json") == retained.model_dump(mode="json")):
                metadata = dict(current.routing_result)
                metadata.update(preRoutingAuthorityRefused=True, actionOwner="project_operator",
                    nextAction="restore_original_route_authority", authorityRefusalCode=reason,
                    authorityRecoveryCount=int(metadata.get("authorityRecoveryCount", 0)))
                current = current.model_copy(update={
                    "outcome": ConversationProjectionOutcome.REQUIRES_RECONCILIATION,
                    "reason": f"Input has not been routed: {reason}; " + (
                        "automatic recovery exhausted; operator review required"
                        if metadata["authorityRecoveryCount"] else
                        "operator review required; exact original authority permits one recovery"),
                    "routing_result": metadata, "updated_at": float(self.clock())})
                state.receipts[current.canonical_event_id] = current
            saved.append(current)
            return state
        self.store.update(apply)
        return saved[0]

    def _recover_authority_refusal(self, receipt, actor):
        """One recovery, only from our atomic pre-claim proof and exact authority."""
        metadata = receipt.routing_result
        if (receipt.outcome != ConversationProjectionOutcome.REQUIRES_RECONCILIATION
            or metadata.get("preRoutingAuthorityRefused") is not True
            or metadata.get("authorityRecoveryCount") != 0
            or receipt.thread_id is not None or receipt.queued_id is not None
            or receipt.authority_digest is None):
            return None
        try:
            content = self.retained_input(receipt, actor)
            event = ConversationChannelEvent.model_validate(content["event"])
            self._validate_retained(receipt, event, actor,
                input_digest=self._input_digest(event, content.get("original_payload")),
                authority_digest=receipt.authority_digest, source_authority_digest=receipt.source_authority_digest,
                check_authority=True,
                persisted_event=self.canonical_events.bus.store.event(receipt.canonical_event_id))
            if self._authority(event, actor) != receipt.authority_digest:
                return None
        except Exception:
            return None
        recovered = []
        def apply(state):
            current = state.receipts.get(receipt.canonical_event_id)
            if current is not None and current.model_dump(mode="json") == receipt.model_dump(mode="json"):
                metadata = dict(current.routing_result)
                metadata.update(authorityRecoveryCount=1, preRoutingAuthorityRefused=False)
                current = current.model_copy(update={"outcome": ConversationProjectionOutcome.PENDING,
                    "routing_result": metadata, "updated_at": float(self.clock()),
                    "reason": "Original authority restored; bounded pre-routing recovery admitted"})
                state.receipts[current.canonical_event_id] = current
                recovered.append(current)
            return state
        self.store.update(apply)
        return recovered[0] if recovered else None

    async def ingest(
        self,
        event: ConversationChannelEvent,
        *,
        actor: AuthenticationActor,
    ) -> ConversationProjectionReceipt:
        source, key = self._retention_identity(event, actor)
        retained = self.store.load().receipts.get(self.canonical_events._event_id(source, key))
        if retained is None:
            retained = await self.retain(event, actor=actor)
        self._validate_retained(retained, event, actor)
        if retained.outcome == ConversationProjectionOutcome.PENDING:
            content = self.retained_input(retained, actor)
            if content is None or content["event"] != event.model_dump(mode="json"):
                raise ConversationChannelConflictError("retained original input changed")
        if retained.outcome == ConversationProjectionOutcome.PENDING and retained.authority_digest is not None:
            refusal_reason = None
            try:
                current = await asyncio.to_thread(self._capture_current_authority, event, actor)
                current_digest = hashlib.sha256(json.dumps(current, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest() if current is not None else None
                if self._source_authority_digest(self.source_authority_snapshot(event, actor)) != self._source_authority_digest(current):
                    refusal_reason = "authority_changed:authenticated_source"
                elif current_digest != retained.authority_digest:
                    original = content.get("authority_snapshot") or {}
                    changed = [key for key in sorted(set(original) | set(current or {}))
                        if original.get(key) != (current or {}).get(key)]
                    if "routing" in changed:
                        changed.remove("routing")
                        previous = original.get("routing") or []
                        observed = (current or {}).get("routing") or []
                        if len(previous) == len(observed) == 1:
                            changed.extend("selected_" + key for key in sorted(set(previous[0]) | set(observed[0]))
                                if previous[0].get(key) != observed[0].get(key))
                        else:
                            changed.append("selected_binding")
                    refusal_reason = "authority_changed:" + ",".join(changed)
            except Exception as exc:
                refusal_reason = f"authority_unavailable:{type(exc).__name__}"
            if refusal_reason is not None:
                return self._refuse_authority_before_routing(retained, actor, refusal_reason)
        canonical_id = retained.canonical_event_id
        provider = event.message.conversation.provider_type
        if retained.outcome == ConversationProjectionOutcome.PENDING and self.routing_gate is not None:
            reason = self.routing_gate(event.project_id, actor)
            if reason:
                # Retention permits the provider ACK. Keep the original durable
                # receipt pending, without claiming routing or rewriting input.
                return retained.model_copy(update={"reason": reason})
        receipt, claimed = self._claim(
            canonical_event_id=canonical_id,
            event=event,
            actor=actor,
        )
        if not claimed:
            if receipt.outcome == ConversationProjectionOutcome.ROUTING:
                return receipt.model_copy(
                    update={
                        "outcome": ConversationProjectionOutcome.REQUIRES_RECONCILIATION,
                        "reason": "canonical event committed with an unfinished routing claim",
                    }
                )
            return receipt.model_copy(
                update={
                    "outcome": ConversationProjectionOutcome.DUPLICATE,
                    "reason": f"duplicate delivery; original outcome={receipt.outcome.value}",
                }
            )

        messages = self.store.load().messages
        position_key = self._message_state_key(actor, event)
        legacy_key = self._position_key(actor.organization_id, actor.workspace_id, None, None, event.message)
        if position_key != legacy_key and legacy_key in messages:
            return self._finish(canonical_id, actor=actor,
                outcome=ConversationProjectionOutcome.REQUIRES_RECONCILIATION,
                reason="legacy external-message position lacks exact connection/Project scope")
        current = messages.get(position_key)
        if current is not None and self._is_stale(current, event):
            return self._finish(
                canonical_id,
                actor=actor,
                outcome=ConversationProjectionOutcome.STALE,
                reason="provider event is not newer than the canonical external-message position",
            )

        if event.event_kind == ConversationEventKind.MESSAGE_DELETED:
            return self._finish(
                canonical_id,
                actor=actor,
                outcome=ConversationProjectionOutcome.DELETED,
                message_state=self._state_for_event(
                    event,
                    actor=actor,
                    canonical_event_id=canonical_id,
                    deleted=True,
                ),
            )

        if event.event_kind == ConversationEventKind.MESSAGE_EDITED:
            return self._finish(
                canonical_id,
                actor=actor,
                outcome=ConversationProjectionOutcome.UPDATED,
                reason="edit recorded as provider conversation state; existing canonical work is not replayed",
                message_state=self._state_for_event(
                    event,
                    actor=actor,
                    canonical_event_id=canonical_id,
                ),
            )

        if event.event_kind in {
            ConversationEventKind.REACTION_ADDED,
            ConversationEventKind.REACTION_REMOVED,
        }:
            return self._finish(
                canonical_id,
                actor=actor,
                outcome=ConversationProjectionOutcome.REACTION,
                reason="reaction recorded without creating canonical work",
            )

        text = self._message_text(event)
        if not text:
            return self._finish(
                canonical_id,
                actor=actor,
                outcome=ConversationProjectionOutcome.IGNORED,
                reason="normalized message contains no routable text or references",
                message_state=self._state_for_event(
                    event,
                    actor=actor,
                    canonical_event_id=canonical_id,
                ),
            )

        message = BotInboundMessage(
            canonical_ingress_id=canonical_id if retained.protected_input is not None else None,
            provider=provider,
            external_conversation_id=event.message.conversation.conversation_id,
            text=text,
            connection_id=event.connection_id,
            sender_id=event.sender.provider_user_id,
            sender_name=event.sender.display_name,
            project_id=event.project_id,
            external_name=event.message.conversation.display_name,
            external_thread_id=event.message.conversation.thread_id,
            message_id=event.message.message_id,
        )
        message._ingress_authority = content.get("authority_snapshot")
        message._ingress_actor_id = actor.identity_id
        try:
            result = await self.route_handler(message)
        except Exception as exc:
            return self._finish(
                canonical_id,
                actor=actor,
                outcome=ConversationProjectionOutcome.REQUIRES_RECONCILIATION,
                reason=(
                    "routing claim is durable but downstream routing outcome is uncertain: "
                    f"{getattr(exc, 'reason_code', type(exc).__name__)}"
                )[:2000],
            )

        projection = self._routing_projection(result)
        return self._finish(
            canonical_id,
            actor=actor,
            outcome=(ConversationProjectionOutcome.ROUTED if result.get("ok") is True
                else ConversationProjectionOutcome.FAILED),
            thread_id=(
                str(result.get("threadId"))
                if result.get("threadId") is not None
                else None
            ),
            queued_id=(
                str(result.get("queuedId"))
                if result.get("queuedId") is not None
                else None
            ),
            routing_result=projection,
            message_state=self._state_for_event(
                event,
                actor=actor,
                canonical_event_id=canonical_id,
            ),
        )

    @staticmethod
    def legacy_routing_result(
        receipt: ConversationProjectionReceipt,
    ) -> dict[str, Any]:
        result: dict[str, Any] = dict(receipt.routing_result)
        encoded = result.pop("availablePrefixesJson", None)
        if result.get("ambiguous") is True:
            prefixes = []
            if isinstance(encoded, str) and len(encoded) <= 65536:
                try:
                    prefixes = ConversationChannelService._bounded_routing_prefixes(json.loads(encoded))
                except (ValueError, RecursionError):
                    pass
            result["availablePrefixes"] = prefixes
        result["conversationOutcome"] = receipt.outcome.value
        result["canonicalEventId"] = receipt.canonical_event_id
        if receipt.thread_id is not None:
            result.setdefault("threadId", receipt.thread_id)
        if receipt.queued_id is not None:
            result.setdefault("queuedId", receipt.queued_id)
        if receipt.outcome == ConversationProjectionOutcome.DUPLICATE:
            result["duplicate"] = True
            result.setdefault("ok", True)
        elif receipt.outcome == ConversationProjectionOutcome.PENDING:
            result.update(ok=True, routed=False, pending=True, reason=receipt.reason)
        elif receipt.outcome == ConversationProjectionOutcome.REQUIRES_RECONCILIATION:
            result["ok"] = False
            result["requiresReconciliation"] = True
            result["error"] = receipt.reason
        elif receipt.outcome in {
            ConversationProjectionOutcome.STALE,
            ConversationProjectionOutcome.UPDATED,
            ConversationProjectionOutcome.DELETED,
            ConversationProjectionOutcome.REACTION,
            ConversationProjectionOutcome.IGNORED,
        }:
            result.setdefault("ok", True)
            result["routed"] = False
        return result

    async def ingest_raw(
        self,
        provider_type: str,
        provider_instance: str,
        payload: object,
        *,
        actor: AuthenticationActor,
        connection_id: str | None = None,
        project_id: str | None = None,
    ) -> tuple[ConversationProjectionReceipt, ...]:
        adapter = self.registry.resolve(provider_type, provider_instance)
        events = await adapter.normalize_events(
            payload,
            connection_id=connection_id,
            project_id=project_id,
        )
        return tuple(
            [
                await self.ingest(event, actor=actor)
                for event in events
            ]
        )

    async def retain_raw(
        self, provider_type: str, provider_instance: str, payload: object,
        *, actor: AuthenticationActor, connection_id: str | None = None,
        project_id: str | None = None,
        original_payload: dict[str, Any] | None = None,
    ) -> tuple[ConversationProjectionReceipt, ...]:
        adapter = self.registry.resolve(provider_type, provider_instance)
        events = await adapter.normalize_events(
            payload, connection_id=connection_id, project_id=project_id,
        )
        return tuple([await self.retain(event, actor=actor, original_payload=original_payload or payload) for event in events])

    def pending_events(
        self, *, actor: AuthenticationActor, connection_id: str,
        project_id: str, limit: int = 100,
        partition_count: int = 1,
        partition_index: Callable[[ConversationChannelEvent], int] | None = None,
    ) -> tuple[ConversationChannelEvent, ...]:
        if limit < 1 or limit > 100:
            raise ConversationChannelValidationError("pending event limit must be between 1 and 100")
        if not 1 <= partition_count <= 32:
            raise ConversationChannelValidationError("pending partition count must be between 1 and 32")
        state = self.store.load()
        # Complete only the ingress projection from atomic admission evidence.
        # Missing native receipts/queue rows cannot prove non-start and never
        # cause re-admission of this input.
        for receipt in state.receipts.values():
            if self._same_scope(receipt, actor) and receipt.ingest_actor_id == actor.identity_id and receipt.connection_id == connection_id and receipt.project_id == project_id and receipt.outcome == ConversationProjectionOutcome.QUEUE_ADMITTED:
                self._finish(receipt.canonical_event_id, actor=actor, outcome=ConversationProjectionOutcome.ROUTED,
                    reason="projection recovered from committed canonical queue admission")
        receipts = [
            receipt for receipt in state.receipts.values()
            if self._same_scope(receipt, actor)
            and receipt.ingest_actor_id == actor.identity_id
            and (receipt.outcome == ConversationProjectionOutcome.PENDING
                or (receipt.outcome == ConversationProjectionOutcome.REQUIRES_RECONCILIATION
                    and receipt.routing_result.get("preRoutingAuthorityRefused") is True
                    and receipt.routing_result.get("authorityRecoveryCount") == 0))
            and (receipt.retained_event is not None or receipt.protected_input is not None)
            and (receipt.connection_id or (receipt.retained_event.connection_id if receipt.retained_event else None)) == connection_id
            and (receipt.project_id or (receipt.retained_event.project_id if receipt.retained_event else None)) == project_id
        ]
        receipts.sort(key=lambda item: (item.outcome != ConversationProjectionOutcome.PENDING,
            item.retention_sequence, item.created_at, item.canonical_event_id))
        counts = [0] * partition_count
        selected = []
        for receipt in receipts:
            # Inspect metadata to choose the bounded shard sample BEFORE
            # decrypting. Never retrieve the whole retained backlog as payload.
            conversation = receipt.message_key.split("::")[2]
            event = receipt.retained_event
            if event is None:
                from codex_web.conversation_channels import ExternalConversationRef, ExternalMessageRef
                event = ConversationChannelEvent(delivery_id=receipt.canonical_event_id,
                    event_kind=ConversationEventKind.MESSAGE_DELETED,
                    message=ExternalMessageRef(conversation=ExternalConversationRef(
                        provider_type="slack", provider_instance=connection_id,
                        conversation_id=conversation), message_id=receipt.canonical_event_id))
            index = partition_index(event) if partition_index else 0
            if not isinstance(index, int) or not 0 <= index < partition_count:
                raise ConversationChannelValidationError("invalid pending partition index")
            if counts[index] < limit:
                counts[index] += 1
                if receipt.outcome != ConversationProjectionOutcome.PENDING:
                    receipt = self._recover_authority_refusal(receipt, actor)
                    if receipt is None:
                        continue
                try:
                    content = self.retained_input(receipt, actor)
                    selected.append(ConversationChannelEvent.model_validate(content["event"]))
                except Exception as exc:
                    self._finish(receipt.canonical_event_id, actor=actor,
                        outcome=ConversationProjectionOutcome.REQUIRES_RECONCILIATION,
                        reason=f"retained input recovery unavailable: {type(exc).__name__}")
        return tuple(selected)

    @staticmethod
    def public_receipt(receipt: ConversationProjectionReceipt) -> dict[str, Any]:
        return {
            **receipt.model_dump(mode="json", exclude={"retained_event", "protected_input"}),
            "input_retained": receipt.retained_event is not None or receipt.protected_input is not None,
            "connection_id": receipt.connection_id,
            "project_id": receipt.project_id,
            "input_protected": receipt.protected_input is not None,
            "delivery_state": ("retained_pending" if receipt.outcome == ConversationProjectionOutcome.PENDING
                else "reconciliation" if receipt.outcome in {ConversationProjectionOutcome.ROUTING, ConversationProjectionOutcome.REQUIRES_RECONCILIATION}
                else "action_admitted" if receipt.routing_result.get("actionIntentId")
                else "projected" if receipt.outcome == ConversationProjectionOutcome.ROUTED else receipt.outcome.value),
        }

    def status(self, *, actor, connection_id=None, project_id=None):
        counts = {"retained_pending": 0, "queue_admitted": 0, "action_admitted": 0, "projected": 0,
            "failed": 0, "reconciliation": 0, "other": 0}
        for receipt in self.store.load().receipts.values():
            if not self._same_scope(receipt, actor):
                continue
            if connection_id is not None and receipt.connection_id != connection_id:
                continue
            if project_id is not None and receipt.project_id != project_id:
                continue
            state = self.public_receipt(receipt)["delivery_state"]
            counts[state if state in counts else "other"] += 1
        return counts

    async def ingest_bot_message(
        self,
        message: BotInboundMessage,
        *,
        actor: AuthenticationActor,
        provider_instance: str | None = None,
        occurred_at: float | None = None,
        provider_sequence: int | None = None,
        provider_revision: str | None = None,
    ) -> ConversationProjectionReceipt:
        from codex_web.conversation_channels import (
            ConversationChannelEvent,
            ConversationEventKind,
            ConversationSenderRef,
            ConversationSenderKind,
            ExternalConversationRef,
            ExternalMessageRef,
        )

        instance = (
            provider_instance
            or message.connection_id
            or f"{message.provider}:default"
        )
        message_id = (message.message_id or "").strip()
        if not message_id:
            material = json.dumps(
                {
                    "provider": message.provider,
                    "instance": instance,
                    "conversation": message.external_conversation_id,
                    "thread": message.external_thread_id,
                    "sender": message.sender_id,
                    "text": message.text,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            message_id = f"synthetic-{hashlib.sha256(material).hexdigest()[:32]}"
        event = ConversationChannelEvent(
            delivery_id=f"message:{message_id}",
            event_kind=ConversationEventKind.MESSAGE_CREATED,
            message=ExternalMessageRef(
                conversation=ExternalConversationRef(
                    provider_type=message.provider,
                    provider_instance=instance,
                    conversation_id=message.external_conversation_id,
                    thread_id=message.external_thread_id,
                    display_name=message.external_name,
                ),
                message_id=message_id,
            ),
            sender=ConversationSenderRef(
                provider_user_id=message.sender_id,
                display_name=message.sender_name,
                kind=ConversationSenderKind.UNKNOWN,
            ),
            text=message.text,
            occurred_at=(
                float(self.clock())
                if occurred_at is None
                else float(occurred_at)
            ),
            provider_sequence=provider_sequence,
            provider_revision=provider_revision,
            connection_id=message.connection_id,
            project_id=message.project_id,
        )
        return await self.ingest(event, actor=actor)

    def provider_capabilities(self) -> tuple[dict[str, Any], ...]:
        return self.registry.provider_contracts()

    def receipts(
        self,
        *,
        actor: AuthenticationActor,
        limit: int = 200,
    ) -> tuple[ConversationProjectionReceipt, ...]:
        if limit < 1 or limit > 1000:
            raise ConversationChannelValidationError(
                "conversation receipt limit must be between 1 and 1000"
            )
        rows = [
            item
            for item in self.store.load().receipts.values()
            if self._same_scope(item, actor)
        ]
        rows.sort(
            key=lambda item: (item.updated_at, item.canonical_event_id),
            reverse=True,
        )
        return tuple(rows[:limit])

    def message_states(
        self,
        *,
        actor: AuthenticationActor,
        provider_type: str | None = None,
        limit: int = 200,
    ) -> tuple[ConversationMessageState, ...]:
        if limit < 1 or limit > 1000:
            raise ConversationChannelValidationError(
                "conversation message limit must be between 1 and 1000"
            )
        rows = [
            item
            for item in self.store.load().messages.values()
            if self._same_scope(item, actor)
        ]
        if provider_type is not None:
            rows = [
                item
                for item in rows
                if item.message.conversation.provider_type.casefold()
                == provider_type.casefold()
            ]
        rows.sort(
            key=lambda item: (item.updated_at, item.last_canonical_event_id),
            reverse=True,
        )
        return tuple(rows[:limit])
