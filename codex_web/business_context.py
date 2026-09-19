from __future__ import annotations

import json
import time
import uuid
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.data_governance import DataClassification


BUSINESS_CONTEXT_CONTRACT = ContractSpec(
    "business-context-state",
    "1.0",
    ("1.0",),
)


class BusinessEntityType(StrEnum):
    CUSTOMER = "customer"
    ACCOUNT = "account"
    PRODUCT = "product"
    PRODUCT_AREA = "product_area"
    SUBSCRIPTION = "subscription"
    COMMERCIAL_AGREEMENT = "commercial_agreement"
    OPPORTUNITY = "opportunity"
    CAMPAIGN = "campaign"
    SUPPORT_RELATIONSHIP = "support_relationship"
    VENDOR = "vendor"
    PARTNER = "partner"
    COST_CENTER = "cost_center"
    OTHER = "other"


class BusinessEntityLifecycle(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    REDACTED = "redacted"
    ANONYMIZED = "anonymized"
    DELETED = "deleted"


class ExternalRecordLifecycle(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    REDACTED = "redacted"
    ANONYMIZED = "anonymized"
    DELETED = "deleted"


class CompanyFactLifecycle(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    REDACTED = "redacted"
    ANONYMIZED = "anonymized"
    DELETED = "deleted"


class FactSourceAuthority(StrEnum):
    OBSERVED = "observed"
    SECONDARY = "secondary"
    AUTHORITATIVE = "authoritative"


FACT_AUTHORITY_RANK: dict[FactSourceAuthority, int] = {
    FactSourceAuthority.OBSERVED: 1,
    FactSourceAuthority.SECONDARY: 2,
    FactSourceAuthority.AUTHORITATIVE: 3,
}


class FactQuality(StrEnum):
    SUSPECT = "suspect"
    PARTIAL = "partial"
    NORMAL = "normal"
    VERIFIED = "verified"


FACT_QUALITY_RANK: dict[FactQuality, int] = {
    FactQuality.SUSPECT: 0,
    FactQuality.PARTIAL: 1,
    FactQuality.NORMAL: 2,
    FactQuality.VERIFIED: 3,
}


class FactFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    SOURCE_REVOKED = "source_revoked"


class BusinessRelationshipType(StrEnum):
    RELATED_TO = "related_to"
    OWNS = "owns"
    USES = "uses"
    SUBSCRIBES_TO = "subscribes_to"
    SUPPLIED_BY = "supplied_by"
    SUPPORTS = "supports"
    PARTNER_OF = "partner_of"
    PARENT_OF = "parent_of"


class FactValueType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"


FactScalar = str | float | int | bool


class BusinessReferenceLinks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_ids: tuple[str, ...] = ()
    resource_ids: tuple[str, ...] = ()
    goal_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    metric_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "BusinessReferenceLinks":
        for field in (
            "project_ids",
            "resource_ids",
            "goal_ids",
            "decision_ids",
            "metric_ids",
            "evidence_ids",
        ):
            values = tuple(
                dict.fromkeys(
                    value.strip()
                    for value in getattr(self, field)
                    if value and value.strip()
                )
            )
            object.__setattr__(self, field, values)
        return self


class BusinessFieldProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    field_name: str = Field(min_length=1, max_length=200)
    external_record_ref_id: str | None = Field(default=None, max_length=500)
    provider: str | None = Field(default=None, max_length=200)
    authority: FactSourceAuthority = FactSourceAuthority.OBSERVED
    priority: int = Field(default=0, ge=0, le=1000)
    source_revision: str | None = Field(default=None, max_length=500)
    observed_at: float | None = None


class BusinessEntityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    entity_type: BusinessEntityType
    name: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=4000)
    classification: DataClassification = DataClassification.INTERNAL
    links: BusinessReferenceLinks = Field(default_factory=BusinessReferenceLinks)
    field_provenance: tuple[BusinessFieldProvenance, ...] = ()
    retention_policy_ref: str | None = Field(default=None, max_length=500)
    retention_expires_at: float | None = None


class BusinessEntity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"business-entity-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    entity_type: BusinessEntityType
    name: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=4000)
    classification: DataClassification
    lifecycle: BusinessEntityLifecycle = BusinessEntityLifecycle.ACTIVE
    links: BusinessReferenceLinks = Field(default_factory=BusinessReferenceLinks)
    field_provenance: tuple[BusinessFieldProvenance, ...] = ()
    governance_record_id: str | None = None
    created_by: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def normalize_provenance(self) -> "BusinessEntity":
        rows: dict[str, BusinessFieldProvenance] = {}
        for item in self.field_provenance:
            rows[item.field_name.casefold()] = item
        self.field_provenance = tuple(rows.values())
        return self


class BusinessEntityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=4000)
    lifecycle: BusinessEntityLifecycle | None = None
    links: BusinessReferenceLinks | None = None
    field_provenance: tuple[BusinessFieldProvenance, ...] | None = None


class ExternalRecordRefCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    system: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=200)
    provider_instance: str | None = Field(default=None, max_length=200)
    object_type: str = Field(min_length=1, max_length=200)
    external_id: str = Field(min_length=1, max_length=1000)
    display_name: str | None = Field(default=None, max_length=500)
    external_url: str | None = Field(default=None, max_length=2000)
    business_entity_ids: tuple[str, ...] = ()
    project_ids: tuple[str, ...] = ()
    resource_ids: tuple[str, ...] = ()
    classification: DataClassification = DataClassification.INTERNAL
    source_updated_at: float | None = None
    source_sequence: int | None = Field(default=None, ge=0)
    source_revision: str | None = Field(default=None, max_length=500)
    synced_at: float | None = None
    retention_policy_ref: str | None = Field(default=None, max_length=500)
    retention_expires_at: float | None = None

    @model_validator(mode="after")
    def normalize_links(self) -> "ExternalRecordRefCreate":
        self.business_entity_ids = tuple(dict.fromkeys(self.business_entity_ids))
        self.project_ids = tuple(dict.fromkeys(self.project_ids))
        self.resource_ids = tuple(dict.fromkeys(self.resource_ids))
        return self


class ExternalRecordRef(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"external-record-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    system: str
    provider: str
    provider_instance: str | None = None
    object_type: str
    external_id: str
    display_name: str | None = None
    external_url: str | None = None
    business_entity_ids: tuple[str, ...] = ()
    project_ids: tuple[str, ...] = ()
    resource_ids: tuple[str, ...] = ()
    classification: DataClassification
    lifecycle: ExternalRecordLifecycle = ExternalRecordLifecycle.ACTIVE
    source_updated_at: float | None = None
    source_sequence: int | None = Field(default=None, ge=0)
    source_revision: str | None = None
    first_seen_at: float = Field(default_factory=time.time)
    synced_at: float = Field(default_factory=time.time)
    revoked_at: float | None = None
    governance_record_id: str | None = None
    created_by: str = Field(min_length=1)

    def stable_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.organization_id,
            self.workspace_id,
            self.system.casefold(),
            self.object_type.casefold(),
            self.external_id,
        )


class ExternalRecordRefUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str | None = Field(default=None, max_length=500)
    external_url: str | None = Field(default=None, max_length=2000)
    business_entity_ids: tuple[str, ...] | None = None
    project_ids: tuple[str, ...] | None = None
    resource_ids: tuple[str, ...] | None = None
    lifecycle: ExternalRecordLifecycle | None = None
    source_updated_at: float | None = None
    source_sequence: int | None = Field(default=None, ge=0)
    source_revision: str | None = Field(default=None, max_length=500)
    synced_at: float | None = None


class CompanyFactSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=300)
    provider: str | None = Field(default=None, max_length=200)
    external_record_ref_id: str | None = Field(default=None, max_length=500)
    authority: FactSourceAuthority = FactSourceAuthority.OBSERVED
    priority: int = Field(default=0, ge=0, le=1000)
    source_revision: str | None = Field(default=None, max_length=500)
    source_observed_at: float | None = None


class CompanyFactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    business_entity_id: str = Field(min_length=1)
    key: str = Field(min_length=1, max_length=300)
    value_type: FactValueType
    value: FactScalar
    unit: str | None = Field(default=None, max_length=100)
    source: CompanyFactSource
    quality: FactQuality = FactQuality.NORMAL
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    effective_from: float | None = None
    effective_to: float | None = None
    observed_at: float | None = None
    freshness_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    classification: DataClassification = DataClassification.INTERNAL
    evidence_ids: tuple[str, ...] = ()
    retention_policy_ref: str | None = Field(default=None, max_length=500)
    retention_expires_at: float | None = None

    @model_validator(mode="after")
    def validate_fact(self) -> "CompanyFactCreate":
        if self.effective_from is not None and self.effective_to is not None:
            if self.effective_to < self.effective_from:
                raise ValueError("effective_to must be >= effective_from")
        if self.value_type == FactValueType.STRING:
            if type(self.value) is not str:
                raise ValueError("string fact requires string value")
            if len(self.value) > 8000:
                raise ValueError("string fact value exceeds 8000 characters")
        elif self.value_type == FactValueType.BOOLEAN:
            if type(self.value) is not bool:
                raise ValueError("boolean fact requires boolean value")
        elif self.value_type == FactValueType.INTEGER:
            if type(self.value) is not int:
                raise ValueError("integer fact requires integer value")
        elif self.value_type == FactValueType.NUMBER:
            if type(self.value) not in {int, float}:
                raise ValueError("number fact requires numeric value")
        self.evidence_ids = tuple(dict.fromkeys(self.evidence_ids))
        return self


class CompanyFact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"company-fact-{uuid.uuid4().hex}")
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    business_entity_id: str = Field(min_length=1)
    key: str = Field(min_length=1, max_length=300)
    value_type: FactValueType
    value: FactScalar | None
    unit: str | None = None
    source: CompanyFactSource
    quality: FactQuality
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    effective_from: float | None = None
    effective_to: float | None = None
    observed_at: float
    freshness_seconds: int | None = None
    classification: DataClassification
    evidence_ids: tuple[str, ...] = ()
    lifecycle: CompanyFactLifecycle = CompanyFactLifecycle.ACTIVE
    supersedes_fact_id: str | None = None
    superseded_by_fact_id: str | None = None
    governance_record_id: str | None = None
    created_by: str = Field(min_length=1)
    created_at: float = Field(default_factory=time.time)


class CompanyFactSupersede(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacement: CompanyFactCreate


class BusinessEntityRelationshipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_entity_id: str = Field(min_length=1)
    to_entity_id: str = Field(min_length=1)
    relationship_type: BusinessRelationshipType


class BusinessEntityRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"business-relation-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    from_entity_id: str
    to_entity_id: str
    relationship_type: BusinessRelationshipType
    created_by: str
    created_at: float = Field(default_factory=time.time)


class FactResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    business_entity_id: str
    key: str
    selected: CompanyFact | None = None
    freshness: FactFreshness
    conflict: bool = False
    conflict_fact_ids: tuple[str, ...] = ()
    stale_fact_ids: tuple[str, ...] = ()
    revoked_source_fact_ids: tuple[str, ...] = ()
    candidate_fact_ids: tuple[str, ...] = ()
    reason: str
    resolved_at: float = Field(default_factory=time.time)


class BusinessContextState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = BUSINESS_CONTEXT_CONTRACT.current
    entities: list[BusinessEntity] = Field(default_factory=list)
    external_records: list[ExternalRecordRef] = Field(default_factory=list)
    facts: list[CompanyFact] = Field(default_factory=list)
    relationships: list[BusinessEntityRelationship] = Field(default_factory=list)


def fact_value_fingerprint(value: FactScalar | None) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
