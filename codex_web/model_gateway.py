from __future__ import annotations

import hashlib
import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.compatibility import ContractSpec
from codex_web.input_plugins import InputGatedProposal, InputPluginProvenance


MODEL_GATEWAY_CONTRACT = ContractSpec(
    "model-gateway-state",
    "1.1",
    ("1.0", "1.1"),
    deprecated=("1.0",),
)

MODEL_CLASS_LIGHTWEIGHT = "lightweight"
MODEL_CLASS_PRIMARY_CODING = "primary-coding"
MODEL_CLASS_HIGH_REASONING = "high-reasoning"
MODEL_CLASS_STRATEGIC = "strategic"


class ModelProviderStatus(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class ModelLifecycle(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class ModelLatencyClass(StrEnum):
    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"


class ModelMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1)
    content: str


class ModelProviderUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    adapter_type: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    base_url: str | None = None
    credential_ref: str | None = None
    credential_required: bool = True
    residency_tags: tuple[str, ...] = ()
    compliance_tags: tuple[str, ...] = ()
    status: ModelProviderStatus = ModelProviderStatus.ACTIVE


class ModelProviderRecord(ModelProviderUpsert):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization_id: str
    workspace_id: str
    updated_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def normalize(self) -> "ModelProviderRecord":
        self.residency_tags = tuple(sorted(set(self.residency_tags)))
        self.compliance_tags = tuple(sorted(set(self.compliance_tags)))
        return self


class ModelDefinitionUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    concrete_model: str = Field(min_length=1)
    model_version: str | None = None
    model_classes: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ("text",)
    modalities: tuple[str, ...] = ("text",)
    supports_tools: bool = False
    context_window_tokens: int = Field(default=128000, ge=1)
    max_output_tokens: int = Field(default=8192, ge=1)
    latency_class: ModelLatencyClass = ModelLatencyClass.STANDARD
    input_price_per_million_usd: float | None = Field(default=None, ge=0.0)
    output_price_per_million_usd: float | None = Field(default=None, ge=0.0)
    residency_tags: tuple[str, ...] = ()
    compliance_tags: tuple[str, ...] = ()
    route_priority: int = Field(default=100, ge=0)
    lifecycle: ModelLifecycle = ModelLifecycle.ACTIVE


class ModelDefinitionRecord(ModelDefinitionUpsert):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    organization_id: str
    workspace_id: str
    updated_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def normalize(self) -> "ModelDefinitionRecord":
        self.model_classes = tuple(dict.fromkeys(self.model_classes))
        self.capabilities = tuple(sorted(set(self.capabilities)))
        self.modalities = tuple(sorted(set(self.modalities)))
        self.residency_tags = tuple(sorted(set(self.residency_tags)))
        self.compliance_tags = tuple(sorted(set(self.compliance_tags)))
        return self


class PromptTemplateUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    content: str = Field(min_length=1)
    active: bool = True


class PromptTemplateRecord(PromptTemplateUpsert):
    model_config = ConfigDict(extra="forbid")

    organization_id: str
    workspace_id: str
    checksum_sha256: str
    updated_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @staticmethod
    def checksum(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


class TenantModelPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_provider_ids: tuple[str, ...] = ()
    allowed_model_ids: tuple[str, ...] = ()
    required_residency_tags: tuple[str, ...] = ()
    required_compliance_tags: tuple[str, ...] = ()
    max_invocation_cost_usd: float | None = Field(default=None, gt=0.0)
    max_attempts: int = Field(default=2, ge=1, le=10)


class TenantModelPolicy(TenantModelPolicyUpdate):
    model_config = ConfigDict(extra="forbid")

    organization_id: str
    workspace_id: str
    updated_by: str
    updated_at: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def normalize(self) -> "TenantModelPolicy":
        self.allowed_provider_ids = tuple(sorted(set(self.allowed_provider_ids)))
        self.allowed_model_ids = tuple(sorted(set(self.allowed_model_ids)))
        self.required_residency_tags = tuple(sorted(set(self.required_residency_tags)))
        self.required_compliance_tags = tuple(sorted(set(self.required_compliance_tags)))
        return self


class ModelInvocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_class: str = Field(min_length=1)
    messages: tuple[ModelMessage, ...]
    system_prompt: str = ""
    prompt_template_id: str = Field(default="generic.system", min_length=1)
    prompt_template_version: str | None = None
    required_capabilities: tuple[str, ...] = ("text",)
    required_residency_tags: tuple[str, ...] = ()
    required_compliance_tags: tuple[str, ...] = ()
    preferred_provider_ids: tuple[str, ...] = ()
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int = Field(default=2048, ge=1)
    timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)
    max_cost_usd: float | None = Field(default=None, gt=0.0)
    allow_fallback: bool = True
    reasoning_effort: str | None = None
    text_verbosity: str | None = None
    work_item_ref: str | None = None
    goal_id: str | None = None
    decision_id: str | None = None
    execution_id: str | None = None
    purpose: str = Field(default="general", min_length=1)


class ModelRouteCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    model_id: str
    concrete_model: str
    model_version: str | None = None
    estimated_input_tokens: int
    max_output_tokens: int
    estimated_upper_cost_usd: float | None = None
    routing_reason: str


class ModelRouteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_class: str
    prompt_template_id: str
    prompt_template_version: str
    prompt_template_checksum_sha256: str
    candidates: tuple[ModelRouteCandidate, ...]
    policy_max_attempts: int
    policy_fingerprint_sha256: str
    effective_required_residency_tags: tuple[str, ...] = ()
    effective_required_compliance_tags: tuple[str, ...] = ()
    effective_max_cost_usd: float | None = None


class ModelProviderUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelProviderResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    usage: ModelProviderUsage = Field(default_factory=ModelProviderUsage)
    provider_request_id: str | None = None
    stop_reason: str | None = None


class ModelInvocationAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    model_id: str
    concrete_model: str
    model_version: str | None = None
    outcome: str
    error_code: str | None = None
    estimated_upper_cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    actual_cost_usd: float | None = None
    provider_request_id: str | None = None
    provider_stop_reason: str | None = None
    started_at: float
    completed_at: float


class ModelInvocationRecord(BaseModel):
    """Metadata-only reasoning attribution; prompt/message content is never stored."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"model-invocation-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    actor_id: str
    model_class: str
    purpose: str
    prompt_template_id: str
    prompt_template_version: str
    prompt_template_checksum_sha256: str
    rendered_prompt_sha256: str
    message_count: int
    input_character_count: int
    required_capabilities: tuple[str, ...] = ()
    required_residency_tags: tuple[str, ...] = ()
    required_compliance_tags: tuple[str, ...] = ()
    max_cost_usd: float | None = None
    policy_fingerprint_sha256: str
    route_reason: str
    work_item_ref: str | None = None
    goal_id: str | None = None
    decision_id: str | None = None
    execution_id: str | None = None
    input_plugin_provenance: tuple[InputPluginProvenance, ...] = ()
    input_gated_proposals: tuple[InputGatedProposal, ...] = ()
    attempts: tuple[ModelInvocationAttempt, ...] = ()
    selected_provider_id: str | None = None
    selected_model_id: str | None = None
    selected_concrete_model: str | None = None
    selected_model_version: str | None = None
    status: str
    created_at: float = Field(default_factory=time.time)
    completed_at: float | None = None


class ModelInvocationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    invocation: ModelInvocationRecord


class ModelGoalUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    goal_id: str
    calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)


class ModelDecisionUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)


class ModelGatewayState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = MODEL_GATEWAY_CONTRACT.current
    providers: list[ModelProviderRecord] = Field(default_factory=list)
    models: list[ModelDefinitionRecord] = Field(default_factory=list)
    prompt_templates: list[PromptTemplateRecord] = Field(default_factory=list)
    policies: list[TenantModelPolicy] = Field(default_factory=list)
    invocations: list[ModelInvocationRecord] = Field(default_factory=list)
