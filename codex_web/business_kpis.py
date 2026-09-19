from __future__ import annotations

import time
import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.business_context import BusinessEntityType
from codex_web.compatibility import ContractSpec
from codex_web.metrics import (
    MetricDirection,
    MetricFreshness,
    MetricThresholdOperator,
)


BUSINESS_KPI_CONTRACT = ContractSpec(
    "business-kpi-state",
    "1.0",
    ("1.0",),
)


class BusinessKpiDomain(StrEnum):
    FINANCE = "finance"
    REVENUE = "revenue"
    MARKETING = "marketing"
    PRODUCT = "product"
    CUSTOMER_SUCCESS = "customer_success"
    SUPPORT = "support"
    OPERATIONS = "operations"


class BusinessKpiOperandAggregation(StrEnum):
    SUM = "sum"
    AVERAGE = "average"
    MINIMUM = "minimum"
    MAXIMUM = "maximum"
    COUNT = "count"
    LAST = "last"


class BusinessKpiMissingPolicy(StrEnum):
    BLOCK = "block"
    PARTIAL = "partial"


class BusinessKpiExpressionKind(StrEnum):
    OPERAND = "operand"
    CONSTANT = "constant"
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    DIVIDE = "divide"


class BusinessKpiTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    domain: BusinessKpiDomain
    name: str
    description: str
    default_unit: str
    direction: MetricDirection
    formula_guidance: str


BUSINESS_KPI_TEMPLATES: tuple[BusinessKpiTemplate, ...] = (
    BusinessKpiTemplate(
        key="recurring_revenue",
        domain=BusinessKpiDomain.REVENUE,
        name="Recurring revenue",
        description="Recurring contracted or recognized revenue selected by tenant policy.",
        default_unit="currency",
        direction=MetricDirection.HIGHER_IS_BETTER,
        formula_guidance="Sum explicit recurring-revenue facts across the selected customer/subscription scope.",
    ),
    BusinessKpiTemplate(
        key="churn_rate",
        domain=BusinessKpiDomain.REVENUE,
        name="Churn rate",
        description="Tenant-defined churn numerator divided by an explicit starting/base population.",
        default_unit="percent",
        direction=MetricDirection.LOWER_IS_BETTER,
        formula_guidance="Define numerator and denominator operands explicitly; multiply the ratio by 100.",
    ),
    BusinessKpiTemplate(
        key="retention_rate",
        domain=BusinessKpiDomain.CUSTOMER_SUCCESS,
        name="Retention rate",
        description="Tenant-defined retained population or revenue divided by an explicit base.",
        default_unit="percent",
        direction=MetricDirection.HIGHER_IS_BETTER,
        formula_guidance="Define retained and base operands explicitly; multiply the ratio by 100.",
    ),
    BusinessKpiTemplate(
        key="pipeline_value",
        domain=BusinessKpiDomain.REVENUE,
        name="Pipeline value",
        description="Configured opportunity pipeline value over an explicit scope.",
        default_unit="currency",
        direction=MetricDirection.HIGHER_IS_BETTER,
        formula_guidance="Sum the selected pipeline-value fact across configured opportunity entities.",
    ),
    BusinessKpiTemplate(
        key="conversion_rate",
        domain=BusinessKpiDomain.MARKETING,
        name="Conversion rate",
        description="Explicit converted numerator divided by the configured eligible population.",
        default_unit="percent",
        direction=MetricDirection.HIGHER_IS_BETTER,
        formula_guidance="Define converted and eligible operands explicitly; multiply the ratio by 100.",
    ),
    BusinessKpiTemplate(
        key="support_responsiveness",
        domain=BusinessKpiDomain.SUPPORT,
        name="Support responsiveness",
        description="Configured response-time measure over governed support facts.",
        default_unit="seconds",
        direction=MetricDirection.LOWER_IS_BETTER,
        formula_guidance="Average or otherwise aggregate the explicitly mapped response-time fact.",
    ),
    BusinessKpiTemplate(
        key="customer_health",
        domain=BusinessKpiDomain.CUSTOMER_SUCCESS,
        name="Customer health",
        description="Tenant-defined deterministic customer-health score.",
        default_unit="score",
        direction=MetricDirection.HIGHER_IS_BETTER,
        formula_guidance="Compose explicit governed health inputs with deterministic arithmetic; no universal formula is assumed.",
    ),
    BusinessKpiTemplate(
        key="product_adoption",
        domain=BusinessKpiDomain.PRODUCT,
        name="Product adoption",
        description="Configured product usage/adoption measure across selected accounts or product areas.",
        default_unit="percent",
        direction=MetricDirection.HIGHER_IS_BETTER,
        formula_guidance="Use explicit adopted and eligible operands or another documented tenant formula.",
    ),
    BusinessKpiTemplate(
        key="service_cost",
        domain=BusinessKpiDomain.OPERATIONS,
        name="Service cost",
        description="Configured cloud/service/vendor cost over an explicit scope and currency.",
        default_unit="currency",
        direction=MetricDirection.LOWER_IS_BETTER,
        formula_guidance="Sum the governed cost facts for the selected service/vendor/cost-center scope.",
    ),
    BusinessKpiTemplate(
        key="gross_margin",
        domain=BusinessKpiDomain.FINANCE,
        name="Gross margin",
        description="Tenant-defined gross-margin inputs and formula.",
        default_unit="percent",
        direction=MetricDirection.HIGHER_IS_BETTER,
        formula_guidance="Typically revenue minus direct cost, divided by revenue, multiplied by 100; configure operands explicitly.",
    ),
    BusinessKpiTemplate(
        key="campaign_performance",
        domain=BusinessKpiDomain.MARKETING,
        name="Campaign performance",
        description="Tenant-selected deterministic campaign outcome/efficiency metric.",
        default_unit="score",
        direction=MetricDirection.HIGHER_IS_BETTER,
        formula_guidance="Configure explicit outcome and spend/eligible operands; no universal campaign formula is assumed.",
    ),
)


