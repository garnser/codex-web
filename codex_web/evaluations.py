from __future__ import annotations

import hashlib
import json
import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.definitions import DefinitionReference


EVALUATION_STATE_CONTRACT = ContractSpec("evaluation-state", "1.0", ("1.0",))


class EvaluationCITier(StrEnum):
    SMOKE = "smoke"
    RELEVANT = "relevant"
    RELEASE = "release"


class EvaluationFailureInjection(StrEnum):
    PROVIDER_TIMEOUT = "provider_timeout"
    MODEL_OUTAGE = "model_outage"
    STALE_EVENT = "stale_event"
    DUPLICATE_EVENT = "duplicate_event"
    UNKNOWN_ACTION_RESULT = "unknown_action_result"
    REVOKED_SECRET = "revoked_secret"
    EXPIRED_LEASE = "expired_lease"
    MISSING_DEFINITION = "missing_definition"
    INCOMPATIBLE_DEFINITION = "incompatible_definition"
    DEPENDENCY_FAILURE = "dependency_failure"


class EvaluationTerminalOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"
    UNKNOWN = "unknown"


class EvaluationRunMode(StrEnum):
    HISTORICAL = "historical"
    CANDIDATE = "candidate"
    FAILURE_INJECTION = "failure_injection"


class EvaluationAssertionKind(StrEnum):
    TERMINAL_OUTCOME = "terminal_outcome"
    STATE_TRANSITION = "state_transition"
    SELECTED_ROLES = "selected_roles"
    DEFINITION_SET = "definition_set"
    ACTION_COUNT = "action_count"
    ALLOWED_ACTIONS = "allowed_actions"
    FORBIDDEN_ACTIONS = "forbidden_actions"
    EVIDENCE = "evidence"
    RETRIES = "retries"
    LATENCY = "latency"
    TOKENS = "tokens"
    COST = "cost"
    NO_LLM = "no_llm"
    POLICY_VIOLATIONS = "policy_violations"


class EvaluationStateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    object_refs: tuple[str, ...] = ()
    fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    description: str | None = None


class EvaluationEventFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    source: str = Field(min_length=1)
    occurred_at: float
    correlation_id: str | None = None
    causation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_sensitive_payload_keys(self) -> "EvaluationEventFixture":
        sensitive = {
            "password",
            "passwd",
            "secret",
            "token",
            "api_key",
            "apikey",
            "authorization",
            "credential",
            "private_key",
        }

        def walk(value: Any, path: str = "payload") -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    normalized = str(key).strip().lower()
                    if normalized in sensitive or any(
                        fragment in normalized
                        for fragment in ("password", "secret", "private_key")
                    ):
                        raise ValueError(
                            f"evaluation fixtures cannot persist sensitive key {path}.{key}"
                        )
                    walk(item, f"{path}.{key}")
            elif isinstance(value, (list, tuple)):
                for index, item in enumerate(value):
                    walk(item, f"{path}[{index}]")

        walk(self.payload)
        return self


class EvaluationRuntimePin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    agent_provider_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    runtime_type: str = Field(min_length=1)
    capability_revision: int = Field(ge=1)
    runtime_version: str | None = None


class EvaluationModelPin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_version: str | None = None
    prompt_template_id: str = Field(min_length=1)
    prompt_template_version: str = Field(min_length=1)
    prompt_template_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvaluationBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_model_calls: int = Field(default=5, ge=0, le=100)
    max_input_tokens: int = Field(default=50000, ge=0)
    max_output_tokens: int = Field(default=12000, ge=0)
    max_cost_usd: float = Field(default=10.0, ge=0.0)
    max_actions: int = Field(default=8, ge=0, le=1000)
    max_retries: int = Field(default=3, ge=0, le=100)
    max_latency_seconds: float = Field(default=300.0, ge=0.0)


class EvaluationActionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    action_kind: str = Field(min_length=1)
    provider_id: str | None = None
    binding_id: str | None = None
    action_intent_id: str | None = None
    outcome: str = Field(min_length=1)


class EvaluationTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    terminal_outcome: EvaluationTerminalOutcome
    state_transitions: tuple[str, ...] = ()
    selected_role_ids: tuple[str, ...] = ()
    resolved_definitions: tuple[DefinitionReference, ...] = ()
    runtime: EvaluationRuntimePin | None = None
    models: tuple[EvaluationModelPin, ...] = ()
    retrieved_context_refs: tuple[str, ...] = ()
    actions: tuple[EvaluationActionRecord, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    policy_violations: tuple[str, ...] = ()
    intervention_count: int = Field(default=0, ge=0)
    reasoning_calls: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    latency_seconds: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def normalize(self) -> "EvaluationTrace":
        for field_name in (
            "state_transitions",
            "selected_role_ids",
            "retrieved_context_refs",
            "evidence_ids",
            "policy_violations",
        ):
            object.__setattr__(
                self,
                field_name,
                tuple(dict.fromkeys(getattr(self, field_name))),
            )
        return self

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class EvaluationExpectedInvariants(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    terminal_outcomes: tuple[EvaluationTerminalOutcome, ...] = (
        EvaluationTerminalOutcome.SUCCEEDED,
    )
    required_state_transitions: tuple[str, ...] = ()
    expected_selected_role_ids: tuple[str, ...] = ()
    required_evidence_ids: tuple[str, ...] = ()
    allowed_action_kinds: tuple[str, ...] = ()
    forbidden_action_kinds: tuple[str, ...] = ()
    require_no_llm: bool = False
    forbid_policy_violations: bool = True
    max_interventions: int | None = Field(default=None, ge=0)


class EvaluationRegressionThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_token_increase_ratio: float = Field(default=0.25, ge=0.0)
    max_cost_increase_ratio: float = Field(default=0.25, ge=0.0)
    max_latency_increase_ratio: float = Field(default=0.50, ge=0.0)
    max_action_increase: int = Field(default=0, ge=0)
    max_intervention_increase: int = Field(default=0, ge=0)
    allow_definition_resolution_changes: bool = False


class EvaluationReplayFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    mode: EvaluationRunMode
    definition_overrides: tuple[DefinitionReference, ...] = ()
    failure_injection: EvaluationFailureInjection | None = None
    trace: EvaluationTrace

    @model_validator(mode="after")
    def validate_mode(self) -> "EvaluationReplayFixture":
        if self.mode == EvaluationRunMode.FAILURE_INJECTION and self.failure_injection is None:
            raise ValueError("failure replay fixture requires failure_injection")
        if self.mode != EvaluationRunMode.FAILURE_INJECTION and self.failure_injection is not None:
            raise ValueError("failure_injection only applies to failure replay fixtures")
        return self


class EvaluationScenarioCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scenario_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    suite_ids: tuple[str, ...] = ()
    ci_tier: EvaluationCITier = EvaluationCITier.RELEVANT
    project_id: str | None = None
    starting_state: EvaluationStateSnapshot
    events: tuple[EvaluationEventFixture, ...]
    historical_definitions: tuple[DefinitionReference, ...] = ()
    runtime: EvaluationRuntimePin | None = None
    models: tuple[EvaluationModelPin, ...] = ()
    retrieved_context_refs: tuple[str, ...] = ()
    expected: EvaluationExpectedInvariants = Field(
        default_factory=EvaluationExpectedInvariants
    )
    budget: EvaluationBudget = Field(default_factory=EvaluationBudget)
    regression_thresholds: EvaluationRegressionThresholds = Field(
        default_factory=EvaluationRegressionThresholds
    )
    replay_fixtures: tuple[EvaluationReplayFixture, ...]
    allowed_failure_injections: tuple[EvaluationFailureInjection, ...] = ()

    @model_validator(mode="after")
    def normalize(self) -> "EvaluationScenarioCreate":
        self.suite_ids = tuple(dict.fromkeys(item for item in self.suite_ids if item))
        self.retrieved_context_refs = tuple(
            dict.fromkeys(item for item in self.retrieved_context_refs if item)
        )
        self.allowed_failure_injections = tuple(
            dict.fromkeys(self.allowed_failure_injections)
        )
        fixture_ids = [item.id for item in self.replay_fixtures]
        if len(fixture_ids) != len(set(fixture_ids)):
            raise ValueError("evaluation replay fixture ids must be unique")
        if not any(item.mode == EvaluationRunMode.HISTORICAL for item in self.replay_fixtures):
            raise ValueError("evaluation scenario requires a historical replay fixture")
        injections = {
            item.failure_injection
            for item in self.replay_fixtures
            if item.failure_injection is not None
        }
        missing = set(self.allowed_failure_injections) - injections
        if missing:
            raise ValueError(
                "allowed failure injections require recorded replay fixtures: "
                + ", ".join(sorted(item.value for item in missing))
            )
        return self


class EvaluationScenario(EvaluationScenarioCreate):
    model_config = ConfigDict(extra="forbid")

    organization_id: str
    workspace_id: str
    created_by: str
    created_at: float = Field(default_factory=time.time)
    fixture_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @staticmethod
    def checksum(payload: EvaluationScenarioCreate) -> str:
        encoded = json.dumps(
            payload.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class EvaluationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scenario_id: str = Field(min_length=1)
    scenario_version: str = Field(min_length=1)
    replay_fixture_id: str = Field(min_length=1)
    baseline_run_id: str | None = None


class EvaluationAssertionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EvaluationAssertionKind
    passed: bool
    expected: str
    actual: str
    detail: str | None = None


class EvaluationRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"evaluation-run-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    scenario_id: str
    scenario_version: str
    suite_ids: tuple[str, ...] = ()
    ci_tier: EvaluationCITier
    replay_fixture_id: str
    mode: EvaluationRunMode
    failure_injection: EvaluationFailureInjection | None = None
    fixture_checksum_sha256: str
    starting_state: EvaluationStateSnapshot
    event_ids: tuple[str, ...]
    definitions: tuple[DefinitionReference, ...] = ()
    runtime: EvaluationRuntimePin | None = None
    models: tuple[EvaluationModelPin, ...] = ()
    trace: EvaluationTrace
    assertions: tuple[EvaluationAssertionResult, ...] = ()
    passed: bool
    evidence_id: str | None = None
    baseline_run_id: str | None = None
    executed_by: str
    started_at: float = Field(default_factory=time.time)
    completed_at: float = Field(default_factory=time.time)


class EvaluationComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"evaluation-comparison-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    scenario_id: str
    scenario_version: str
    baseline_run_id: str
    candidate_run_id: str
    success_changed: bool
    token_delta: int
    cost_delta_usd: float
    latency_delta_seconds: float
    action_delta: int
    intervention_delta: int
    policy_violation_delta: int
    definition_resolution_changed: bool
    regression_reasons: tuple[str, ...] = ()
    passed: bool
    created_at: float = Field(default_factory=time.time)


class EvaluationSuiteRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"evaluation-suite-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    suite_id: str
    run_ids: tuple[str, ...]
    comparison_ids: tuple[str, ...] = ()
    passed: bool
    evidence_ids: tuple[str, ...] = ()
    executed_by: str
    created_at: float = Field(default_factory=time.time)


class EvaluationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = EVALUATION_STATE_CONTRACT.current
    scenarios: list[EvaluationScenario] = Field(default_factory=list)
    runs: list[EvaluationRun] = Field(default_factory=list)
    comparisons: list[EvaluationComparison] = Field(default_factory=list)
    suite_runs: list[EvaluationSuiteRun] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        EVALUATION_STATE_CONTRACT.require(self.schema_version)
