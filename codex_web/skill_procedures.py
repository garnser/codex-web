"""Code-owned, non-executable procedure schema over canonical agent.skill data.

These contracts describe requirements. They never issue grants or run operations.
Discovery/admission and execution consume them through their canonical boundaries.
"""
from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.agent_providers import AgentProviderCapability
from codex_web.compatibility import ContractSpec
from codex_web.authority import AuthorityApprovalRequirement, AuthorityEnvironment, AuthorityLevel
from codex_web.execution_workers import WorkerCapability
from codex_web.resources import ResourceType

PROCEDURE_CONTRACT = ContractSpec("skill-operational-procedure", "1.0", ("1.0",))
PROCEDURE_SCHEMA_VERSION = PROCEDURE_CONTRACT.current
PROCEDURE_MAX_BYTES = 64 * 1024
Identifier = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
Version = Annotated[str, Field(pattern=r"^\d{1,6}\.\d{1,6}\.\d{1,6}$")]


class ProcedureData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, allow_inf_nan=False)


class ProcedureValueContract(ProcedureData):
    name: Identifier
    type: Literal["string", "integer", "number", "boolean", "resource_reference", "evidence_reference", "secret_reference"]
    required: bool = True
    description: Text
    # No literals/defaults: credentials and operational facts are supplied by
    # canonical inputs, never embedded into reusable content.


class ProcedureResourceSelector(ProcedureData):
    id: Identifier
    resource_types: tuple[ResourceType, ...] = Field(min_length=1, max_length=8)
    resource_ids: tuple[Identifier, ...] = Field(default=(), max_length=32)
    tags: tuple[Identifier, ...] = Field(default=(), max_length=24)
    match: Literal["all"] = "all"


class ProcedureServiceVersion(ProcedureData):
    resource_selector: Identifier
    service: Identifier
    min_version: Version
    max_version: Version

    @model_validator(mode="after")
    def ordered(self):
        if tuple(map(int, self.min_version.split('.'))) > tuple(map(int, self.max_version.split('.'))):
            raise ValueError("service version interval is inverted")
        return self


class ProcedurePermission(ProcedureData):
    capability: Identifier
    level: AuthorityLevel
    resource_selector: Identifier
    approvals: AuthorityApprovalRequirement = Field(default_factory=AuthorityApprovalRequirement)


class ProcedureCondition(ProcedureData):
    # Conditions reference declared inputs or preceding step outcomes. No
    # expressions, eval, scripts, shell commands, URLs or network grants.
    source: Literal["input", "step"]
    name: Identifier
    predicate: Literal["present", "absent", "succeeded", "failed", "verification_failed", "reconciliation_required"]

    @model_validator(mode="after")
    def compatible(self):
        if (self.source == "input") != (self.predicate in {"present", "absent"}):
            raise ValueError("condition predicate is incompatible with its source")
        return self


