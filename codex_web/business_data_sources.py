from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.business_context import (
    BusinessEntityType,
    FactQuality,
    FactSourceAuthority,
    FactValueType,
)
from codex_web.compatibility import ContractSpec
from codex_web.data_governance import DataClassification


BUSINESS_DATA_SOURCE_CONTRACT = ContractSpec(
    "business-data-source",
    "1.0",
    ("1.0",),
)


class BusinessDataSourceCapability(StrEnum):
    DISCOVERY = "discovery"
    READ = "read"
    EVENTS = "events"
    PAGED_DISCOVERY = "paged_discovery"
    INCREMENTAL_SYNC = "incremental_sync"
    HISTORICAL_READ = "historical_read"
    TOMBSTONES = "tombstones"


@dataclass(frozen=True, slots=True)
class BusinessDataSourceCapabilities:
    supported: frozenset[BusinessDataSourceCapability] = field(
        default_factory=frozenset
    )

    def supports(self, capability: BusinessDataSourceCapability) -> bool:
        return capability in self.supported

    def require(self, capability: BusinessDataSourceCapability) -> None:
        if not self.supports(capability):
            raise UnsupportedBusinessDataSourceCapability(capability)


class UnsupportedBusinessDataSourceCapability(RuntimeError):
    def __init__(self, capability: BusinessDataSourceCapability) -> None:
        self.capability = capability
        super().__init__(
            "Business data source does not support capability: "
            f"{capability.value}"
        )


@dataclass(frozen=True, slots=True)
class BusinessDataField:
    source_field: str
    value: str | float | int | bool | None

    def __post_init__(self) -> None:
        field_name = str(self.source_field or "").strip()
        if not field_name:
            raise ValueError("business data field name must not be empty")
        if self.value is not None and type(self.value) not in {
            str,
            float,
            int,
            bool,
        }:
            raise ValueError("business data fields must be bounded scalar values")
        if isinstance(self.value, str) and len(self.value) > 8000:
            raise ValueError("business data field string exceeds 8000 characters")
        object.__setattr__(self, "source_field", field_name)


@dataclass(frozen=True, slots=True)
class BusinessDataSnapshot:
    """Provider-neutral minimum-sufficient record snapshot.

    entity_key is a configured stable business identity shared across sources,
    while external_id remains provider-owned identity. Adapters must not put raw
    provider payloads here.
    """

    object_type: str
    external_id: str
    entity_key: str
    entity_name: str
    fields: tuple[BusinessDataField, ...] = ()
    display_name: str | None = None
    external_url: str | None = None
    source_updated_at: float | None = None
    source_sequence: int | None = None
    source_revision: str | None = None
    tombstone: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "object_type",
            "external_id",
            "entity_key",
            "entity_name",
        ):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        display = (
            str(self.display_name).strip()
            if self.display_name is not None
            else None
        )
        url = (
            str(self.external_url).strip()
            if self.external_url is not None
            else None
        )
        revision = (
            str(self.source_revision).strip()
            if self.source_revision is not None
            else None
        )
        object.__setattr__(self, "display_name", display or None)
        object.__setattr__(self, "external_url", url or None)
        object.__setattr__(self, "source_revision", revision or None)
        seen: set[str] = set()
        for item in self.fields:
            key = item.source_field.casefold()
            if key in seen:
                raise ValueError(
                    f"duplicate business data source field: {item.source_field}"
                )
            seen.add(key)


@dataclass(frozen=True, slots=True)
class BusinessDataPage:
    items: tuple[BusinessDataSnapshot, ...]
    next_cursor: str | None = None
    checkpoint: str | None = None
    exhausted: bool = True

    def __post_init__(self) -> None:
        cursor = (
            str(self.next_cursor).strip()
            if self.next_cursor is not None
            else None
        )
        checkpoint = (
            str(self.checkpoint).strip()
            if self.checkpoint is not None
            else None
        )
        object.__setattr__(self, "next_cursor", cursor or None)
        object.__setattr__(self, "checkpoint", checkpoint or None)
        if not self.exhausted and self.next_cursor is None:
            raise ValueError(
                "non-exhausted business data page requires next_cursor"
            )


class BusinessDataEventKind(StrEnum):
    UPSERT = "upsert"
    TOMBSTONE = "tombstone"


@dataclass(frozen=True, slots=True)
class BusinessDataSourceEvent:
    event_id: str
    event_kind: BusinessDataEventKind
    snapshot: BusinessDataSnapshot
    occurred_at: float | None = None
    cursor: str | None = None
    checkpoint: str | None = None
    contract_version: str = BUSINESS_DATA_SOURCE_CONTRACT.current

    def __post_init__(self) -> None:
        event_id = str(self.event_id or "").strip()
        if not event_id:
            raise ValueError("business data source event_id must not be empty")
        BUSINESS_DATA_SOURCE_CONTRACT.require(self.contract_version)
        object.__setattr__(self, "event_id", event_id)
        if (
            self.event_kind == BusinessDataEventKind.TOMBSTONE
            and not self.snapshot.tombstone
        ):
            object.__setattr__(
                self,
                "snapshot",
                BusinessDataSnapshot(
                    object_type=self.snapshot.object_type,
                    external_id=self.snapshot.external_id,
                    entity_key=self.snapshot.entity_key,
                    entity_name=self.snapshot.entity_name,
                    fields=self.snapshot.fields,
                    display_name=self.snapshot.display_name,
                    external_url=self.snapshot.external_url,
                    source_updated_at=self.snapshot.source_updated_at,
                    source_sequence=self.snapshot.source_sequence,
                    source_revision=self.snapshot.source_revision,
                    tombstone=True,
                ),
            )


