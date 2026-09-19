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
    MetricThreshold,
    MetricValueType,
)


BUSINESS_KPI_CONTRACT = ContractSpec(
    "business-kpi-state",
    "1.0",
    ("1.0",),
)


class BusinessKPIPack(StrEnum):
    CFO = "cfo"
    CRO = "cro"
    CMO = "cmo"
    CPO = "cpo"
    CUSTOMER_SUCCESS = "customer_success"


class BusinessKPIDomain(StrEnum):
    FINANCE = "finance"
    REVENUE = "revenue"
    MARKETING = "marketing"
    PRODUCT = "product"
    CUSTOMER_SUCCESS = "customer_success"
    OPERATIONS = "operations"
    COST = "cost"
    CUSTOM = "custom"


class BusinessKPITermAggregation(StrEnum):
    SUM = "sum"
    AVERAGE = "average"
    MINIMUM = "minimum"
    MAXIMUM = "maximum"
    COUNT = "count"


class BusinessKPIFormulaKind(StrEnum):
    AGGREGATE = "aggregate"
    RATIO = "ratio"
    DIFFERENCE = "difference"
    PERCENT_CHANGE = "percent_change"


class BusinessKPITargetKind(StrEnum):
    GOAL = "goal"
    DECISION = "decision"


class BusinessKPIReadiness(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    PARTIAL = "partial"
    MISSING = "missing"


class BusinessKPIThresholdState(StrEnum):
    MET = "met"
    NOT_MET = "not_met"
    UNKNOWN = "unknown"


class BusinessKPIFactTerm(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    alias: str = Field(min_length=1, max_length=100)
    fact_key: str = Field(min_length=1, max_length=300)
    aggregation: BusinessKPITermAggregation
    entity_type: BusinessEntityType | None = None
    business_entity_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_scope(self) -> "BusinessKPIFactTerm":
        object.__setattr__(
            self,
            "business_entity_ids",
            tuple(dict.fromkeys(self.business_entity_ids)),
        )
        if self.entity_type is None and not self.business_entity_ids:
            raise ValueError(
                "business KPI term requires entity_type or explicit business_entity_ids"
            )
        return self


class BusinessKPIFormula(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: BusinessKPIFormulaKind
    terms: tuple[BusinessKPIFactTerm, ...]
    left_alias: str
    right_alias: str | None = None
    scale: float = 1.0

    @model_validator(mode="after")
    def validate_formula(self) -> "BusinessKPIFormula":
        if not self.terms:
            raise ValueError("business KPI formula requires at least one term")
        aliases = [item.alias.casefold() for item in self.terms]
        if len(aliases) != len(set(aliases)):
            raise ValueError("business KPI term aliases must be unique")
        known = set(aliases)
        if self.left_alias.casefold() not in known:
            raise ValueError("left_alias must reference a declared KPI term")
        if self.kind != BusinessKPIFormulaKind.AGGREGATE:
            if not self.right_alias:
                raise ValueError(f"{self.kind.value} KPI formula requires right_alias")
            if self.right_alias.casefold() not in known:
                raise ValueError("right_alias must reference a declared KPI term")
        elif self.right_alias is not None:
            raise ValueError("aggregate KPI formula cannot define right_alias")
        return self


class BusinessKPIDefinitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    domain: BusinessKPIDomain
    owner_identity_id: str = Field(min_length=1)
    unit: str = Field(min_length=1, max_length=100)
    value_type: MetricValueType = MetricValueType.NUMBER
    freshness_seconds: int = Field(default=3600, ge=1)
    window_seconds: int | None = Field(default=None, ge=1)
    direction: MetricDirection = MetricDirection.NEUTRAL
    thresholds: tuple[MetricThreshold, ...] = ()
    formula: BusinessKPIFormula
    project_id: str | None = None
    resource_id: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=10)

    @model_validator(mode="after")
    def validate_currency(self) -> "BusinessKPIDefinitionCreate":
        if self.value_type != MetricValueType.NUMBER:
            raise ValueError(
                "business KPI formulas currently produce numeric Metric observations"
            )
        if self.currency is not None:
            self.currency = self.currency.upper()
        return self


class BusinessKPIDefinitionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    domain: BusinessKPIDomain | None = None
    owner_identity_id: str | None = Field(default=None, min_length=1)
    unit: str | None = Field(default=None, min_length=1, max_length=100)
    value_type: MetricValueType | None = None
    freshness_seconds: int | None = Field(default=None, ge=1)
    window_seconds: int | None = Field(default=None, ge=1)
    direction: MetricDirection | None = None
    thresholds: tuple[MetricThreshold, ...] | None = None
    formula: BusinessKPIFormula | None = None
    project_id: str | None = None
    resource_id: str | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=10)
    reason: str = Field(min_length=1, max_length=4000)


class BusinessKPIDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(default_factory=lambda: f"business-kpi-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    metric_id: str
    key: str
    name: str
    description: str
    domain: BusinessKPIDomain
    owner_identity_id: str
    unit: str
    value_type: MetricValueType
    freshness_seconds: int
    window_seconds: int | None = None
    direction: MetricDirection
    thresholds: tuple[MetricThreshold, ...] = ()
    formula: BusinessKPIFormula
    project_id: str | None = None
    resource_id: str | None = None
    currency: str | None = None
    revision: int = Field(default=1, ge=1)
    metric_revision: int = Field(ge=1)
    created_by: str
    updated_by: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class BusinessKPIDefinitionRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_id: str
    revision: int = Field(ge=1)
    snapshot: BusinessKPIDefinition
    reason: str
    revised_by: str
    revised_at: float = Field(default_factory=time.time)


class BusinessKPITargetBindingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kpi_id: str = Field(min_length=1)
    target_kind: BusinessKPITargetKind
    target_id: str = Field(min_length=1)
    window_start: float | None = None
    window_end: float | None = None
    purpose: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_window(self) -> "BusinessKPITargetBindingCreate":
        if self.window_start is not None and self.window_end is not None:
            if self.window_end < self.window_start:
                raise ValueError("KPI binding window_end must be >= window_start")
        return self


class BusinessKPITargetBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"business-kpi-binding-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    kpi_id: str
    target_kind: BusinessKPITargetKind
    target_id: str
    window_start: float | None = None
    window_end: float | None = None
    purpose: str
    created_by: str
    created_at: float = Field(default_factory=time.time)


class BusinessKPITermEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: str
    fact_key: str
    aggregation: BusinessKPITermAggregation
    value: float | int | None = None
    fact_ids: tuple[str, ...] = ()
    external_record_ref_ids: tuple[str, ...] = ()
    business_entity_ids: tuple[str, ...] = ()
    partial: bool = False
    findings: tuple[str, ...] = ()


class BusinessKPIRefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_id: str
    kpi_revision: int
    metric_id: str
    metric_revision: int
    observation_id: str | None = None
    value: float | int | None = None
    unit: str
    partial: bool
    terms: tuple[BusinessKPITermEvaluation, ...] = ()
    findings: tuple[str, ...] = ()
    refreshed_at: float = Field(default_factory=time.time)


class BusinessKPIThresholdEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    operator: str
    target_value: float | int | bool
    observed_value: float | int | bool | None = None
    state: BusinessKPIThresholdState
    variance: float | None = None


class BusinessKPIOperatingItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_id: str
    kpi_revision: int
    metric_id: str
    metric_revision: int
    key: str
    name: str
    domain: BusinessKPIDomain
    value: float | int | bool | None = None
    unit: str
    currency: str | None = None
    window_seconds: int | None = None
    freshness: MetricFreshness
    readiness: BusinessKPIReadiness
    readiness_reasons: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    newest_observation_at: float | None = None
    trend_delta: float | None = None
    trend_percent: float | None = None
    thresholds: tuple[BusinessKPIThresholdEvaluation, ...] = ()
    fact_keys: tuple[str, ...] = ()
    external_record_ref_ids: tuple[str, ...] = ()
    goal_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()


class CompanyOperatingView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    organization_id: str
    workspace_id: str
    current: bool
    items: tuple[BusinessKPIOperatingItem, ...] = ()
    blockers: tuple[str, ...] = ()
    evaluated_at: float = Field(default_factory=time.time)


class BusinessKPISnapshotItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kpi_id: str
    kpi_revision: int
    metric_id: str
    metric_revision: int
    metric_snapshot_id: str
    observation_ids: tuple[str, ...] = ()
    value: float | int | bool | None = None
    unit: str
    freshness: MetricFreshness
    window_start: float | None = None
    window_end: float | None = None
    goal_ids: tuple[str, ...] = ()
    decision_ids: tuple[str, ...] = ()


class BusinessKPITargetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"business-kpi-target-snapshot-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    binding_id: str
    target_kind: BusinessKPITargetKind
    target_id: str
    kpi_id: str
    kpi_revision: int
    metric_id: str
    metric_revision: int
    metric_snapshot_id: str
    observation_ids: tuple[str, ...] = ()
    value: float | int | bool | None = None
    unit: str
    freshness: MetricFreshness
    window_start: float | None = None
    window_end: float | None = None
    captured_by: str
    captured_at: float = Field(default_factory=time.time)


class CompanyOperatingSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: f"company-operating-snapshot-{uuid.uuid4().hex}")
    organization_id: str
    workspace_id: str
    items: tuple[BusinessKPISnapshotItem, ...]
    captured_by: str
    captured_at: float = Field(default_factory=time.time)


class BusinessKPITemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pack: BusinessKPIPack
    key: str
    name: str
    domain: BusinessKPIDomain
    description: str
    suggested_unit: str
    supported_formula_kinds: tuple[BusinessKPIFormulaKind, ...]
    required_explicit_inputs: tuple[str, ...]
    notes: str


BUSINESS_KPI_TEMPLATES: tuple[BusinessKPITemplate, ...] = (
    BusinessKPITemplate(
        pack=BusinessKPIPack.CFO,
        key="recurring_revenue",
        name="Recurring revenue",
        domain=BusinessKPIDomain.FINANCE,
        description="Tenant-defined recurring revenue over selected contracts/subscriptions.",
        suggested_unit="currency",
        supported_formula_kinds=(BusinessKPIFormulaKind.AGGREGATE,),
        required_explicit_inputs=("revenue fact key", "entity scope", "currency", "freshness/window"),
        notes="No ARR/MRR normalization convention is assumed.",
    ),
    BusinessKPITemplate(
        pack=BusinessKPIPack.CFO,
        key="gross_margin_inputs",
        name="Gross-margin inputs",
        domain=BusinessKPIDomain.FINANCE,
        description="Explicit revenue and direct-cost inputs for tenant-defined margin reporting.",
        suggested_unit="currency_or_percent",
        supported_formula_kinds=(
            BusinessKPIFormulaKind.DIFFERENCE,
            BusinessKPIFormulaKind.RATIO,
        ),
        required_explicit_inputs=("revenue term", "direct-cost term", "currency", "formula scale"),
        notes="Cost allocation policy remains tenant-defined.",
    ),
    BusinessKPITemplate(
        pack=BusinessKPIPack.CFO,
        key="cloud_service_cost",
        name="Cloud/service cost",
        domain=BusinessKPIDomain.COST,
        description="Selected infrastructure/service cost facts across configured cost scopes.",
        suggested_unit="currency",
        supported_formula_kinds=(BusinessKPIFormulaKind.AGGREGATE,),
        required_explicit_inputs=("cost fact key", "cost-center/resource scope", "currency"),
        notes="No provider billing taxonomy is assumed.",
    ),
    BusinessKPITemplate(
        pack=BusinessKPIPack.CRO,
        key="pipeline",
        name="Pipeline",
        domain=BusinessKPIDomain.REVENUE,
        description="Configured opportunity/pipeline value or count.",
        suggested_unit="currency_or_count",
        supported_formula_kinds=(BusinessKPIFormulaKind.AGGREGATE,),
        required_explicit_inputs=("pipeline fact key", "opportunity scope", "currency/window"),
        notes="Stage weighting and qualification rules must be explicit in source facts.",
    ),
    BusinessKPITemplate(
        pack=BusinessKPIPack.CRO,
        key="conversion",
        name="Conversion",
        domain=BusinessKPIDomain.REVENUE,
        description="Explicit numerator/denominator conversion measure.",
        suggested_unit="percent",
        supported_formula_kinds=(BusinessKPIFormulaKind.RATIO,),
        required_explicit_inputs=("converted term", "eligible-population term", "window", "scale"),
        notes="The platform does not assume what counts as a conversion.",
    ),
    BusinessKPITemplate(
        pack=BusinessKPIPack.CRO,
        key="retention_or_churn",
        name="Retention / churn",
        domain=BusinessKPIDomain.REVENUE,
        description="Tenant-defined retained/churned population or value.",
        suggested_unit="percent",
        supported_formula_kinds=(
            BusinessKPIFormulaKind.RATIO,
            BusinessKPIFormulaKind.PERCENT_CHANGE,
        ),
        required_explicit_inputs=("population/value terms", "window", "scale"),
        notes="Logo, revenue and cohort retention are distinct definitions and must not be conflated.",
    ),
    BusinessKPITemplate(
        pack=BusinessKPIPack.CMO,
        key="campaign_performance",
        name="Campaign performance",
        domain=BusinessKPIDomain.MARKETING,
        description="Configured campaign outcome, cost or conversion measure.",
        suggested_unit="tenant_defined",
        supported_formula_kinds=(
            BusinessKPIFormulaKind.AGGREGATE,
            BusinessKPIFormulaKind.RATIO,
        ),
        required_explicit_inputs=("campaign scope", "outcome/cost facts", "window"),
        notes="Attribution model remains source-/tenant-defined.",
    ),
    BusinessKPITemplate(
        pack=BusinessKPIPack.CPO,
        key="product_adoption",
        name="Product adoption",
        domain=BusinessKPIDomain.PRODUCT,
        description="Configured usage/adoption measure for a product or product area.",
        suggested_unit="count_or_percent",
        supported_formula_kinds=(
            BusinessKPIFormulaKind.AGGREGATE,
            BusinessKPIFormulaKind.RATIO,
        ),
        required_explicit_inputs=("usage fact", "eligible population", "product scope", "window"),
        notes="Active-user/adoption semantics must be explicitly defined.",
    ),
    BusinessKPITemplate(
        pack=BusinessKPIPack.CUSTOMER_SUCCESS,
        key="support_responsiveness",
        name="Support responsiveness",
        domain=BusinessKPIDomain.CUSTOMER_SUCCESS,
        description="Configured response-time or SLA-attainment measure.",
        suggested_unit="time_or_percent",
        supported_formula_kinds=(
            BusinessKPIFormulaKind.AGGREGATE,
            BusinessKPIFormulaKind.RATIO,
        ),
        required_explicit_inputs=("support fact", "relationship scope", "window"),
        notes="Priority/SLA class and clock semantics are tenant-defined.",
    ),
    BusinessKPITemplate(
        pack=BusinessKPIPack.CUSTOMER_SUCCESS,
        key="customer_health",
        name="Customer health",
        domain=BusinessKPIDomain.CUSTOMER_SUCCESS,
        description="Configured measured customer-health input or score.",
        suggested_unit="score",
        supported_formula_kinds=(
            BusinessKPIFormulaKind.AGGREGATE,
            BusinessKPIFormulaKind.RATIO,
        ),
        required_explicit_inputs=("health fact(s)", "customer scope", "window"),
        notes="No universal health-score weighting is assumed.",
    ),
)


class BusinessKPIState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = BUSINESS_KPI_CONTRACT.current
    definitions: list[BusinessKPIDefinition] = Field(default_factory=list)
    revisions: list[BusinessKPIDefinitionRevision] = Field(default_factory=list)
    bindings: list[BusinessKPITargetBinding] = Field(default_factory=list)
    refreshes: list[BusinessKPIRefreshResult] = Field(default_factory=list)
    operating_snapshots: list[CompanyOperatingSnapshot] = Field(default_factory=list)
    target_snapshots: list[BusinessKPITargetSnapshot] = Field(default_factory=list)
