from __future__ import annotations

import hashlib
import json
import math
import time
import uuid

from codex_web.business_context import (
    FactFreshness,
)
from codex_web.business_kpis import (
    BUSINESS_KPI_CONTRACT,
    BusinessKPIDefinition,
    BusinessKPIDefinitionCreate,
    BusinessKPIDefinitionRevision,
    BusinessKPIDefinitionUpdate,
    BusinessKPIFormulaKind,
    BusinessKPIOperatingItem,
    BusinessKPIReadiness,
    BusinessKPIRefreshResult,
    BusinessKPIState,
    BusinessKPISnapshotItem,
    BusinessKPITargetBinding,
    BusinessKPITargetBindingCreate,
    BusinessKPITargetKind,
    BusinessKPITermAggregation,
    BusinessKPITermEvaluation,
    BusinessKPIThresholdEvaluation,
    BusinessKPIThresholdState,
    CompanyOperatingSnapshot,
    CompanyOperatingView,
)
from codex_web.identity import AuthenticationActor
from codex_web.metrics import (
    MetricAggregation,
    MetricDefinitionCreate,
    MetricDefinitionUpdate,
    MetricFreshness,
    MetricObservationCreate,
    MetricSnapshotRequest,
    MetricThresholdOperator,
)
from codex_web.services.business_context import (
    BusinessContextError,
    BusinessContextService,
)
from codex_web.services.decisions import DecisionService
from codex_web.services.goals import GoalService
from codex_web.services.metrics import (
    MetricError,
    MetricService,
)
from codex_web.storage.business_kpis import BusinessKPIStore


class BusinessKPIError(RuntimeError):
    pass


class BusinessKPINotFoundError(BusinessKPIError):
    pass


class BusinessKPIConflictError(BusinessKPIError):
    pass


class BusinessKPIValidationError(BusinessKPIError):
    pass