@runtime_checkable
class BusinessDataSource(Protocol):
    source_type: str
    source_instance: str
    capabilities: BusinessDataSourceCapabilities
    contract_version: str

    async def discover_page(
        self,
        *,
        scope: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> BusinessDataPage:
        ...

    async def sync_since(
        self,
        *,
        scope: str,
        cursor: str | None = None,
        limit: int = 100,
    ) -> BusinessDataPage:
        ...

    async def read(
        self,
        *,
        object_type: str,
        external_id: str,
    ) -> BusinessDataSnapshot:
        ...

    async def normalize_event(
        self,
        payload: object,
    ) -> BusinessDataSourceEvent | None:
        ...


class BusinessDataSourceStatus(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    PAUSED = "paused"
    QUARANTINED = "quarantined"


class BusinessDataFieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source_field: str = Field(min_length=1, max_length=300)
    fact_key: str = Field(min_length=1, max_length=300)
    value_type: FactValueType
    unit: str | None = Field(default=None, max_length=100)
    authority: FactSourceAuthority = FactSourceAuthority.OBSERVED
    priority: int = Field(default=0, ge=0, le=1000)
    quality: FactQuality = FactQuality.NORMAL
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    freshness_seconds: int | None = Field(
        default=None,
        ge=1,
        le=31_536_000,
    )
    classification: DataClassification = DataClassification.INTERNAL


class BusinessDataSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=300)
    source_type: str = Field(min_length=1, max_length=200)
    source_instance: str = Field(min_length=1, max_length=1000)
    provider_id: str = Field(min_length=1, max_length=200)
    extension_installation_id: str | None = Field(default=None, max_length=500)
    scope: str = Field(min_length=1, max_length=1000)
    object_type: str = Field(min_length=1, max_length=200)
    entity_type: BusinessEntityType
    entity_key_namespace: str = Field(min_length=1, max_length=200)
    entity_name_authority: FactSourceAuthority = FactSourceAuthority.OBSERVED
    entity_name_priority: int = Field(default=0, ge=0, le=1000)
    field_mappings: tuple[BusinessDataFieldMapping, ...] = ()
    credential_ref: str | None = Field(default=None, max_length=1000)
    classification: DataClassification = DataClassification.INTERNAL
    reconciliation_interval_seconds: int | None = Field(
        default=None,
        ge=60,
        le=31_536_000,
    )
    page_size: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def validate_mappings(self) -> "BusinessDataSourceCreate":
        source_fields = [
            item.source_field.casefold() for item in self.field_mappings
        ]
        if len(source_fields) != len(set(source_fields)):
            raise ValueError("business data field mappings require unique source_field")
        fact_keys = [item.fact_key.casefold() for item in self.field_mappings]
        if len(fact_keys) != len(set(fact_keys)):
            raise ValueError("business data field mappings require unique fact_key")
        return self


class BusinessDataSourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(
        default_factory=lambda: f"business-source-{uuid.uuid4().hex}"
    )
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    name: str
    source_type: str
    source_instance: str
    provider_id: str
    extension_installation_id: str | None = None
    scope: str
    object_type: str
    entity_type: BusinessEntityType
    entity_key_namespace: str
    entity_name_authority: FactSourceAuthority
    entity_name_priority: int = Field(default=0, ge=0, le=1000)
    field_mappings: tuple[BusinessDataFieldMapping, ...] = ()
    credential_ref: str | None = None
    credential_required: bool = True
    classification: DataClassification
    capabilities: tuple[BusinessDataSourceCapability, ...] = ()
    status: BusinessDataSourceStatus = BusinessDataSourceStatus.ACTIVE
    cursor: str | None = None
    checkpoint: str | None = None
    reconciliation_interval_seconds: int | None = None
    schedule_id: str | None = None
    page_size: int = Field(default=100, ge=1, le=500)
    last_sync_started_at: float | None = None
    last_sync_completed_at: float | None = None
    last_success_at: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    projected_records: int = Field(default=0, ge=0)
    stale_events: int = Field(default=0, ge=0)
    duplicate_events: int = Field(default=0, ge=0)
    created_by: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class BusinessEntityBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    organization_id: str
    workspace_id: str
    entity_key_namespace: str = Field(min_length=1)
    entity_key: str = Field(min_length=1)
    business_entity_id: str = Field(min_length=1)
    created_by_source_id: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)

    def key(self) -> tuple[str, str, str, str]:
        return (
            self.organization_id,
            self.workspace_id,
            self.entity_key_namespace.casefold(),
            self.entity_key.casefold(),
        )


class BusinessDataProjectionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_event_id: str
    source_id: str
    external_record_ref_id: str | None = None
    business_entity_id: str | None = None
    fact_ids: tuple[str, ...] = ()
    outcome: str
    reason: str | None = None
    projected_at: float = Field(default_factory=time.time)


class BusinessDataSourceState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    sources: dict[str, BusinessDataSourceRecord] = Field(default_factory=dict)
    bindings: list[BusinessEntityBinding] = Field(default_factory=list)
    projection_receipts: dict[str, BusinessDataProjectionReceipt] = Field(
        default_factory=dict
    )


class BusinessDataSyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    pages: int = Field(ge=0)
    records_seen: int = Field(ge=0)
    projected: int = Field(ge=0)
    duplicates: int = Field(ge=0)
    stale: int = Field(ge=0)
    tombstones: int = Field(ge=0)
    cursor_before: str | None = None
    cursor_after: str | None = None
    checkpoint: str | None = None
    exhausted: bool


class BusinessDataSourceStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: BusinessDataSourceStatus
