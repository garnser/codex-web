from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from codex_web.business_context import FactFreshness
from codex_web.business_kpis import (
    BUSINESS_KPI_TEMPLATES,
    BusinessKpiDefinition,
    BusinessKpiDefinitionCreate,
    BusinessKpiDefinitionRevision,
    BusinessKpiDefinitionUpdate,
    BusinessKpiEvaluation,
    BusinessKpiExpression,
    BusinessKpiExpressionKind,
    BusinessKpiMissingPolicy,
    BusinessKpiObservationAttribution,
    BusinessKpiOperand,
    BusinessKpiOperandAggregation,
    BusinessKpiOperandAttribution,
    BusinessKpiOperatingItem,
    BusinessKpiOperatingSnapshot,
    BusinessKpiState,
    BusinessKpiTargetEvaluation,
    BusinessKpiTrend,
)
from codex_web.decisions import (
    DecisionEvidenceInput,
    DecisionEvidenceKind,
    DecisionUpdate,
)
from codex_web.goals import (
    GoalCriterionKind,
    GoalCriterionOperator,
    GoalSuccessCriterion,
    GoalUpdate,
)
from codex_web.identity import AuthenticationActor
from codex_web.metrics import (
    MetricAggregation,
    MetricDefinitionCreate,
    MetricDefinitionUpdate,
    MetricFreshness,
    MetricObservationCreate,
    MetricThreshold,
    MetricThresholdOperator,
    MetricValueType,
)
from codex_web.services.business_context import (
    BusinessContextNotFoundError,
    BusinessContextService,
)
from codex_web.services.decisions import DecisionService
from codex_web.services.goals import GoalService
from codex_web.services.metrics import (
    MetricError,
    MetricService,
)
from codex_web.storage.business_kpis import BusinessKpiStore


class BusinessKpiError(RuntimeError):
    pass


class BusinessKpiNotFoundError(BusinessKpiError):
    pass


class BusinessKpiConflictError(BusinessKpiError):
    pass


class BusinessKpiValidationError(BusinessKpiError):
    pass


