"""Versioned checkpoint facet of a canonical ExecutionAssignment (no new job engine)."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator
from codex_web.definitions import DefinitionReference
from codex_web.authority import AuthorityDecision


class ProcedureStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: DefinitionReference
    input_snapshot_id: str = Field(min_length=1, max_length=200)

class ProcedureAdvance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str | None = Field(default=None, min_length=1, max_length=100)


class ProcedureStepCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "running", "awaiting_approval", "waiting", "reconciliation_required", "succeeded", "failed", "verification_failed", "skipped"] = "pending"
    attempts: int = Field(default=0, ge=0, le=5)
    action_intent_id: str | None = None
    approval_id: str | None = None
    rollback_origin_receipt_id: str | None = None
    not_before: float | None = None
    started_at: float | None = None
    # A bounded name-to-Artifact reference index. Values live in governed
    # managed content so retention/deletion cannot leave raw checkpoint copies.
    outputs: dict[str, str] = Field(default_factory=dict)
    output_snapshot_id: str | None = None
    output_snapshot_checksum: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    evidence: dict[str, str] = Field(default_factory=dict)
    invocation_ids: tuple[str, ...] = ()
    authority_decisions: tuple[AuthorityDecision, ...] = Field(default=(), max_length=16)
    failure_code: str | None = None


class ProcedureDeviation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    step_id: str
    reason: str = Field(min_length=1, max_length=2000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    authority_definition: DefinitionReference
    actor_identity_id: str


class ProcedureCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0", "1.1"] = "1.0"
    id: str | None = Field(default=None, pattern=r"^procedure-run-[a-f0-9]{32}$")
    canonical_input_checksum: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    revision: int = Field(default=1, ge=1)
    definition: DefinitionReference
    actor_identity_id: str
    input_snapshot_id: str
    input_snapshot_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    resource_context_checksum: str
    started_at: float
    deadline_at: float
    status: Literal["ready", "running", "waiting", "awaiting_approval", "reconciliation_required", "recovery_required", "recovered", "completed"] = "ready"
    current_step_id: str | None = None
    steps: dict[str, ProcedureStepCheckpoint]
    deviations: tuple[ProcedureDeviation, ...] = Field(default=(), max_length=32)
    attention_id: str | None = None
    failure_code: str | None = None
    recovery_step_id: str | None = None

    @model_validator(mode="after")
    def run_identity(self):
        if (self.schema_version == "1.1") != (self.id is not None and self.canonical_input_checksum is not None):
            raise ValueError("procedure run identity requires schema 1.1")
        if self.schema_version == "1.0" and (self.id is not None or self.canonical_input_checksum is not None):
            raise ValueError("legacy checkpoints cannot acquire a new origin identity")
        return self

    @model_serializer(mode="wrap")
    def preserve_legacy(self, handler):
        data = handler(self)
        if self.schema_version == "1.0":
            data.pop("id", None)
            data.pop("canonical_input_checksum", None)
        return data
