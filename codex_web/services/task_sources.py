from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from codex_web.compatibility import TASK_SOURCE_CONTRACT
from codex_web.models import TaskSourceIdentity, WorkItemStage


class TaskSourceCapability(StrEnum):
    """Capabilities an authoritative task-source adapter may expose."""

    CREATE = "create"
    DISCOVERY = "discovery"
    READ = "read"
    EVENTS = "events"
    OWNER_WRITE = "owner_write"
    STATE_WRITE = "state_write"
    COMMENTS = "comments"
    ARTIFACT_LINKS = "artifact_links"
    PAGED_DISCOVERY = "paged_discovery"
    INCREMENTAL_RECONCILIATION = "incremental_reconciliation"
    WORKFLOW_TRANSITIONS = "workflow_transitions"
    RICH_TEXT = "rich_text"
    PROVIDER_IDENTITIES = "provider_identities"
    ATTACHMENTS = "attachments"


@dataclass(frozen=True, slots=True)
class TaskSourceCapabilities:
    """Immutable capability declaration for one task-source adapter."""

    supported: frozenset[TaskSourceCapability] = field(default_factory=frozenset)

    def supports(self, capability: TaskSourceCapability) -> bool:
        return capability in self.supported

    def require(self, capability: TaskSourceCapability) -> None:
        if not self.supports(capability):
            raise UnsupportedTaskSourceCapability(capability)


class UnsupportedTaskSourceCapability(RuntimeError):
    def __init__(self, capability: TaskSourceCapability) -> None:
        self.capability = capability
        super().__init__(f"Task source does not support capability: {capability.value}")


@dataclass(frozen=True, slots=True)
class TaskSourceUserReference:
    """Provider-native user reference that is never canonical authorization truth."""

    provider_id: str
    display_name: str | None = None
    username: str | None = None
    email_hint: str | None = None

    def __post_init__(self) -> None:
        provider_id = str(self.provider_id or "").strip()
        if not provider_id:
            raise ValueError("provider user reference must include provider_id")
        object.__setattr__(self, "provider_id", provider_id)
        for field_name in ("display_name", "username", "email_hint"):
            value = getattr(self, field_name)
            normalized = str(value).strip() if value is not None else None
            object.__setattr__(self, field_name, normalized or None)


@dataclass(frozen=True, slots=True)
class TaskSourceSnapshot:
    """Provider-neutral task facts exposed to canonical reconciliation."""

    identity: TaskSourceIdentity
    title: str | None = None
    body_text: str | None = None
    source_state: str | None = None
    owners: tuple[str, ...] = ()
    owner_references: tuple[TaskSourceUserReference, ...] = ()
    labels: tuple[str, ...] = ()
    priority: str | None = None
    category: str | None = None
    parent_external_id: str | None = None
    artifact_links: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskSourcePage:
    """One bounded provider-neutral discovery page."""

    items: tuple[TaskSourceSnapshot, ...]
    next_cursor: str | None = None
    watermark: str | None = None
    exhausted: bool = True

    def __post_init__(self) -> None:
        cursor = str(self.next_cursor).strip() if self.next_cursor is not None else None
        watermark = str(self.watermark).strip() if self.watermark is not None else None
        object.__setattr__(self, "next_cursor", cursor or None)
        object.__setattr__(self, "watermark", watermark or None)
        if not self.exhausted and self.next_cursor is None:
            raise ValueError("non-exhausted task-source page must include next_cursor")


@dataclass(frozen=True, slots=True)
class TaskSourceReconciliationCursor:
    """Stable incremental-sync position with deterministic tie-breaking."""

    watermark: str
    tiebreaker: str | None = None

    def __post_init__(self) -> None:
        watermark = str(self.watermark or "").strip()
        if not watermark:
            raise ValueError("task-source reconciliation watermark must not be empty")
        tiebreaker = str(self.tiebreaker).strip() if self.tiebreaker is not None else None
        object.__setattr__(self, "watermark", watermark)
        object.__setattr__(self, "tiebreaker", tiebreaker or None)


@dataclass(frozen=True, slots=True)
class TaskSourceCreateRequest:
    """Provider-neutral request to create one authoritative task."""

    title: str
    body: str | None = None
    owners: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        title = str(self.title or "").strip()
        if not title:
            raise ValueError("task-source create title must not be empty")
        body = str(self.body).strip() if self.body is not None else None
        owners = tuple(
            dict.fromkeys(
                value
                for value in (str(item or "").strip() for item in self.owners)
                if value
            )
        )
        labels = tuple(
            dict.fromkeys(
                value
                for value in (str(item or "").strip() for item in self.labels)
                if value
            )
        )
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "body", body or None)
        object.__setattr__(self, "owners", owners)
        object.__setattr__(self, "labels", labels)


