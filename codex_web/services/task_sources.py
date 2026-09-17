from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class TaskSourceCapability(StrEnum):
    """Capabilities an authoritative task-source adapter may expose."""

    DISCOVERY = "discovery"
    READ = "read"
    EVENTS = "events"
    OWNER_WRITE = "owner_write"
    STATE_WRITE = "state_write"
    COMMENTS = "comments"
    ARTIFACT_LINKS = "artifact_links"


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
class TaskSourceIdentity:
    """Provider-neutral external identity/provenance for one authoritative item."""

    source_type: str
    source_instance: str
    external_id: str
    external_url: str | None = None
    revision: str | None = None
    event_cursor: str | None = None

    def __post_init__(self) -> None:
        if not self.source_type.strip():
            raise ValueError("source_type must not be empty")
        if not self.source_instance.strip():
            raise ValueError("source_instance must not be empty")
        if not self.external_id.strip():
            raise ValueError("external_id must not be empty")


@dataclass(frozen=True, slots=True)
class TaskSourceSnapshot:
    """Minimum provider-neutral task facts exposed to canonical reconciliation."""

    identity: TaskSourceIdentity
    title: str | None = None
    source_state: str | None = None
    owners: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    artifact_links: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskSourceEvent:
    """Provider-neutral event envelope before canonical work-item reconciliation."""

    identity: TaskSourceIdentity
    event_type: str
    occurred_at: float | None = None
    snapshot: TaskSourceSnapshot | None = None

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise ValueError("event_type must not be empty")


@runtime_checkable
class TaskSource(Protocol):
    """Authoritative external task-system contract.

    Adapters declare capabilities up front. Core work-item code can therefore
    fail deterministically when a provider does not support an operation rather
    than assuming GitLab-like behavior. Provider-specific API objects must be
    normalized into ``TaskSourceSnapshot``/``TaskSourceEvent`` before crossing
    this boundary.
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