class ProcedureAction(ProcedureData):
    provider_type: Identifier
    provider_instance: Annotated[str, Field(min_length=1, max_length=240, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
    action_id: Identifier
    provider_contract_version: Annotated[str, Field(pattern=r"^\d{1,3}\.\d{1,3}$")]
    resource_selector: Identifier
    # The registered ActionDefinition, binding and policy determine credentials,
    # authority, risk, network and process access, never these descriptions.


class ProcedureWorkerOperation(ProcedureData):
    capability: WorkerCapability
    artifact_reference: Identifier
    artifact_checksum: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    resource_selector: Identifier
    isolation: Literal["canonical_execution_worker"] = "canonical_execution_worker"


class ProcedureEvidence(ProcedureData):
    name: Identifier
    description: Text
    required: Literal[True] = True
    kind: Literal["artifact", "provider_receipt", "verification", "rollback_verification"]


class ProcedureFailure(ProcedureData):
    outcome: Literal["stop", "escalate", "conditional_recovery", "reconcile"]
    escalation_owner_identity_id: Identifier
    attention_reason: Text
    recovery_step_id: Identifier | None = None
    unknown_outcome: Literal["reconcile_before_retry"] = "reconcile_before_retry"

    @model_validator(mode="after")
    def recovery(self):
        if (self.outcome == "conditional_recovery") != (self.recovery_step_id is not None):
            raise ValueError("conditional recovery requires exactly one recovery step reference")
        return self


class ProcedureImpact(ProcedureData):
    resource_selector: Identifier
    change: Literal["rotation", "restart", "deployment", "configuration", "rollback"]
    description: Text
    max_unavailable_seconds: int = Field(ge=0, le=3600, strict=True)
    reversible: bool


class ProcedureStep(ProcedureData):
    id: Identifier
    kind: Literal["diagnostic", "mutation", "verification", "recovery"]
    description: Text
    inputs: tuple[ProcedureValueContract, ...] = Field(default=(), max_length=24)
    outputs: tuple[ProcedureValueContract, ...] = Field(min_length=1, max_length=24)
    preconditions: tuple[ProcedureCondition, ...] = Field(default=(), max_length=24)
    expected_evidence: tuple[ProcedureEvidence, ...] = Field(min_length=1, max_length=16)
    timeout_seconds: int = Field(ge=1, le=3600, strict=True)
    max_attempts: int = Field(ge=1, le=5, strict=True)
    retry_backoff_seconds: int = Field(ge=0, le=300, strict=True)
    failure: ProcedureFailure
    permissions: tuple[ProcedurePermission, ...] = Field(min_length=1, max_length=16)
    action: ProcedureAction | None = None
    worker: ProcedureWorkerOperation | None = None
    impact: ProcedureImpact | None = None
    verification_step_id: Identifier | None = None

    @model_validator(mode="after")
    def operation(self):
        if (self.action is None) == (self.worker is None):
            raise ValueError("step requires exactly one registered action or isolated worker operation")
        if self.kind in {"mutation", "recovery"} and self.action is None:
            raise ValueError("external mutation/recovery requires a registered ActionProvider operation")
        if self.kind in {"mutation", "recovery"} and (self.impact is None or self.verification_step_id is None):
            raise ValueError("mutation/recovery requires bounded impact and a verification step")
        if self.kind == "recovery" and not any(c.source == "step" and c.predicate in {"failed", "verification_failed"} for c in self.preconditions):
            raise ValueError("recovery must be conditional on a preceding failure")
        for values in (self.inputs, self.outputs, self.expected_evidence):
            names = [v.name for v in values]
            if len(names) != len(set(names)):
                raise ValueError("step contract/evidence names must be unique")
        return self


class OperationalProcedure(ProcedureData):
    schema_version: Literal["1.0"] = PROCEDURE_SCHEMA_VERSION
    owner_identity_id: Identifier
    task_triggers: tuple[Identifier, ...] = Field(min_length=1, max_length=32)
    operation_triggers: tuple[Identifier, ...] = Field(min_length=1, max_length=32)
    resource_selectors: tuple[ProcedureResourceSelector, ...] = Field(min_length=1, max_length=16)
    environments: tuple[AuthorityEnvironment, ...] = Field(min_length=1, max_length=4)
    service_versions: tuple[ProcedureServiceVersion, ...] = Field(default=(), max_length=16)
    inputs: tuple[ProcedureValueContract, ...] = Field(default=(), max_length=24)
    reviewed_at: float = Field(ge=0)
    review_due_at: float = Field(gt=0)
    expires_at: float = Field(gt=0)
    required_provider_capabilities: tuple[AgentProviderCapability, ...] = Field(default=(), max_length=16)
    required_worker_capabilities: tuple[WorkerCapability, ...] = Field(default=(), max_length=8)
    conflict_group: Identifier
    precedence: int = Field(ge=0, le=1000, strict=True)
    on_conflict: Literal["fail_closed"] = "fail_closed"
    max_duration_seconds: int = Field(ge=1, le=86400, strict=True)
    steps: tuple[ProcedureStep, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def bounded_contract(self):
        from codex_web.skills import _contains_secret_material

        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        if len(raw.encode()) > PROCEDURE_MAX_BYTES:
            raise ValueError("procedure exceeds bounded content size")
        if _contains_secret_material(raw) or re.search(r"\bglpat-[A-Za-z0-9_-]{16,}\b", raw) or re.search(r'(?i)(?:password|api[_ -]?key|access[_ -]?token|secret)\s*[=:]\s*\S+', raw):
            raise ValueError("procedure appears to contain raw secret material")
        # Descriptive prose is untrusted; executable fields are forbidden by the
        # schema. Also reject obvious code blocks/commands smuggled into prose.
        if re.search(r'```|#!|\b(?:eval|exec)\s*\(|\$\(|\b(?:sudo|bash|sh|python|node)\s+-[ce]\b', raw):
            raise ValueError("procedure cannot contain arbitrary executable code")
        if not self.reviewed_at < self.review_due_at <= self.expires_at:
            raise ValueError("procedure freshness dates must be ordered")
        selectors = {s.id for s in self.resource_selectors}
        if len(selectors) != len(self.resource_selectors):
            raise ValueError("resource selector IDs must be unique")
        inputs = {i.name: i for i in self.inputs}
        if len(inputs) != len(self.inputs):
            raise ValueError("procedure input names must be unique")
        seen: dict[str, ProcedureStep] = {}
        ids = {s.id for s in self.steps}
        if len(ids) != len(self.steps):
            raise ValueError("step IDs must be unique")
        worst_case = sum(s.max_attempts * s.timeout_seconds + (s.max_attempts - 1) * s.retry_backoff_seconds for s in self.steps)
        if worst_case > self.max_duration_seconds:
            raise ValueError("step timeout/retry bounds exceed procedure duration budget")
        for version in self.service_versions:
            if version.resource_selector not in selectors:
                raise ValueError("service version references unknown resource selector")
        for step in self.steps:
            for condition in step.preconditions:
                if condition.source == "input" and condition.name not in inputs:
                    raise ValueError("precondition references an undeclared procedure input")
                if condition.source == "step" and condition.name not in seen:
                    raise ValueError("step conditions must reference preceding steps; cycles are forbidden")
            for permission in step.permissions:
                if permission.resource_selector not in selectors:
                    raise ValueError("permission references unknown resource selector")
            if step.impact and step.impact.resource_selector not in selectors:
                raise ValueError("impact references unknown resource selector")
            if step.verification_step_id is not None:
                verification = next((s for s in self.steps if s.id == step.verification_step_id), None)
                if verification is None or verification.kind != "verification" or verification.id in seen or verification.id == step.id:
                    raise ValueError("verification must reference a later verification step")
                if not any(c.source == "step" and c.name == step.id and c.predicate == "succeeded" for c in verification.preconditions):
                    raise ValueError("verification must be conditional on the referring step success")
            if step.action and step.action.resource_selector not in selectors:
                raise ValueError("action references unknown resource selector")
            if step.worker and step.worker.resource_selector not in selectors:
                raise ValueError("worker references unknown resource selector")
            if step.worker and step.worker.capability not in self.required_worker_capabilities:
                raise ValueError("worker operation capability must be declared as required")
            target = step.failure.recovery_step_id
            if target is not None:
                recovery = next((s for s in self.steps if s.id == target), None)
                if recovery is None or recovery.kind != "recovery" or target in seen or target == step.id:
                    raise ValueError("failure recovery must reference a later conditional recovery step")
                if not any(c.source == "step" and c.name == step.id and c.predicate in {"failed", "verification_failed"} for c in recovery.preconditions):
                    raise ValueError("recovery must be conditional on the referring step failure")
            seen[step.id] = step
        if any(s.kind in {"mutation", "recovery"} for s in self.steps) and not any(s.kind == "verification" for s in self.steps):
            raise ValueError("mutating procedures require explicit verification")
        return self