@dataclass(frozen=True, slots=True)
class TaskSourceEvent:
    """Versioned provider-neutral event envelope before reconciliation."""

    identity: TaskSourceIdentity
    event_type: str
    occurred_at: float | None = None
    snapshot: TaskSourceSnapshot | None = None
    schema_version: str = TASK_SOURCE_CONTRACT.current

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise ValueError("event_type must not be empty")
        TASK_SOURCE_CONTRACT.require(self.schema_version)


@dataclass(frozen=True, slots=True)
class TaskSourceCanonicalProjection:
    """Provider-neutral canonical facts produced by deterministic adapter mapping."""

    identity: TaskSourceIdentity
    stage: WorkItemStage | None = None
    owner: str | None = None
    owner_known: bool = True
    source_state: str | None = None


@runtime_checkable
class TaskSource(Protocol):
    """Authoritative external task-system contract.

    Adapters declare capabilities up front. Adapters may also expose
    ``contract_version`` for explicit compatibility negotiation; it remains
    outside the runtime-checkable structural protocol so pre-versioned v1
    adapters stay protocol-compatible during the documented migration window.
    Core work-item code can reject declared incompatible versions or unsupported
    operations deterministically rather than assuming GitLab-like behavior.
    Provider-specific API objects must be normalized into
    ``TaskSourceSnapshot``/``TaskSourceEvent`` before crossing this boundary.
    """

    source_type: str
    source_instance: str
    capabilities: TaskSourceCapabilities

    async def discover(self, *, scope: str) -> list[TaskSourceSnapshot]:
        """Discover authoritative task items within a configured source scope."""
        ...

    async def read(self, identity: TaskSourceIdentity) -> TaskSourceSnapshot:
        """Read one authoritative external task item."""
        ...

    async def normalize_event(self, payload: object) -> TaskSourceEvent | None:
        """Normalize one provider webhook/event payload without mutating state."""
        ...

    def project(
        self,
        snapshot: TaskSourceSnapshot,
        *,
        current_stage: WorkItemStage | None = None,
    ) -> TaskSourceCanonicalProjection:
        """Map provider-native normalized facts into canonical work-item facts.

        This mapping is deterministic adapter logic. ``current_stage`` may be
        used when a provider state is less specific than codex-web's lifecycle,
        but an adapter must not mutate canonical state while projecting.
        """
        ...

    async def write_owner(self, identity: TaskSourceIdentity, owner: str | None) -> TaskSourceSnapshot:
        """Project canonical owner state back to the authoritative source."""
        ...

    async def write_state(self, identity: TaskSourceIdentity, state: str) -> TaskSourceSnapshot:
        """Project canonical lifecycle state back to the authoritative source."""
        ...

    async def add_comment(self, identity: TaskSourceIdentity, body: str) -> None:
        """Append a comment when the source declares comment support."""
        ...

    async def attach_artifact(self, identity: TaskSourceIdentity, url: str) -> None:
        """Attach/link an artifact when the source declares artifact support."""
        ...


@runtime_checkable
class TaskSourceCreateCapable(Protocol):
    """Optional additive creation contract gated by TaskSourceCapability.CREATE."""

    async def create(
        self,
        request: TaskSourceCreateRequest,
        *,
        scope: str,
    ) -> TaskSourceSnapshot:
        """Create one authoritative task and return normalized provider identity."""
        ...


@runtime_checkable
class TaskSourcePagedDiscoveryCapable(Protocol):
    """Optional bounded pagination contract gated by PAGED_DISCOVERY."""

    async def discover_page(
        self,
        *,
        scope: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> TaskSourcePage:
        ...


@runtime_checkable
class TaskSourceIncrementalReconciliationCapable(Protocol):
    """Optional watermark/cursor reconciliation contract."""

    async def reconcile_since(
        self,
        *,
        scope: str,
        cursor: TaskSourceReconciliationCursor | None = None,
        limit: int = 100,
    ) -> TaskSourcePage:
        ...


@runtime_checkable
class TaskSourceWorkflowCapable(Protocol):
    """Optional provider workflow transition contract."""

    async def available_transitions(
        self,
        identity: TaskSourceIdentity,
    ) -> tuple[str, ...]:
        ...

    async def transition(
        self,
        identity: TaskSourceIdentity,
        transition: str,
    ) -> TaskSourceSnapshot:
        ...