class BusinessKpiOperand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=200)
    fact_key: str = Field(min_length=1, max_length=300)
    aggregation: BusinessKpiOperandAggregation
    business_entity_ids: tuple[str, ...] = ()
    entity_types: tuple[BusinessEntityType, ...] = ()
    missing_policy: BusinessKpiMissingPolicy = BusinessKpiMissingPolicy.BLOCK

    @model_validator(mode="after")
    def normalize(self) -> "BusinessKpiOperand":
        object.__setattr__(
            self,
            "business_entity_ids",
            tuple(dict.fromkeys(self.business_entity_ids)),
        )
        object.__setattr__(
            self,
            "entity_types",
            tuple(dict.fromkeys(self.entity_types)),
        )
        return self


class BusinessKpiExpression(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: BusinessKpiExpressionKind
    operand_key: str | None = None
    constant: float | None = None
    left: "BusinessKpiExpression | None" = None
    right: "BusinessKpiExpression | None" = None

    @model_validator(mode="after")
    def validate_shape(self) -> "BusinessKpiExpression":
        if self.kind == BusinessKpiExpressionKind.OPERAND:
            if not self.operand_key or self.constant is not None or self.left or self.right:
                raise ValueError("operand expression requires only operand_key")
            return self
        if self.kind == BusinessKpiExpressionKind.CONSTANT:
            if self.constant is None or self.operand_key or self.left or self.right:
                raise ValueError("constant expression requires only constant")
            return self
        if self.operand_key is not None or self.constant is not None:
            raise ValueError("arithmetic expression cannot include operand_key/constant directly")
        if self.left is None or self.right is None:
            raise ValueError("arithmetic expression requires left and right expressions")
        return self

    def operand_keys(self) -> tuple[str, ...]:
        if self.kind == BusinessKpiExpressionKind.OPERAND:
            return (str(self.operand_key),)
        if self.kind == BusinessKpiExpressionKind.CONSTANT:
            return ()
        assert self.left is not None and self.right is not None
        return tuple(
            dict.fromkeys(
                (*self.left.operand_keys(), *self.right.operand_keys())
            )
        )


class BusinessKpiTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=300)
    operator: MetricThresholdOperator
    value: float


class BusinessKpiDefinitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=8000)
    owner_identity_id: str = Field(min_length=1)
    domain: BusinessKpiDomain
    template_key: str | None = Field(default=None, max_length=200)
    unit: str = Field(min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    direction: MetricDirection = MetricDirection.NEUTRAL
    freshness_seconds: int = Field(default=3600, ge=1)
    window_seconds: int | None = Field(default=None, ge=1)
    operands: tuple[BusinessKpiOperand, ...]
    expression: BusinessKpiExpression
    target: BusinessKpiTarget | None = None

    @model_validator(mode="after")
    def validate_definition(self) -> "BusinessKpiDefinitionCreate":
        keys = [item.key.casefold() for item in self.operands]
        if not keys:
            raise ValueError("business KPI requires at least one operand")
        if len(keys) != len(set(keys)):
            raise ValueError("business KPI operand keys must be unique")
        unknown = [
            key
            for key in self.expression.operand_keys()
            if key.casefold() not in set(keys)
        ]
        if unknown:
            raise ValueError(
                "business KPI expression references unknown operands: "
                + ", ".join(unknown)
            )
        if self.template_key is not None:
            known = {item.key for item in BUSINESS_KPI_TEMPLATES}
            if self.template_key not in known:
                raise ValueError("unknown business KPI template_key")
        if self.currency is not None:
            self.currency = self.currency.upper()
        return self


class BusinessKpiDefinitionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, min_length=1, max_length=8000)
    owner_identity_id: str | None = Field(default=None, min_length=1)
    unit: str | None = Field(default=None, min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    direction: MetricDirection | None = None
    freshness_seconds: int | None = Field(default=None, ge=1)
    window_seconds: int | None = Field(default=None, ge=1)
    operands: tuple[BusinessKpiOperand, ...] | None = None
    expression: BusinessKpiExpression | None = None
    target: BusinessKpiTarget | None = None
    reason: str = Field(min_length=1, max_length=4000)


class BusinessKpiDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"business-kpi-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    key: str
    name: str
    description: str
    owner_identity_id: str
    domain: BusinessKpiDomain
    template_key: str | None = None
    metric_id: str
    unit: str
    currency: str | None = None
    direction: MetricDirection
    freshness_seconds: int
    window_seconds: int | None = None
    operands: tuple[BusinessKpiOperand, ...]
    expression: BusinessKpiExpression
    target: BusinessKpiTarget | None = None
    revision: int = Field(default=1, ge=1)
    created_by: str
    updated_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class BusinessKpiDefinitionRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_id: str
    revision: int
    snapshot: BusinessKpiDefinition
    reason: str
    revised_by: str
    revised_at: float = Field(default_factory=time.time)


class BusinessKpiOperandAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operand_key: str
    business_entity_ids: tuple[str, ...] = ()
    selected_fact_ids: tuple[str, ...] = ()
    conflict_fact_ids: tuple[str, ...] = ()
    stale_fact_ids: tuple[str, ...] = ()
    revoked_source_fact_ids: tuple[str, ...] = ()
    missing_entity_ids: tuple[str, ...] = ()
    value: float | None = None
    partial: bool = False
    reason: str


class BusinessKpiObservationAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_id: str
    kpi_revision: int
    metric_id: str
    metric_revision: int
    metric_observation_id: str
    operand_attributions: tuple[BusinessKpiOperandAttribution, ...]
    selected_fact_ids: tuple[str, ...]
    formula_fingerprint: str
    created_at: float = Field(default_factory=time.time)


class BusinessKpiTrend(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    previous_observation_id: str | None = None
    previous_value: float | None = None
    absolute_delta: float | None = None
    percent_delta: float | None = None


class BusinessKpiTargetEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    operator: MetricThresholdOperator
    target_value: float
    passed: bool
    variance: float


class BusinessKpiEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_id: str
    kpi_revision: int
    metric_id: str
    metric_revision: int
    value: float | None = None
    unit: str
    freshness: MetricFreshness
    reasons: tuple[str, ...] = ()
    observation_id: str | None = None
    selected_fact_ids: tuple[str, ...] = ()
    operand_attributions: tuple[BusinessKpiOperandAttribution, ...] = ()
    target: BusinessKpiTargetEvaluation | None = None
    trend: BusinessKpiTrend = Field(default_factory=BusinessKpiTrend)
    evaluated_at: float = Field(default_factory=time.time)


class BusinessKpiOperatingItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_id: str
    kpi_revision: int
    name: str
    domain: BusinessKpiDomain
    metric_id: str
    metric_revision: int
    metric_snapshot_id: str | None = None
    observation_ids: tuple[str, ...] = ()
    value: float | None = None
    unit: str
    freshness: MetricFreshness
    reasons: tuple[str, ...] = ()
    selected_fact_ids: tuple[str, ...] = ()
    target: BusinessKpiTargetEvaluation | None = None
    trend: BusinessKpiTrend = Field(default_factory=BusinessKpiTrend)


class BusinessKpiOperatingSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"business-operating-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    items: tuple[BusinessKpiOperatingItem, ...]
    captured_by: str
    captured_at: float = Field(default_factory=time.time)


class BusinessKpiState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = BUSINESS_KPI_CONTRACT.current
    definitions: list[BusinessKpiDefinition] = Field(default_factory=list)
    revisions: list[BusinessKpiDefinitionRevision] = Field(default_factory=list)
    attributions: list[BusinessKpiObservationAttribution] = Field(default_factory=list)
    operating_snapshots: list[BusinessKpiOperatingSnapshot] = Field(default_factory=list)