class BusinessKpiService:
    MAX_ENTITY_SCAN = 500

    def __init__(
        self,
        store: BusinessKpiStore,
        metrics: MetricService,
        business_context: BusinessContextService,
        *,
        goals: GoalService | None = None,
        decisions: DecisionService | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.metrics = metrics
        self.business_context = business_context
        self.goals = goals
        self.decisions = decisions
        self.clock = clock

    @staticmethod
    def templates():
        return BUSINESS_KPI_TEMPLATES

    @staticmethod
    def _visible(item: BusinessKpiDefinition, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    def _definition(
        self,
        state: BusinessKpiState,
        kpi_id: str,
        actor: AuthenticationActor,
    ) -> BusinessKpiDefinition:
        item = next(
            (
                row
                for row in state.definitions
                if row.id == kpi_id and self._visible(row, actor)
            ),
            None,
        )
        if item is None:
            raise BusinessKpiNotFoundError("business KPI not found")
        return item

    def get(
        self,
        kpi_id: str,
        *,
        actor: AuthenticationActor,
    ) -> BusinessKpiDefinition:
        return self._definition(self.store.load(), kpi_id, actor)

    def list(
        self,
        *,
        actor: AuthenticationActor,
    ) -> tuple[BusinessKpiDefinition, ...]:
        rows = [
            item
            for item in self.store.load().definitions
            if self._visible(item, actor)
        ]
        rows.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return tuple(rows)

    @staticmethod
    def _thresholds(target) -> tuple[MetricThreshold, ...]:
        if target is None:
            return ()
        return (
            MetricThreshold(
                label=target.label,
                operator=target.operator,
                value=target.value,
            ),
        )

    def create(
        self,
        payload: BusinessKpiDefinitionCreate,
        *,
        actor: AuthenticationActor,
    ) -> BusinessKpiDefinition:
        state = self.store.load()
        if any(
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
            and item.key.casefold() == payload.key.casefold()
            for item in state.definitions
        ):
            raise BusinessKpiConflictError(
                "business KPI key already exists in workspace"
            )

        kpi_id = f"business-kpi-{uuid.uuid4().hex}"
        metric = self.metrics.create_definition(
            MetricDefinitionCreate(
                key=payload.key,
                name=payload.name,
                description=payload.description,
                owner_identity_id=payload.owner_identity_id,
                unit=payload.unit,
                value_type=MetricValueType.NUMBER,
                aggregation=MetricAggregation.LAST,
                window_seconds=payload.window_seconds,
                freshness_seconds=payload.freshness_seconds,
                direction=payload.direction,
                source_requirements=(f"business-kpi:{kpi_id}:r1",),
                thresholds=self._thresholds(payload.target),
            ),
            scope=actor.tenant,
            actor_id=actor.identity_id,
        )
        now = float(self.clock())
        item = BusinessKpiDefinition(
            id=kpi_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            key=payload.key,
            name=payload.name,
            description=payload.description,
            owner_identity_id=payload.owner_identity_id,
            domain=payload.domain,
            template_key=payload.template_key,
            metric_id=metric.id,
            unit=payload.unit,
            currency=payload.currency,
            direction=payload.direction,
            freshness_seconds=payload.freshness_seconds,
            window_seconds=payload.window_seconds,
            operands=payload.operands,
            expression=payload.expression,
            target=payload.target,
            created_by=actor.identity_id,
            updated_by=actor.identity_id,
            created_at=now,
            updated_at=now,
        )
        revision = BusinessKpiDefinitionRevision(
            kpi_id=item.id,
            revision=1,
            snapshot=item.model_copy(deep=True),
            reason="business KPI created",
            revised_by=actor.identity_id,
            revised_at=now,
        )

        def apply(current: BusinessKpiState) -> BusinessKpiState:
            if any(
                row.organization_id == actor.organization_id
                and row.workspace_id == actor.workspace_id
                and row.key.casefold() == item.key.casefold()
                for row in current.definitions
            ):
                raise BusinessKpiConflictError(
                    "business KPI key already exists in workspace"
                )
            current.definitions.append(item)
            current.revisions.append(revision)
            return current

        try:
            self.store.update(apply)
        except Exception:
            # The metric is intentionally left inspectable rather than being
            # silently deleted after a failed cross-domain write.
            raise
        return item

    def revisions(
        self,
        kpi_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[BusinessKpiDefinitionRevision, ...]:
        self.get(kpi_id, actor=actor)
        rows = [
            item
            for item in self.store.load().revisions
            if item.kpi_id == kpi_id
        ]
        rows.sort(key=lambda item: item.revision)
        return tuple(rows)

    @staticmethod
    def _validated_candidate(
        current: BusinessKpiDefinition,
        payload: BusinessKpiDefinitionUpdate,
    ) -> dict[str, Any]:
        changes = payload.model_dump(
            mode="python",
            exclude={"reason"},
            exclude_unset=True,
        )
        if not changes:
            raise BusinessKpiConflictError(
                "business KPI update contains no changes"
            )
        if "currency" in changes and changes["currency"] is not None:
            changes["currency"] = str(changes["currency"]).upper()
        candidate = current.model_dump(mode="python")
        candidate.update(changes)
        BusinessKpiDefinitionCreate(
            key=current.key,
            name=candidate["name"],
            description=candidate["description"],
            owner_identity_id=candidate["owner_identity_id"],
            domain=current.domain,
            template_key=current.template_key,
            unit=candidate["unit"],
            currency=candidate.get("currency"),
            direction=candidate["direction"],
            freshness_seconds=candidate["freshness_seconds"],
            window_seconds=candidate.get("window_seconds"),
            operands=candidate["operands"],
            expression=candidate["expression"],
            target=candidate.get("target"),
        )
        return changes

    def update(
        self,
        kpi_id: str,
        payload: BusinessKpiDefinitionUpdate,
        *,
        actor: AuthenticationActor,
    ) -> BusinessKpiDefinition:
        current = self.get(kpi_id, actor=actor)
        changes = self._validated_candidate(current, payload)

        metric_changes: dict[str, Any] = {
            "reason": payload.reason,
            "source_requirements": (
                f"business-kpi:{current.id}:r{current.revision + 1}",
            ),
        }
        mapping = {
            "name": "name",
            "description": "description",
            "owner_identity_id": "owner_identity_id",
            "unit": "unit",
            "direction": "direction",
            "freshness_seconds": "freshness_seconds",
            "window_seconds": "window_seconds",
        }
        for source_field, metric_field in mapping.items():
            if source_field in changes:
                metric_changes[metric_field] = changes[source_field]
        if "target" in changes:
            metric_changes["thresholds"] = self._thresholds(changes["target"])
        self.metrics.update_definition(
            current.metric_id,
            MetricDefinitionUpdate(**metric_changes),
            scope=actor.tenant,
            actor_id=actor.identity_id,
        )

        updated: list[BusinessKpiDefinition] = []

        def apply(state: BusinessKpiState) -> BusinessKpiState:
            selected = self._definition(state, kpi_id, actor)
            if selected.revision != current.revision:
                raise BusinessKpiConflictError(
                    "business KPI changed while update was being applied"
                )
            now = float(self.clock())
            replacement = selected.model_copy(
                update={
                    **changes,
                    "revision": selected.revision + 1,
                    "updated_by": actor.identity_id,
                    "updated_at": now,
                }
            )
            state.definitions = [
                replacement if item.id == kpi_id else item
                for item in state.definitions
            ]
            state.revisions.append(
                BusinessKpiDefinitionRevision(
                    kpi_id=kpi_id,
                    revision=replacement.revision,
                    snapshot=replacement.model_copy(deep=True),
                    reason=payload.reason,
                    revised_by=actor.identity_id,
                    revised_at=now,
                )
            )
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def _entities_for_operand(
        self,
        operand: BusinessKpiOperand,
        *,
        actor: AuthenticationActor,
    ):
        if operand.business_entity_ids:
            rows = [
                self.business_context.get_entity(item, actor=actor)
                for item in operand.business_entity_ids
            ]
        elif operand.entity_types:
            rows = []
            for entity_type in operand.entity_types:
                rows.extend(
                    self.business_context.list_entities(
                        actor=actor,
                        entity_type=entity_type,
                        limit=self.MAX_ENTITY_SCAN,
                    )
                )
        else:
            rows = list(
                self.business_context.list_entities(
                    actor=actor,
                    limit=self.MAX_ENTITY_SCAN,
                )
            )
        unique = {item.id: item for item in rows}
        return tuple(
            sorted(unique.values(), key=lambda item: (item.name.casefold(), item.id))
        )

    def _evaluate_operand(
        self,
        operand: BusinessKpiOperand,
        *,
        actor: AuthenticationActor,
        at: float,
    ) -> BusinessKpiOperandAttribution:
        entities = self._entities_for_operand(operand, actor=actor)
        selected = []
        conflict_ids: list[str] = []
        stale_ids: list[str] = []
        revoked_ids: list[str] = []
        missing_entities: list[str] = []
        partial = False
        reasons: list[str] = []

        for entity in entities:
            resolution = self.business_context.resolve_fact(
                entity.id,
                operand.fact_key,
                actor=actor,
                at=at,
            )
            conflict_ids.extend(resolution.conflict_fact_ids)
            stale_ids.extend(resolution.stale_fact_ids)
            revoked_ids.extend(resolution.revoked_source_fact_ids)
            if resolution.conflict:
                partial = True
                reasons.append(f"{entity.id}: conflicting providers")
            if resolution.stale_fact_ids:
                partial = True
                reasons.append(f"{entity.id}: stale competing source fact(s)")
            if resolution.revoked_source_fact_ids:
                partial = True
                reasons.append(f"{entity.id}: revoked competing source fact(s)")
            if resolution.selected is None:
                missing_entities.append(entity.id)
                if resolution.freshness == FactFreshness.STALE:
                    reasons.append(f"{entity.id}: stale source fact")
                elif resolution.freshness == FactFreshness.SOURCE_REVOKED:
                    reasons.append(f"{entity.id}: source record revoked")
                else:
                    reasons.append(f"{entity.id}: fact missing")
                continue
            selected.append((entity, resolution.selected))

        if missing_entities:
            partial = True

        numeric: list[tuple[Any, float]] = []
        if operand.aggregation != BusinessKpiOperandAggregation.COUNT:
            for entity, fact in selected:
                if type(fact.value) not in {int, float}:
                    raise BusinessKpiValidationError(
                        f"operand {operand.key!r} received non-numeric "
                        f"CompanyFact {fact.id}"
                    )
                numeric.append((fact, float(fact.value)))

        value: float | None
        if (
            missing_entities
            and operand.missing_policy == BusinessKpiMissingPolicy.BLOCK
        ):
            value = None
            reasons.append("missing policy blocks operand evaluation")
        elif not selected:
            value = None
            reasons.append("no usable source facts")
        elif operand.aggregation == BusinessKpiOperandAggregation.COUNT:
            value = float(len(selected))
        else:
            values = [number for _fact, number in numeric]
            if operand.aggregation == BusinessKpiOperandAggregation.SUM:
                value = float(sum(values))
            elif operand.aggregation == BusinessKpiOperandAggregation.AVERAGE:
                value = float(sum(values) / len(values))
            elif operand.aggregation == BusinessKpiOperandAggregation.MINIMUM:
                value = float(min(values))
            elif operand.aggregation == BusinessKpiOperandAggregation.MAXIMUM:
                value = float(max(values))
            elif operand.aggregation == BusinessKpiOperandAggregation.LAST:
                newest = max(
                    numeric,
                    key=lambda item: (item[0].observed_at, item[0].id),
                )
                value = newest[1]
            else:
                raise BusinessKpiValidationError(
                    f"unsupported operand aggregation: {operand.aggregation}"
                )

        selected_facts = [fact for _entity, fact in selected]
        observed = [item.observed_at for item in selected_facts]
        return BusinessKpiOperandAttribution(
            operand_key=operand.key,
            business_entity_ids=tuple(item.id for item in entities),
            selected_fact_ids=tuple(item.id for item in selected_facts),
            conflict_fact_ids=tuple(dict.fromkeys(conflict_ids)),
            stale_fact_ids=tuple(dict.fromkeys(stale_ids)),
            revoked_source_fact_ids=tuple(dict.fromkeys(revoked_ids)),
            missing_entity_ids=tuple(dict.fromkeys(missing_entities)),
            oldest_selected_at=min(observed) if observed else None,
            newest_selected_at=max(observed) if observed else None,
            value=value,
            partial=partial,
            reason="; ".join(dict.fromkeys(reasons)) or "complete",
        )

    def _evaluate_expression(
        self,
        expression: BusinessKpiExpression,
        values: dict[str, float],
    ) -> float:
        if expression.kind == BusinessKpiExpressionKind.OPERAND:
            assert expression.operand_key is not None
            try:
                return float(values[expression.operand_key.casefold()])
            except KeyError as exc:
                raise BusinessKpiValidationError(
                    f"formula operand is unavailable: {expression.operand_key}"
                ) from exc
        if expression.kind == BusinessKpiExpressionKind.CONSTANT:
            assert expression.constant is not None
            return float(expression.constant)
        assert expression.left is not None and expression.right is not None
        left = self._evaluate_expression(expression.left, values)
        right = self._evaluate_expression(expression.right, values)
        if expression.kind == BusinessKpiExpressionKind.ADD:
            return left + right
        if expression.kind == BusinessKpiExpressionKind.SUBTRACT:
            return left - right
        if expression.kind == BusinessKpiExpressionKind.MULTIPLY:
            return left * right
        if expression.kind == BusinessKpiExpressionKind.DIVIDE:
            if right == 0:
                raise BusinessKpiValidationError(
                    "business KPI formula division by zero"
                )
            return left / right
        raise BusinessKpiValidationError(
            f"unsupported expression kind: {expression.kind}"
        )

    @staticmethod
    def _target(
        definition: BusinessKpiDefinition,
        value: float,
    ) -> BusinessKpiTargetEvaluation | None:
        target = definition.target
        if target is None:
            return None
        if target.operator == MetricThresholdOperator.EQ:
            passed = value == target.value
        elif target.operator == MetricThresholdOperator.GTE:
            passed = value >= target.value
        elif target.operator == MetricThresholdOperator.LTE:
            passed = value <= target.value
        else:
            raise BusinessKpiValidationError(
                f"unsupported target operator: {target.operator}"
            )
        variance = value - target.value
        variance_percent = (
            variance / abs(target.value) * 100.0
            if target.value != 0
            else None
        )
        return BusinessKpiTargetEvaluation(
            label=target.label,
            operator=target.operator,
            target_value=target.value,
            passed=passed,
            variance=variance,
            variance_percent=variance_percent,
        )

    def _trend(
        self,
        metric_id: str,
        observation_id: str,
        value: float,
        *,
        actor: AuthenticationActor,
        current_partial: bool = False,
    ) -> BusinessKpiTrend:
        history = self.metrics.history(
            metric_id,
            scope=actor.tenant,
            limit=20,
        )
        previous = next(
            (item for item in history if item.id != observation_id),
            None,
        )
        if previous is None or type(previous.value) not in {int, float}:
            return BusinessKpiTrend()
        previous_value = float(previous.value)
        if current_partial or previous.partial:
            return BusinessKpiTrend(
                previous_observation_id=previous.id,
                previous_value=previous_value,
                previous_partial=previous.partial,
            )
        absolute = value - previous_value
        percent = (
            absolute / abs(previous_value) * 100.0
            if previous_value != 0
            else None
        )
        return BusinessKpiTrend(
            previous_observation_id=previous.id,
            previous_value=previous_value,
            previous_partial=previous.partial,
            absolute_delta=absolute,
            percent_delta=percent,
        )

    @staticmethod
    def _formula_fingerprint(
        definition: BusinessKpiDefinition,
        operands: tuple[BusinessKpiOperandAttribution, ...],
        *,
        window_end: float | None,
    ) -> str:
        payload = {
            "kpi_id": definition.id,
            "revision": definition.revision,
            "expression": definition.expression.model_dump(mode="json"),
            "operands": [
                item.model_dump(mode="json")
                for item in operands
            ],
            "window_end": window_end,
        }
        raw = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def evaluate(
        self,
        kpi_id: str,
        *,
        actor: AuthenticationActor,
        at: float | None = None,
    ) -> BusinessKpiEvaluation:
        definition = self.get(kpi_id, actor=actor)
        evaluated_at = float(self.clock()) if at is None else float(at)
        operands = tuple(
            self._evaluate_operand(
                operand,
                actor=actor,
                at=evaluated_at,
            )
            for operand in definition.operands
        )
        values = {
            item.operand_key.casefold(): item.value
            for item in operands
            if item.value is not None
        }
        blocked = [
            item for item in operands if item.value is None
        ]
        selected_fact_ids = tuple(
            dict.fromkeys(
                fact_id
                for item in operands
                for fact_id in item.selected_fact_ids
            )
        )
        reasons = tuple(
            dict.fromkeys(
                item.reason
                for item in operands
                if item.reason != "complete"
            )
        )

        if blocked:
            has_stale = any(item.stale_fact_ids for item in blocked)
            freshness = (
                MetricFreshness.STALE
                if has_stale
                else MetricFreshness.MISSING
            )
            metric = self.metrics.get_definition(
                definition.metric_id,
                scope=actor.tenant,
            )
            return BusinessKpiEvaluation(
                kpi_id=definition.id,
                kpi_revision=definition.revision,
                metric_id=metric.id,
                metric_revision=metric.revision,
                unit=definition.unit,
                freshness=freshness,
                reasons=reasons or ("required KPI operands are unavailable",),
                selected_fact_ids=selected_fact_ids,
                operand_attributions=operands,
                evaluated_at=evaluated_at,
            )

        value = self._evaluate_expression(definition.expression, values)
        partial = any(item.partial for item in operands)
        freshness = (
            MetricFreshness.PARTIAL if partial else MetricFreshness.FRESH
        )
        formula_fingerprint = self._formula_fingerprint(
            definition,
            operands,
            window_end=(
                evaluated_at if definition.window_seconds is not None else None
            ),
        )
        observation_times = [
            item.oldest_selected_at
            for item in operands
            if item.oldest_selected_at is not None
        ]
        observed_at = (
            evaluated_at
            if definition.window_seconds is not None
            else min(observation_times)
            if observation_times
            else evaluated_at
        )
        window_start = (
            evaluated_at - definition.window_seconds
            if definition.window_seconds is not None
            else None
        )
        window_end = (
            evaluated_at
            if definition.window_seconds is not None
            else None
        )
        observation = self.metrics.ingest(
            definition.metric_id,
            MetricObservationCreate(
                value=value,
                unit=definition.unit,
                observed_at=observed_at,
                window_start=window_start,
                window_end=window_end,
                source=(
                    f"business-kpi:{definition.id}:r{definition.revision}"
                ),
                external_record_ref=None,
                partial=partial,
                idempotency_key=f"business-kpi:{formula_fingerprint}",
            ),
            scope=actor.tenant,
            actor_id=actor.identity_id,
        )
        attribution = BusinessKpiObservationAttribution(
            kpi_id=definition.id,
            kpi_revision=definition.revision,
            metric_id=definition.metric_id,
            metric_revision=observation.metric_revision,
            metric_observation_id=observation.id,
            operand_attributions=operands,
            selected_fact_ids=selected_fact_ids,
            formula_fingerprint=formula_fingerprint,
            created_at=evaluated_at,
        )

        def save(state: BusinessKpiState) -> BusinessKpiState:
            if not any(
                item.metric_observation_id == observation.id
                for item in state.attributions
            ):
                state.attributions.append(attribution)
            return state

        self.store.update(save)
        return BusinessKpiEvaluation(
            kpi_id=definition.id,
            kpi_revision=definition.revision,
            metric_id=definition.metric_id,
            metric_revision=observation.metric_revision,
            value=value,
            unit=definition.unit,
            freshness=freshness,
            reasons=reasons,
            observation_id=observation.id,
            selected_fact_ids=selected_fact_ids,
            operand_attributions=operands,
            target=self._target(definition, value),
            trend=self._trend(
                definition.metric_id,
                observation.id,
                value,
                actor=actor,
                current_partial=partial,
            ),
            evaluated_at=evaluated_at,
        )

    def attribution(
        self,
        observation_id: str,
        *,
        actor: AuthenticationActor,
    ) -> BusinessKpiObservationAttribution:
        visible_ids = {item.id for item in self.list(actor=actor)}
        item = next(
            (
                row
                for row in self.store.load().attributions
                if row.metric_observation_id == observation_id
                and row.kpi_id in visible_ids
            ),
            None,
        )
        if item is None:
            raise BusinessKpiNotFoundError(
                "business KPI observation attribution not found"
            )
        return item

    def capture_exact_metric_snapshot(
        self,
        evaluation: BusinessKpiEvaluation,
        *,
        actor: AuthenticationActor,
    ):
        if evaluation.observation_id is None:
            raise BusinessKpiValidationError(
                "business KPI has no metric observation to snapshot"
            )
        return self.metrics.capture_observation_snapshot(
            evaluation.metric_id,
            evaluation.observation_id,
            scope=actor.tenant,
            actor_id=actor.identity_id,
            captured_at=evaluation.evaluated_at,
        )

    def operating_snapshot(
        self,
        *,
        actor: AuthenticationActor,
    ) -> BusinessKpiOperatingSnapshot:
        captured_at = float(self.clock())
        items: list[BusinessKpiOperatingItem] = []
        for definition in self.list(actor=actor):
            evaluation = self.evaluate(
                definition.id,
                actor=actor,
                at=captured_at,
            )
            metric_snapshot = (
                self.capture_exact_metric_snapshot(
                    evaluation,
                    actor=actor,
                )
                if evaluation.observation_id is not None
                else None
            )
            freshness = (
                metric_snapshot.freshness
                if metric_snapshot is not None
                and metric_snapshot.freshness != MetricFreshness.FRESH
                else evaluation.freshness
            )
            items.append(
                BusinessKpiOperatingItem(
                    kpi_id=definition.id,
                    kpi_revision=definition.revision,
                    name=definition.name,
                    domain=definition.domain,
                    metric_id=definition.metric_id,
                    metric_revision=evaluation.metric_revision,
                    metric_snapshot_id=(
                        metric_snapshot.id
                        if metric_snapshot is not None
                        else None
                    ),
                    observation_ids=(
                        metric_snapshot.observation_ids
                        if metric_snapshot is not None
                        else ()
                    ),
                    value=evaluation.value,
                    unit=evaluation.unit,
                    freshness=freshness,
                    reasons=evaluation.reasons,
                    selected_fact_ids=evaluation.selected_fact_ids,
                    target=evaluation.target,
                    trend=evaluation.trend,
                )
            )
        snapshot = BusinessKpiOperatingSnapshot(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            items=tuple(items),
            captured_by=actor.identity_id,
            captured_at=captured_at,
        )

        def save(state: BusinessKpiState) -> BusinessKpiState:
            state.operating_snapshots.append(snapshot)
            state.operating_snapshots = state.operating_snapshots[-500:]
            return state

        self.store.update(save)
        return snapshot

    def operating_snapshots(
        self,
        *,
        actor: AuthenticationActor,
        limit: int = 20,
    ) -> tuple[BusinessKpiOperatingSnapshot, ...]:
        if limit < 1 or limit > 100:
            raise BusinessKpiValidationError(
                "operating snapshot limit must be between 1 and 100"
            )
        rows = [
            item
            for item in self.store.load().operating_snapshots
            if item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        ]
        rows.sort(key=lambda item: (item.captured_at, item.id), reverse=True)
        return tuple(rows[:limit])

    @staticmethod
    def _require_binding_freshness(
        evaluation: BusinessKpiEvaluation,
        *,
        allow_partial: bool,
    ) -> None:
        allowed = {MetricFreshness.FRESH}
        if allow_partial:
            allowed.add(MetricFreshness.PARTIAL)
        if evaluation.freshness not in allowed:
            raise BusinessKpiValidationError(
                f"KPI freshness {evaluation.freshness.value} cannot be bound"
            )

    def bind_goal(
        self,
        kpi_id: str,
        goal_id: str,
        *,
        actor: AuthenticationActor,
        description: str | None = None,
        operator: MetricThresholdOperator | None = None,
        target_value: float | None = None,
        allow_partial: bool = False,
    ):
        if self.goals is None:
            raise BusinessKpiValidationError("GoalService is unavailable")
        definition = self.get(kpi_id, actor=actor)
        evaluation = self.evaluate(kpi_id, actor=actor)
        self._require_binding_freshness(
            evaluation,
            allow_partial=allow_partial,
        )
        snapshot = self.capture_exact_metric_snapshot(
            evaluation,
            actor=actor,
        )
        target = definition.target
        effective_operator = operator or (
            target.operator if target is not None else None
        )
        effective_value = (
            target_value
            if target_value is not None
            else target.value
            if target is not None
            else None
        )
        if effective_operator is None or effective_value is None:
            raise BusinessKpiValidationError(
                "Goal binding requires a KPI target or explicit operator/value"
            )
        goal = self.goals.get(goal_id, scope=actor.tenant)
        criterion_id = f"goal-business-kpi-{definition.id}"
        criterion = GoalSuccessCriterion(
            id=criterion_id,
            description=description or definition.name,
            kind=GoalCriterionKind.METRIC,
            metric_id=definition.metric_id,
            metric_snapshot_id=snapshot.id,
            metric_window_seconds=definition.window_seconds,
            operator=GoalCriterionOperator(effective_operator.value),
            target_value=effective_value,
            unit=definition.unit,
        )
        criteria = tuple(
            item
            for item in goal.success_criteria
            if item.id != criterion_id
        ) + (criterion,)
        return self.goals.revise(
            goal_id,
            GoalUpdate(
                success_criteria=criteria,
                reason=f"bind business KPI {definition.id} snapshot {snapshot.id}",
            ),
            scope=actor.tenant,
            actor_id=actor.identity_id,
        )

    async def bind_decision(
        self,
        kpi_id: str,
        decision_id: str,
        *,
        actor: AuthenticationActor,
        summary: str | None = None,
        allow_partial: bool = False,
    ):
        if self.decisions is None:
            raise BusinessKpiValidationError(
                "DecisionService is unavailable"
            )
        definition = self.get(kpi_id, actor=actor)
        evaluation = self.evaluate(kpi_id, actor=actor)
        self._require_binding_freshness(
            evaluation,
            allow_partial=allow_partial,
        )
        snapshot = self.capture_exact_metric_snapshot(
            evaluation,
            actor=actor,
        )
        decision = self.decisions.get(decision_id, actor=actor)
        inputs: list[DecisionEvidenceInput] = []
        for item in decision.evidence:
            if (
                item.kind == DecisionEvidenceKind.METRIC_SNAPSHOT
                and item.metric_id == definition.metric_id
            ):
                continue
            if item.kind == DecisionEvidenceKind.EVIDENCE:
                inputs.append(
                    DecisionEvidenceInput(
                        kind=item.kind,
                        evidence_id=item.evidence_id,
                        summary=item.summary,
                    )
                )
            else:
                inputs.append(
                    DecisionEvidenceInput(
                        kind=item.kind,
                        metric_id=item.metric_id,
                        metric_snapshot_id=item.metric_snapshot_id,
                        summary=item.summary,
                    )
                )
        inputs.append(
            DecisionEvidenceInput(
                kind=DecisionEvidenceKind.METRIC_SNAPSHOT,
                metric_id=definition.metric_id,
                metric_snapshot_id=snapshot.id,
                summary=summary or definition.name,
            )
        )
        return await self.decisions.revise(
            decision_id,
            DecisionUpdate(
                evidence=tuple(inputs),
                reason=(
                    f"bind business KPI {definition.id} "
                    f"snapshot {snapshot.id}"
                ),
                expected_revision=getattr(decision, "revision", None),
            ),
            actor=actor,
        )