class BusinessKPIService:
    MAX_REFRESH_HISTORY = 1000
    MAX_SNAPSHOTS = 500

    def __init__(
        self,
        store: BusinessKPIStore,
        metrics: MetricService,
        business_context: BusinessContextService,
        goals: GoalService,
        decisions: DecisionService,
        *,
        clock=time.time,
    ) -> None:
        self.store = store
        self.metrics = metrics
        self.business_context = business_context
        self.goals = goals
        self.decisions = decisions
        self.clock = clock

    @staticmethod
    def _same_scope(item, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    def _definition(
        self,
        state: BusinessKPIState,
        kpi_id: str,
        *,
        actor: AuthenticationActor,
    ) -> BusinessKPIDefinition:
        item = next(
            (
                row
                for row in state.definitions
                if row.id == kpi_id and self._same_scope(row, actor)
            ),
            None,
        )
        if item is None:
            raise BusinessKPINotFoundError("business KPI not found")
        return item

    def get(
        self,
        kpi_id: str,
        *,
        actor: AuthenticationActor,
    ) -> BusinessKPIDefinition:
        return self._definition(self.store.load(), kpi_id, actor=actor)

    def list(
        self,
        *,
        actor: AuthenticationActor,
    ) -> tuple[BusinessKPIDefinition, ...]:
        rows = [
            item
            for item in self.store.load().definitions
            if self._same_scope(item, actor)
        ]
        rows.sort(key=lambda item: (item.domain.value, item.name.casefold(), item.id))
        return tuple(rows)

    @staticmethod
    def _revision(
        item: BusinessKPIDefinition,
        *,
        actor_id: str,
        reason: str,
        at: float,
    ) -> BusinessKPIDefinitionRevision:
        return BusinessKPIDefinitionRevision(
            kpi_id=item.id,
            revision=item.revision,
            snapshot=item.model_copy(deep=True),
            reason=reason,
            revised_by=actor_id,
            revised_at=at,
        )

    def revisions(
        self,
        kpi_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[BusinessKPIDefinitionRevision, ...]:
        state = self.store.load()
        self._definition(state, kpi_id, actor=actor)
        rows = [
            item
            for item in state.revisions
            if item.kpi_id == kpi_id
            and item.snapshot.organization_id == actor.organization_id
            and item.snapshot.workspace_id == actor.workspace_id
        ]
        rows.sort(key=lambda item: item.revision)
        return tuple(rows)

    def create(
        self,
        payload: BusinessKPIDefinitionCreate,
        *,
        actor: AuthenticationActor,
    ) -> BusinessKPIDefinition:
        state = self.store.load()
        if any(
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
            and item.key.casefold() == payload.key.casefold()
            for item in state.definitions
        ):
            raise BusinessKPIConflictError(
                "business KPI key already exists in workspace"
            )

        now = float(self.clock())
        kpi_id = f"business-kpi-{uuid.uuid4().hex}"
        source = f"business-kpi:{kpi_id}"
        metric = self.metrics.create_definition(
            MetricDefinitionCreate(
                key=f"business.{payload.key}",
                name=payload.name,
                description=payload.description,
                owner_identity_id=payload.owner_identity_id,
                unit=payload.unit,
                value_type=payload.value_type,
                aggregation=MetricAggregation.LAST,
                window_seconds=None,
                freshness_seconds=payload.freshness_seconds,
                direction=payload.direction,
                source_requirements=(source,),
                thresholds=payload.thresholds,
                project_id=payload.project_id,
                resource_id=payload.resource_id,
            ),
            scope=actor.tenant,
            actor_id=actor.identity_id,
        )
        item = BusinessKPIDefinition(
            id=kpi_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            metric_id=metric.id,
            key=payload.key,
            name=payload.name,
            description=payload.description,
            domain=payload.domain,
            owner_identity_id=payload.owner_identity_id,
            unit=payload.unit,
            value_type=payload.value_type,
            freshness_seconds=payload.freshness_seconds,
            direction=payload.direction,
            thresholds=payload.thresholds,
            formula=payload.formula,
            project_id=payload.project_id,
            resource_id=payload.resource_id,
            currency=payload.currency,
            metric_revision=metric.revision,
            created_by=actor.identity_id,
            updated_by=actor.identity_id,
            created_at=now,
            updated_at=now,
        )

        def apply(current: BusinessKPIState) -> BusinessKPIState:
            if any(
                row.organization_id == item.organization_id
                and row.workspace_id == item.workspace_id
                and row.key.casefold() == item.key.casefold()
                for row in current.definitions
            ):
                raise BusinessKPIConflictError(
                    "business KPI key already exists in workspace"
                )
            current.definitions.append(item)
            current.revisions.append(
                self._revision(
                    item,
                    actor_id=actor.identity_id,
                    reason="business KPI created",
                    at=now,
                )
            )
            return current

        try:
            self.store.update(apply)
        except Exception:
            # Metric definitions are immutable-versioned shared primitives.
            # If the KPI store write fails, leave the metric visible rather
            # than attempting a destructive rollback that could erase audit.
            raise
        return item

    def update(
        self,
        kpi_id: str,
        payload: BusinessKPIDefinitionUpdate,
        *,
        actor: AuthenticationActor,
    ) -> BusinessKPIDefinition:
        current = self.get(kpi_id, actor=actor)
        changes = payload.model_dump(
            mode="python",
            exclude={"reason"},
            exclude_unset=True,
        )
        if not changes:
            raise BusinessKPIConflictError(
                "business KPI update contains no changes"
            )
        if (
            "value_type" in changes
            and changes["value_type"] != current.value_type
        ):
            raise BusinessKPIValidationError(
                "business KPI value_type cannot change in place"
            )
        if changes.get("currency") is not None:
            changes["currency"] = str(changes["currency"]).upper()

        metric = self.metrics.update_definition(
            current.metric_id,
            MetricDefinitionUpdate(
                name=changes.get("name", current.name),
                description=changes.get("description", current.description),
                owner_identity_id=changes.get(
                    "owner_identity_id",
                    current.owner_identity_id,
                ),
                unit=changes.get("unit", current.unit),
                freshness_seconds=changes.get(
                    "freshness_seconds",
                    current.freshness_seconds,
                ),
                direction=changes.get("direction", current.direction),
                source_requirements=(f"business-kpi:{current.id}",),
                thresholds=changes.get("thresholds", current.thresholds),
                project_id=changes.get("project_id", current.project_id),
                resource_id=changes.get("resource_id", current.resource_id),
                reason=f"business KPI r{current.revision + 1}: {payload.reason}",
            ),
            scope=actor.tenant,
            actor_id=actor.identity_id,
        )
        now = float(self.clock())
        updated = current.model_copy(
            update={
                **changes,
                "revision": current.revision + 1,
                "metric_revision": metric.revision,
                "updated_by": actor.identity_id,
                "updated_at": now,
            }
        )

        def apply(state: BusinessKPIState) -> BusinessKPIState:
            stored = self._definition(state, kpi_id, actor=actor)
            if stored.revision != current.revision:
                raise BusinessKPIConflictError(
                    "business KPI changed during revision"
                )
            state.definitions = [
                updated if item.id == current.id else item
                for item in state.definitions
            ]
            state.revisions.append(
                self._revision(
                    updated,
                    actor_id=actor.identity_id,
                    reason=payload.reason,
                    at=now,
                )
            )
            return state

        self.store.update(apply)
        return updated

    def _term_entities(
        self,
        term,
        *,
        actor: AuthenticationActor,
    ):
        if term.business_entity_ids:
            rows = tuple(
                self.business_context.get_entity(item, actor=actor)
                for item in term.business_entity_ids
            )
            if term.entity_type is not None and any(
                item.entity_type != term.entity_type for item in rows
            ):
                raise BusinessKPIValidationError(
                    f"KPI term {term.alias!r} includes entity outside configured type"
                )
            return rows
        assert term.entity_type is not None
        return self.business_context.list_entities(
            actor=actor,
            entity_type=term.entity_type,
            include_inactive=False,
            limit=500,
        )

    def _evaluate_term(
        self,
        term,
        *,
        actor: AuthenticationActor,
        at: float,
    ) -> BusinessKPITermEvaluation:
        entities = self._term_entities(term, actor=actor)
        values: list[float] = []
        fact_ids: list[str] = []
        external_ids: list[str] = []
        used_entities: list[str] = []
        findings: list[str] = []
        partial = False

        if not entities:
            partial = True
            findings.append("no business entities match the KPI term scope")

        for entity in entities:
            resolved = self.business_context.resolve_fact(
                entity.id,
                term.fact_key,
                actor=actor,
                at=at,
            )
            if resolved.freshness != FactFreshness.FRESH or resolved.selected is None:
                partial = True
                findings.append(
                    f"{entity.id}: {term.fact_key} is {resolved.freshness.value}"
                )
                continue
            fact = resolved.selected
            if resolved.conflict:
                partial = True
                findings.append(
                    f"{entity.id}: {term.fact_key} has conflicting provider values"
                )
            fact_ids.append(fact.id)
            used_entities.append(entity.id)
            if fact.source.external_record_ref_id:
                external_ids.append(fact.source.external_record_ref_id)
            if term.aggregation == BusinessKPITermAggregation.COUNT:
                values.append(1.0)
                continue
            if isinstance(fact.value, bool) or not isinstance(fact.value, (int, float)):
                partial = True
                findings.append(
                    f"{entity.id}: {term.fact_key} is not numeric"
                )
                continue
            values.append(float(fact.value))

        value: float | int | None
        if not values:
            value = None
        elif term.aggregation == BusinessKPITermAggregation.SUM:
            value = sum(values)
        elif term.aggregation == BusinessKPITermAggregation.AVERAGE:
            value = sum(values) / len(values)
        elif term.aggregation == BusinessKPITermAggregation.MINIMUM:
            value = min(values)
        elif term.aggregation == BusinessKPITermAggregation.MAXIMUM:
            value = max(values)
        elif term.aggregation == BusinessKPITermAggregation.COUNT:
            value = len(values)
        else:
            raise BusinessKPIValidationError(
                f"unsupported KPI term aggregation: {term.aggregation}"
            )
        return BusinessKPITermEvaluation(
            alias=term.alias,
            fact_key=term.fact_key,
            aggregation=term.aggregation,
            value=value,
            fact_ids=tuple(dict.fromkeys(fact_ids)),
            external_record_ref_ids=tuple(dict.fromkeys(external_ids)),
            business_entity_ids=tuple(dict.fromkeys(used_entities)),
            partial=partial,
            findings=tuple(dict.fromkeys(findings)),
        )

    @staticmethod
    def _formula_value(formula, terms) -> tuple[float | None, tuple[str, ...]]:
        by_alias = {item.alias.casefold(): item for item in terms}
        left = by_alias[formula.left_alias.casefold()].value
        findings: list[str] = []
        if left is None:
            return None, (f"term {formula.left_alias!r} has no value",)
        left_value = float(left)
        if formula.kind == BusinessKPIFormulaKind.AGGREGATE:
            return left_value * formula.scale, ()

        assert formula.right_alias is not None
        right = by_alias[formula.right_alias.casefold()].value
        if right is None:
            return None, (f"term {formula.right_alias!r} has no value",)
        right_value = float(right)
        if formula.kind == BusinessKPIFormulaKind.RATIO:
            if right_value == 0:
                return None, ("ratio denominator is zero",)
            value = (left_value / right_value) * formula.scale
        elif formula.kind == BusinessKPIFormulaKind.DIFFERENCE:
            value = (left_value - right_value) * formula.scale
        elif formula.kind == BusinessKPIFormulaKind.PERCENT_CHANGE:
            if right_value == 0:
                return None, ("percent-change baseline is zero",)
            value = (
                (left_value - right_value)
                / abs(right_value)
                * 100.0
                * formula.scale
            )
        else:
            raise BusinessKPIValidationError(
                f"unsupported business KPI formula: {formula.kind}"
            )
        if not math.isfinite(value):
            findings.append("business KPI formula produced a non-finite value")
            return None, tuple(findings)
        return value, tuple(findings)

    @staticmethod
    def _refresh_key(
        item: BusinessKPIDefinition,
        terms: tuple[BusinessKPITermEvaluation, ...],
        value: float,
    ) -> str:
        payload = {
            "kpi_id": item.id,
            "revision": item.revision,
            "metric_revision": item.metric_revision,
            "value": value,
            "terms": [
                {
                    "alias": term.alias,
                    "fact_ids": term.fact_ids,
                    "partial": term.partial,
                }
                for term in terms
            ],
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:32]
        return f"business-kpi:{item.id}:r{item.revision}:{digest}"

    def _record_refresh(
        self,
        result: BusinessKPIRefreshResult,
    ) -> BusinessKPIRefreshResult:
        def apply(state: BusinessKPIState) -> BusinessKPIState:
            state.refreshes.append(result)
            if len(state.refreshes) > self.MAX_REFRESH_HISTORY:
                state.refreshes = sorted(
                    state.refreshes,
                    key=lambda item: item.refreshed_at,
                    reverse=True,
                )[: self.MAX_REFRESH_HISTORY]
            return state

        self.store.update(apply)
        return result

    def refresh(
        self,
        kpi_id: str,
        *,
        actor: AuthenticationActor,
        at: float | None = None,
    ) -> BusinessKPIRefreshResult:
        item = self.get(kpi_id, actor=actor)
        refreshed_at = float(self.clock()) if at is None else float(at)
        terms = tuple(
            self._evaluate_term(
                term,
                actor=actor,
                at=refreshed_at,
            )
            for term in item.formula.terms
        )
        value, formula_findings = self._formula_value(item.formula, terms)
        findings = [
            finding
            for term in terms
            for finding in term.findings
        ]
        findings.extend(formula_findings)
        partial = any(term.partial for term in terms) or value is None
        observation_id: str | None = None

        if value is not None:
            observation = self.metrics.ingest(
                item.metric_id,
                MetricObservationCreate(
                    value=value,
                    unit=item.unit,
                    observed_at=refreshed_at,
                    source=f"business-kpi:{item.id}",
                    provider="business-kpi",
                    partial=partial,
                    idempotency_key=self._refresh_key(item, terms, value),
                ),
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
            observation_id = observation.id

        result = BusinessKPIRefreshResult(
            kpi_id=item.id,
            kpi_revision=item.revision,
            metric_id=item.metric_id,
            metric_revision=item.metric_revision,
            observation_id=observation_id,
            value=value,
            unit=item.unit,
            partial=partial,
            terms=terms,
            findings=tuple(dict.fromkeys(findings)),
            refreshed_at=refreshed_at,
        )
        return self._record_refresh(result)

    def refresh_all(
        self,
        *,
        actor: AuthenticationActor,
        at: float | None = None,
    ) -> tuple[BusinessKPIRefreshResult, ...]:
        timestamp = float(self.clock()) if at is None else float(at)
        return tuple(
            self.refresh(item.id, actor=actor, at=timestamp)
            for item in self.list(actor=actor)
        )

    def latest_refresh(
        self,
        kpi_id: str,
        *,
        actor: AuthenticationActor,
    ) -> BusinessKPIRefreshResult | None:
        self.get(kpi_id, actor=actor)
        rows = [
            item
            for item in self.store.load().refreshes
            if item.kpi_id == kpi_id
        ]
        rows.sort(key=lambda item: item.refreshed_at, reverse=True)
        return rows[0] if rows else None

    def bind(
        self,
        payload: BusinessKPITargetBindingCreate,
        *,
        actor: AuthenticationActor,
    ) -> BusinessKPITargetBinding:
        self.get(payload.kpi_id, actor=actor)
        if payload.target_kind == BusinessKPITargetKind.GOAL:
            self.goals.get(payload.target_id, scope=actor.tenant)
        else:
            self.decisions.get(payload.target_id, actor=actor)
        item = BusinessKPITargetBinding(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            kpi_id=payload.kpi_id,
            target_kind=payload.target_kind,
            target_id=payload.target_id,
            window_start=payload.window_start,
            window_end=payload.window_end,
            purpose=payload.purpose,
            created_by=actor.identity_id,
            created_at=float(self.clock()),
        )

        def apply(state: BusinessKPIState) -> BusinessKPIState:
            duplicate = next(
                (
                    row
                    for row in state.bindings
                    if self._same_scope(row, actor)
                    and row.kpi_id == item.kpi_id
                    and row.target_kind == item.target_kind
                    and row.target_id == item.target_id
                    and row.window_start == item.window_start
                    and row.window_end == item.window_end
                ),
                None,
            )
            if duplicate is not None:
                raise BusinessKPIConflictError(
                    "equivalent business KPI target binding already exists"
                )
            state.bindings.append(item)
            return state

        self.store.update(apply)
        return item

    def bindings(
        self,
        *,
        actor: AuthenticationActor,
        kpi_id: str | None = None,
        target_kind: BusinessKPITargetKind | None = None,
        target_id: str | None = None,
    ) -> tuple[BusinessKPITargetBinding, ...]:
        if kpi_id is not None:
            self.get(kpi_id, actor=actor)
        rows = [
            item
            for item in self.store.load().bindings
            if self._same_scope(item, actor)
        ]
        if kpi_id is not None:
            rows = [item for item in rows if item.kpi_id == kpi_id]
        if target_kind is not None:
            rows = [item for item in rows if item.target_kind == target_kind]
        if target_id is not None:
            rows = [item for item in rows if item.target_id == target_id]
        rows.sort(key=lambda item: (item.created_at, item.id))
        return tuple(rows)

    def unbind(
        self,
        binding_id: str,
        *,
        actor: AuthenticationActor,
    ) -> None:
        found = False

        def apply(state: BusinessKPIState) -> BusinessKPIState:
            nonlocal found
            kept = []
            for item in state.bindings:
                if item.id == binding_id and self._same_scope(item, actor):
                    found = True
                    continue
                kept.append(item)
            state.bindings = kept
            return state

        self.store.update(apply)
        if not found:
            raise BusinessKPINotFoundError("business KPI binding not found")

    @staticmethod
    def _readiness(evaluation) -> BusinessKPIReadiness:
        if evaluation.freshness == MetricFreshness.FRESH:
            return BusinessKPIReadiness.CURRENT
        if evaluation.freshness == MetricFreshness.STALE:
            return BusinessKPIReadiness.STALE
        if evaluation.freshness == MetricFreshness.PARTIAL:
            return BusinessKPIReadiness.PARTIAL
        return BusinessKPIReadiness.MISSING

    @staticmethod
    def _thresholds(definition, evaluation, readiness):
        rows: list[BusinessKPIThresholdEvaluation] = []
        for threshold in definition.thresholds:
            state = BusinessKPIThresholdState.UNKNOWN
            variance: float | None = None
            observed = evaluation.value
            if (
                readiness == BusinessKPIReadiness.CURRENT
                and observed is not None
            ):
                if threshold.operator == MetricThresholdOperator.EQ:
                    met = observed == threshold.value
                elif threshold.operator == MetricThresholdOperator.GTE:
                    met = float(observed) >= float(threshold.value)
                elif threshold.operator == MetricThresholdOperator.LTE:
                    met = float(observed) <= float(threshold.value)
                else:
                    met = False
                state = (
                    BusinessKPIThresholdState.MET
                    if met
                    else BusinessKPIThresholdState.NOT_MET
                )
                if (
                    not isinstance(observed, bool)
                    and not isinstance(threshold.value, bool)
                ):
                    variance = float(observed) - float(threshold.value)
            rows.append(
                BusinessKPIThresholdEvaluation(
                    label=threshold.label,
                    operator=threshold.operator.value,
                    target_value=threshold.value,
                    observed_value=observed,
                    state=state,
                    variance=variance,
                )
            )
        return tuple(rows)

    def _operating_item(
        self,
        item: BusinessKPIDefinition,
        *,
        actor: AuthenticationActor,
        at: float,
    ) -> BusinessKPIOperatingItem:
        evaluation = self.metrics.evaluate(
            item.metric_id,
            scope=actor.tenant,
            at=at,
        )
        metric_definition = self.metrics.get_definition(
            item.metric_id,
            scope=actor.tenant,
        )
        readiness = self._readiness(evaluation)
        reasons: list[str] = []
        if readiness != BusinessKPIReadiness.CURRENT:
            reasons.append(evaluation.freshness_reason)

        latest = self.latest_refresh(item.id, actor=actor)
        if (
            latest is not None
            and latest.refreshed_at >= (evaluation.newest_observation_at or 0.0)
            and latest.observation_id is None
        ):
            readiness = BusinessKPIReadiness.MISSING
            reasons.extend(latest.findings or ("latest KPI refresh produced no observation",))
        elif latest is not None and latest.partial:
            readiness = BusinessKPIReadiness.PARTIAL
            reasons.extend(latest.findings or ("latest KPI refresh is partial",))

        history = tuple(
            row
            for row in self.metrics.history(
                item.metric_id,
                scope=actor.tenant,
                limit=100,
            )
            if row.metric_revision == metric_definition.revision
        )[:2]
        trend_delta = trend_percent = None
        if (
            readiness == BusinessKPIReadiness.CURRENT
            and len(history) >= 2
            and not isinstance(history[0].value, bool)
            and not isinstance(history[1].value, bool)
        ):
            current = float(history[0].value)
            previous = float(history[1].value)
            trend_delta = current - previous
            if previous != 0:
                trend_percent = trend_delta / abs(previous) * 100.0

        bindings = self.bindings(actor=actor, kpi_id=item.id)
        goal_ids = tuple(
            dict.fromkeys(
                row.target_id
                for row in bindings
                if row.target_kind == BusinessKPITargetKind.GOAL
            )
        )
        decision_ids = tuple(
            dict.fromkeys(
                row.target_id
                for row in bindings
                if row.target_kind == BusinessKPITargetKind.DECISION
            )
        )
        fact_keys = tuple(
            dict.fromkeys(term.fact_key for term in item.formula.terms)
        )
        external_ids = tuple(
            dict.fromkeys(
                external_id
                for term in (latest.terms if latest is not None else ())
                for external_id in term.external_record_ref_ids
            )
        )
        return BusinessKPIOperatingItem(
            kpi_id=item.id,
            kpi_revision=item.revision,
            metric_id=item.metric_id,
            metric_revision=metric_definition.revision,
            key=item.key,
            name=item.name,
            domain=item.domain,
            value=evaluation.value,
            unit=evaluation.unit,
            currency=item.currency,
            freshness=evaluation.freshness,
            readiness=readiness,
            readiness_reasons=tuple(dict.fromkeys(reasons)),
            observation_ids=evaluation.observation_ids,
            newest_observation_at=evaluation.newest_observation_at,
            trend_delta=trend_delta,
            trend_percent=trend_percent,
            thresholds=self._thresholds(
                metric_definition,
                evaluation,
                readiness,
            ),
            fact_keys=fact_keys,
            external_record_ref_ids=external_ids,
            goal_ids=goal_ids,
            decision_ids=decision_ids,
        )

    def operating_view(
        self,
        *,
        actor: AuthenticationActor,
        at: float | None = None,
    ) -> CompanyOperatingView:
        timestamp = float(self.clock()) if at is None else float(at)
        items = tuple(
            self._operating_item(item, actor=actor, at=timestamp)
            for item in self.list(actor=actor)
        )
        blockers = tuple(
            f"{item.key}: {item.readiness.value}"
            for item in items
            if item.readiness != BusinessKPIReadiness.CURRENT
        )
        return CompanyOperatingView(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            current=not blockers,
            items=items,
            blockers=blockers,
            evaluated_at=timestamp,
        )

    def capture_operating_snapshot(
        self,
        *,
        actor: AuthenticationActor,
    ) -> CompanyOperatingSnapshot:
        items: list[BusinessKPISnapshotItem] = []
        for kpi in self.list(actor=actor):
            metric_snapshot = self.metrics.capture_snapshot(
                kpi.metric_id,
                MetricSnapshotRequest(),
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )
            bindings = self.bindings(actor=actor, kpi_id=kpi.id)
            items.append(
                BusinessKPISnapshotItem(
                    kpi_id=kpi.id,
                    kpi_revision=kpi.revision,
                    metric_id=kpi.metric_id,
                    metric_revision=metric_snapshot.metric_revision,
                    metric_snapshot_id=metric_snapshot.id,
                    observation_ids=metric_snapshot.observation_ids,
                    value=metric_snapshot.value,
                    unit=metric_snapshot.unit,
                    freshness=metric_snapshot.freshness,
                    window_start=metric_snapshot.window_start,
                    window_end=metric_snapshot.window_end,
                    goal_ids=tuple(
                        dict.fromkeys(
                            row.target_id
                            for row in bindings
                            if row.target_kind == BusinessKPITargetKind.GOAL
                        )
                    ),
                    decision_ids=tuple(
                        dict.fromkeys(
                            row.target_id
                            for row in bindings
                            if row.target_kind == BusinessKPITargetKind.DECISION
                        )
                    ),
                )
            )
        snapshot = CompanyOperatingSnapshot(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            items=tuple(items),
            captured_by=actor.identity_id,
            captured_at=float(self.clock()),
        )

        def apply(state: BusinessKPIState) -> BusinessKPIState:
            state.operating_snapshots.append(snapshot)
            if len(state.operating_snapshots) > self.MAX_SNAPSHOTS:
                state.operating_snapshots = sorted(
                    state.operating_snapshots,
                    key=lambda item: item.captured_at,
                    reverse=True,
                )[: self.MAX_SNAPSHOTS]
            return state

        self.store.update(apply)
        return snapshot

    def operating_snapshots(
        self,
        *,
        actor: AuthenticationActor,
        limit: int = 100,
    ) -> tuple[CompanyOperatingSnapshot, ...]:
        if limit < 1 or limit > self.MAX_SNAPSHOTS:
            raise BusinessKPIValidationError(
                f"snapshot limit must be between 1 and {self.MAX_SNAPSHOTS}"
            )
        rows = [
            item
            for item in self.store.load().operating_snapshots
            if self._same_scope(item, actor)
        ]
        rows.sort(key=lambda item: (item.captured_at, item.id), reverse=True)
        return tuple(rows[:limit])

    def target_snapshot(
        self,
        target_kind: BusinessKPITargetKind,
        target_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[BusinessKPISnapshotItem, ...]:
        if target_kind == BusinessKPITargetKind.GOAL:
            self.goals.get(target_id, scope=actor.tenant)
        else:
            self.decisions.get(target_id, actor=actor)
        rows: list[BusinessKPISnapshotItem] = []
        snapshots = self.operating_snapshots(actor=actor, limit=self.MAX_SNAPSHOTS)
        for snapshot in snapshots:
            for item in snapshot.items:
                target_ids = (
                    item.goal_ids
                    if target_kind == BusinessKPITargetKind.GOAL
                    else item.decision_ids
                )
                if target_id in target_ids:
                    rows.append(item)
        return tuple(rows)
