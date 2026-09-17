from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class ContractCompatibilityError(ValueError):
    """Raised when a durable/public contract cannot be interpreted safely."""

    def __init__(
        self,
        contract: str,
        received: str,
        *,
        supported: tuple[str, ...],
        reason: str = "unsupported_version",
    ) -> None:
        self.contract = contract
        self.received = received
        self.supported = supported
        self.reason = reason
        super().__init__(
            f"Unsupported {contract} version {received!r}; supported: {', '.join(supported)}"
        )


@dataclass(frozen=True, order=True, slots=True)
class ContractVersion:
    major: int
    minor: int = 0

    @classmethod
    def parse(cls, value: str | int | float) -> ContractVersion:
        text = str(value).strip()
        parts = text.split(".")
        if len(parts) > 2 or not parts[0].isdigit() or (len(parts) == 2 and not parts[1].isdigit()):
            raise ValueError(f"Invalid contract version: {value!r}")
        return cls(int(parts[0]), int(parts[1]) if len(parts) == 2 else 0)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """Explicit compatibility window for one public/durable contract.

    Supported versions are exact. New additive minor versions therefore require
    an intentional code change before consumers accept them; a future producer
    can never rely on an older process silently ignoring unknown semantics.
    """

    name: str
    current: str
    supported: tuple[str, ...]
    deprecated: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("contract name must not be empty")
        current = str(ContractVersion.parse(self.current))
        supported = tuple(str(ContractVersion.parse(value)) for value in self.supported)
        if current not in supported:
            raise ValueError("current contract version must be in supported versions")
        unknown_deprecated = set(self.deprecated) - set(supported)
        if unknown_deprecated:
            raise ValueError("deprecated versions must also be supported")

    def require(self, received: str | int | float) -> ContractVersion:
        version = str(ContractVersion.parse(received))
        normalized_supported = tuple(str(ContractVersion.parse(value)) for value in self.supported)
        if version not in normalized_supported:
            raise ContractCompatibilityError(
                self.name,
                version,
                supported=normalized_supported,
            )
        return ContractVersion.parse(version)

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "current": str(ContractVersion.parse(self.current)),
            "supported": [str(ContractVersion.parse(value)) for value in self.supported],
            "deprecated": [str(ContractVersion.parse(value)) for value in self.deprecated],
        }


API_CONTRACT = ContractSpec("http-api", "1.0", ("1.0",))
CANONICAL_EVENT_CONTRACT = ContractSpec("canonical-event", "1.0", ("1.0",))
TASK_SOURCE_CONTRACT = ContractSpec("task-source", "1.0", ("1.0",))
PERSISTED_RECORD_CONTRACT = ContractSpec("persisted-record", "1.0", ("1.0",))


class CanonicalEventEnvelope(BaseModel):
    """Versioned event boundary shared by future event producers/consumers."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: str = "1.0"
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    occurred_at: float
    source: str = Field(min_length=1)
    correlation_id: str | None = None
    causation_id: str | None = None
    tenant_id: str | None = None
    workspace_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        CANONICAL_EVENT_CONTRACT.require(self.schema_version)


class VersionedRecord(BaseModel):
    """Minimal wrapper for durable documents that need explicit evolution."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    payload: dict[str, Any]

    def model_post_init(self, __context: Any) -> None:
        PERSISTED_RECORD_CONTRACT.require(self.schema_version)


Migration = Callable[[dict[str, Any]], dict[str, Any]]


class MigrationRegistry:
    """Deterministic, stepwise migration registry for persisted payloads."""

    def __init__(self, contract: str) -> None:
        self.contract = contract
        self._steps: dict[tuple[str, str], Migration] = {}

    def register(self, from_version: str, to_version: str, migration: Migration) -> None:
        source = str(ContractVersion.parse(from_version))
        target = str(ContractVersion.parse(to_version))
        if ContractVersion.parse(target) <= ContractVersion.parse(source):
            raise ValueError("migration target must be newer than source")
        key = (source, target)
        if key in self._steps:
            raise ValueError(f"migration already registered: {source} -> {target}")
        self._steps[key] = migration

    def path(self, from_version: str, to_version: str) -> tuple[tuple[str, str], ...]:
        source = str(ContractVersion.parse(from_version))
        target = str(ContractVersion.parse(to_version))
        if source == target:
            return ()
        current = source
        path: list[tuple[str, str]] = []
        visited: set[str] = set()
        while current != target:
            if current in visited:
                raise ContractCompatibilityError(
                    self.contract,
                    source,
                    supported=(target,),
                    reason="migration_cycle",
                )
            visited.add(current)
            candidates = sorted(
                (edge for edge in self._steps if edge[0] == current),
                key=lambda edge: ContractVersion.parse(edge[1]),
            )
            if not candidates:
                raise ContractCompatibilityError(
                    self.contract,
                    source,
                    supported=(target,),
                    reason="migration_path_missing",
                )
            viable = [
                edge
                for edge in candidates
                if ContractVersion.parse(edge[1]) <= ContractVersion.parse(target)
            ]
            if not viable:
                raise ContractCompatibilityError(
                    self.contract,
                    source,
                    supported=(target,),
                    reason="migration_path_missing",
                )
            edge = viable[-1]
            path.append(edge)
            current = edge[1]
        return tuple(path)

    def migrate(
        self,
        payload: Mapping[str, Any],
        *,
        from_version: str,
        to_version: str,
    ) -> dict[str, Any]:
        result = dict(payload)
        for edge in self.path(from_version, to_version):
            result = dict(self._steps[edge](dict(result)))
        return result

    def assert_idempotent(
        self,
        payload: Mapping[str, Any],
        *,
        from_version: str,
        to_version: str,
    ) -> dict[str, Any]:
        first = self.migrate(payload, from_version=from_version, to_version=to_version)
        # A payload already at the target version is a no-op by definition.
        second = self.migrate(first, from_version=to_version, to_version=to_version)
        if first != second:
            raise ValueError("migration is not idempotent at target version")
        return first


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class NegotiatedContract(Generic[T]):
    value: T
    version: str
    deprecated: bool


def negotiate(
    value: T,
    *,
    version: str,
    spec: ContractSpec,
) -> NegotiatedContract[T]:
    normalized = str(spec.require(version))
    return NegotiatedContract(
        value=value,
        version=normalized,
        deprecated=normalized in {str(ContractVersion.parse(v)) for v in spec.deprecated},
    )
