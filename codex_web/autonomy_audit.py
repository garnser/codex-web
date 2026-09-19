from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.autonomy import AutonomyCycleOutcome
from codex_web.compatibility import ContractSpec


AUTONOMY_AUDIT_CONTRACT = ContractSpec("autonomy-audit-state", "1.0", ("1.0",))
AUDIT_HASH_ALGORITHM = "sha256-chain-v1"
CHECKPOINT_HASH_ALGORITHM = "sha256-checkpoint-v1"
GENESIS_HASH = "0" * 64


class AutonomyAuditKind(StrEnum):
    CYCLE = "cycle"
    REDACTION = "redaction"
    CHECKPOINT = "checkpoint"
    INTEGRITY_VERIFICATION = "integrity_verification"
    AUTO_SUSPENSION = "auto_suspension"


class AuditIntegrityStatus(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    EMPTY = "empty"


class AutonomySafetySignalKind(StrEnum):
    AUDIT_INTEGRITY = "audit_integrity"
    RELIABILITY = "reliability"
    EVALUATION_REGRESSION = "evaluation_regression"
    SLO_ERROR_BUDGET = "slo_error_budget"
    CRITICAL_INCIDENT = "critical_incident"
    RELEASE_READINESS = "release_readiness"
    RECOVERY_READINESS = "recovery_readiness"


class AutonomyReliabilityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_samples: int = Field(default=5, ge=1, le=100000)
    max_failure_rate: float = Field(default=0.40, ge=0.0, le=1.0)
    max_consecutive_failures: int = Field(default=3, ge=1, le=1000)
    max_human_intervention_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens_per_success: float | None = Field(default=None, gt=0.0)
    max_cost_usd_per_success: float | None = Field(default=None, gt=0.0)
    auto_suspend: bool = False
    suspension_signal_kinds: tuple[AutonomySafetySignalKind, ...] = (
        AutonomySafetySignalKind.AUDIT_INTEGRITY,
        AutonomySafetySignalKind.EVALUATION_REGRESSION,
        AutonomySafetySignalKind.SLO_ERROR_BUDGET,
        AutonomySafetySignalKind.CRITICAL_INCIDENT,
    )

    @model_validator(mode="after")
    def normalize(self) -> "AutonomyReliabilityPolicy":
        object.__setattr__(
            self,
            "suspension_signal_kinds",
            tuple(dict.fromkeys(self.suspension_signal_kinds)),
        )
        return self


class AutonomyAuditPayload(BaseModel):
    """Content-minimized metadata used to derive one immutable audit row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AutonomyAuditKind
    organization_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    project_id: str | None = None
    occurred_at: float = Field(default_factory=time.time)

    cycle_id: str | None = None
    event_id: str | None = None
    event_type: str | None = None
    source: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None

    actor_identity_id: str | None = None
    actor_principal_kind: str | None = None
    actor_assurance: str | None = None

    autonomy_level: str | None = None
    policy_fingerprint: str | None = None
    break_glass_grant_id: str | None = None
    approval_request_ids: tuple[str, ...] = ()

    outcome: str | None = None
    reason_code: str | None = None
    reasoning_invoked: bool = False
    reasoning_attempts: int = 0

    model_provider_id: str | None = None
    model_id: str | None = None
    model_revision: str | None = None
    prompt_template_id: str | None = None
    model_routing_reason: str | None = None
    model_invocation_ids: tuple[str, ...] = ()

    action_intent_ids: tuple[str, ...] = ()
    resource_ids: tuple[str, ...] = ()
    goal_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()
    provider_receipt_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    verification_ids: tuple[str, ...] = ()

    model_tokens: int = Field(default=0, ge=0)
    model_cost_usd: float = Field(default=0.0, ge=0.0)
    monetary_impact_usd: float = Field(default=0.0, ge=0.0)
    cloud_spend_usd: float = Field(default=0.0, ge=0.0)
    production_changes: int = Field(default=0, ge=0)

    target_audit_record_id: str | None = None
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize(self) -> "AutonomyAuditPayload":
        for name in (
            "approval_request_ids",
            "model_invocation_ids",
            "action_intent_ids",
            "resource_ids",
            "goal_ids",
            "decision_ids",
            "provider_receipt_ids",
            "evidence_ids",
            "verification_ids",
        ):
            object.__setattr__(
                self,
                name,
                tuple(dict.fromkeys(str(item).strip() for item in getattr(self, name) if str(item).strip())),
            )
        bounded: dict[str, str | int | float | bool | None] = {}
        for key, value in list(self.details.items())[:32]:
            normalized_key = str(key)[:120]
            if isinstance(value, str):
                bounded[normalized_key] = value[:500]
            elif isinstance(value, (int, float, bool)) or value is None:
                bounded[normalized_key] = value
            else:
                bounded[normalized_key] = str(value)[:500]
        object.__setattr__(self, "details", bounded)
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


class AutonomyAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"autonomy-audit-{uuid.uuid4().hex}")
    partition_id: str
    sequence: int = Field(ge=1)
    previous_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    hash_algorithm: str = AUDIT_HASH_ALGORITHM
    payload: AutonomyAuditPayload
    created_at: float = Field(default_factory=time.time)

    @staticmethod
    def partition_for(organization_id: str, workspace_id: str) -> str:
        return hashlib.sha256(
            f"{organization_id}\x00{workspace_id}".encode("utf-8")
        ).hexdigest()[:32]

    @classmethod
    def build(
        cls,
        payload: AutonomyAuditPayload,
        *,
        sequence: int,
        previous_hash: str,
        created_at: float | None = None,
        record_id: str | None = None,
    ) -> "AutonomyAuditRecord":
        partition_id = cls.partition_for(
            payload.organization_id,
            payload.workspace_id,
        )
        payload_hash = hashlib.sha256(payload.canonical_bytes()).hexdigest()
        digest_input = (
            f"{AUDIT_HASH_ALGORITHM}\x00{partition_id}\x00{sequence}\x00"
            f"{previous_hash}\x00{payload_hash}"
        ).encode("utf-8")
        record_hash = hashlib.sha256(digest_input).hexdigest()
        return cls(
            id=record_id or f"autonomy-audit-{uuid.uuid4().hex}",
            partition_id=partition_id,
            sequence=sequence,
            previous_hash=previous_hash,
            payload_hash=payload_hash,
            record_hash=record_hash,
            payload=payload,
            created_at=time.time() if created_at is None else float(created_at),
        )


class AutonomyAuditCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"autonomy-checkpoint-{uuid.uuid4().hex}")
    partition_id: str
    sequence: int = Field(ge=0)
    root_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_checkpoint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    hash_algorithm: str = CHECKPOINT_HASH_ALGORITHM
    signature_algorithm: str | None = None
    signature: str | None = None
    signing_key_ref: str | None = None
    export_refs: tuple[str, ...] = ()
    created_at: float = Field(default_factory=time.time)

    @classmethod
    def unsigned(
        cls,
        *,
        partition_id: str,
        sequence: int,
        root_hash: str,
        previous_checkpoint_hash: str,
        created_at: float | None = None,
    ) -> "AutonomyAuditCheckpoint":
        timestamp = time.time() if created_at is None else float(created_at)
        checkpoint_hash = hashlib.sha256(
            (
                f"{CHECKPOINT_HASH_ALGORITHM}\x00{partition_id}\x00{sequence}\x00"
                f"{root_hash}\x00{previous_checkpoint_hash}\x00{timestamp:.6f}"
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            partition_id=partition_id,
            sequence=sequence,
            root_hash=root_hash,
            previous_checkpoint_hash=previous_checkpoint_hash,
            checkpoint_hash=checkpoint_hash,
            created_at=timestamp,
        )

    def signing_payload(self) -> bytes:
        return (
            f"{self.hash_algorithm}\x00{self.partition_id}\x00{self.sequence}\x00"
            f"{self.root_hash}\x00{self.previous_checkpoint_hash}\x00"
            f"{self.checkpoint_hash}"
        ).encode("utf-8")


class AuditSignature(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: str
    key_ref: str
    signature: str


@runtime_checkable
class AuditCheckpointSigner(Protocol):
    def sign(self, payload: bytes) -> AuditSignature: ...
    def verify(self, payload: bytes, signature: AuditSignature) -> bool: ...


@runtime_checkable
class AuditCheckpointExporter(Protocol):
    def export(self, checkpoint: AutonomyAuditCheckpoint) -> str: ...


class AuditIntegrityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AuditIntegrityStatus
    partition_id: str
    records_checked: int = Field(ge=0)
    first_sequence: int | None = None
    last_sequence: int | None = None
    root_hash: str = GENESIS_HASH
    checkpoint_id: str | None = None
    failed_record_id: str | None = None
    reason: str | None = None
    verified_at: float = Field(default_factory=time.time)


class AutonomyAuditMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_count: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    human_interventions: int = Field(ge=0)
    rollbacks: int = Field(ge=0)
    recoveries: int = Field(ge=0)
    incidents: int = Field(ge=0)
    total_model_tokens: int = Field(ge=0)
    total_model_cost_usd: float = Field(ge=0.0)
    failure_rate: float = Field(ge=0.0, le=1.0)
    human_intervention_rate: float = Field(ge=0.0, le=1.0)
    tokens_per_success: float | None = None
    cost_usd_per_success: float | None = None
    consecutive_failures: int = Field(ge=0)
    successful_outcomes: int = Field(ge=0)
    integrity_status: AuditIntegrityStatus
    should_suspend: bool = False
    suspension_reasons: tuple[str, ...] = ()


class AutonomySafetySignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"autonomy-signal-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    kind: AutonomySafetySignalKind
    source: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=4000)
    evidence_ids: tuple[str, ...] = ()
    active: bool = True
    observed_at: float = Field(default_factory=time.time)
    cleared_at: float | None = None


class AutonomySafetySignalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: AutonomySafetySignalKind
    source: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=4000)
    evidence_ids: tuple[str, ...] = ()


class AutonomyAuditState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = AUTONOMY_AUDIT_CONTRACT.current
    records: list[AutonomyAuditRecord] = Field(default_factory=list)
    checkpoints: list[AutonomyAuditCheckpoint] = Field(default_factory=list)
    signals: list[AutonomySafetySignal] = Field(default_factory=list)
    reliability_policy: AutonomyReliabilityPolicy = Field(
        default_factory=AutonomyReliabilityPolicy
    )

    def model_post_init(self, __context: Any) -> None:
        AUTONOMY_AUDIT_CONTRACT.require(self.schema_version)
