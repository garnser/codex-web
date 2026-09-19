from __future__ import annotations

import time

from codex_web.identity import TenantScope
from codex_web.metrics import (
    MetricAggregation,
    MetricDefinition,
    MetricDefinitionCreate,
    MetricDefinitionRevision,
    MetricDefinitionUpdate,
    MetricEvaluation,
    MetricFreshness,
    MetricObservation,
    MetricObservationCreate,
    MetricSnapshot,
    MetricSnapshotRequest,
    MetricState,
    MetricValueType,
)
from codex_web.storage.metrics import MetricStore


class MetricError(RuntimeError):
    pass


class MetricNotFoundError(MetricError):
    pass


class MetricConflictError(MetricError):
    pass


class MetricValidationError(MetricError):
    pass


class MetricService:
    """Deterministic metric definitions, observations, aggregation and snapshots."""

    MAX_HISTORY = 500

    def __init__(self, store: MetricStore) -> None:
        self.store = store

    @staticmethod
    def _visible(item, scope: TenantScope) -> bool:
        return (
            item.organization_id == scope.organization_id
            and item.workspace_id == scope.workspace_id
        )

    def _definition(
        self,
        state: MetricState,
        metric_id: str,
        scope: TenantScope,
    ) -> MetricDefinition:
        item = next(
            (
                row
                for row in state.definitions
                if row.id == metric_id and self._visible(row, scope)
            ),
            None,
        )
        if item is None:
            raise MetricNotFoundError("metric not found")
        return item

    @staticmethod
    def _validate_value(definition: MetricDefinition, value) -> None:
        if definition.value_type == MetricValueType.BOOLEAN:
            if type(value) is not bool:
                raise MetricValidationError("boolean metric requires a boolean value")
            return
        if definition.value_type == MetricValueType.INTEGER:
            if type(value) is not int:
                raise MetricValidationError("integer metric requires an integer value")
            return
        if type(value) not in {int, float}:
            raise MetricValidationError("numeric metric requires a number")

    @staticmethod
    def _revision(
        definition: MetricDefinition,
        *,
        actor_id: str,
        reason: str,
        at: float,
    ) -> MetricDefinitionRevision:
        return MetricDefinitionRevision(
            metric_id=definition.id,
            revision=definition.revision,
            snapshot=definition.model_copy(deep=True),
            reason=reason,
            revised_by=actor_id,
            revised_at=at,
        )

    def create_definition(
        self,
        payload: MetricDefinitionCreate,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> MetricDefinition:
        now = time.time()
        item = MetricDefinition(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            key=payload.key,
            name=payload.name,
            description=payload.description,
            owner_identity_id=payload.owner_identity_id,
            unit=payload.unit,
            value_type=payload.value_type,
            aggregation=payload.aggregation,
            window_seconds=payload.window_seconds,
            freshness_seconds=payload.freshness_seconds,
            direction=payload.direction,
            source_requirements=payload.source_requirements,
            thresholds=payload.thresholds,
            project_id=payload.project_id,
            resource_id=payload.resource_id,
            created_by=actor_id,
            updated_by=actor_id,
            created_at=now,
            updated_at=now,
        )

        def apply(state: MetricState) -> MetricState:
            duplicate = next(
                (
                    row
                    for row in state.definitions
                    if self._visible(row, scope) and row.key.casefold() == item.key.casefold()
                ),
                None,
            )
            if duplicate is not None:
                raise MetricConflictError("metric key already exists in workspace")
            state.definitions.append(item)
            state.revisions.append(
                self._revision(
                    item,
                    actor_id=actor_id,
                    reason="metric definition created",
                    at=now,
                )
            )
            return state

        self.store.update(apply)
        return item

    def update_definition(
        self,
        metric_id: str,
        payload: MetricDefinitionUpdate,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> MetricDefinition:
        result: MetricDefinition | None = None

        def apply(state: MetricState) -> MetricState:
            nonlocal result
            current = self._definition(state, metric_id, scope)
            changes = payload.model_dump(
                mode="python",
                exclude={"reason"},
                exclude_unset=True,
            )
            if not changes:
                raise MetricConflictError("metric definition update contains no changes")
            data = current.model_dump(mode="python")
            data.update(changes)
            data.update(
                revision=current.revision + 1,
                updated_by=actor_id,
                updated_at=time.time(),
            )
            candidate = MetricDefinition.model_validate(data)
            index = state.definitions.index(current)
            state.definitions[index] = candidate
            state.revisions.append(
                self._revision(
                    candidate,
                    actor_id=actor_id,
                    reason=payload.reason,
                    at=candidate.updated_at,
                )
            )
            result = candidate
            return state

        self.store.update(apply)
        assert result is not None
        return result

    def get_definition(
        self,
        metric_id: str,
        *,
        scope: TenantScope,
    ) -> MetricDefinition:
        return self._definition(self.store.load(), metric_id, scope)

    def list_definitions(
        self,
        *,
        scope: TenantScope,
        project_id: str | None = None,
        resource_id: str | None = None,
    ) -> tuple[MetricDefinition, ...]:
        rows = [
            item
            for item in self.store.load().definitions
            if self._visible(item, scope)
        ]
        if project_id is not None:
            rows = [item for item in rows if item.project_id == project_id]
        if resource_id is not None:
            rows = [item for item in rows if item.resource_id == resource_id]
        rows.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return tuple(rows)

    def revisions(
        self,
        metric_id: str,
        *,
        scope: TenantScope,
    ) -> tuple[MetricDefinitionRevision, ...]:
        state = self.store.load()
        self._definition(state, metric_id, scope)
        rows = [row for row in state.revisions if row.metric_id == metric_id]
        rows.sort(key=lambda row: row.revision)
        return tuple(rows)

    def ingest(
        self,
        metric_id: str,
        payload: MetricObservationCreate,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> MetricObservation:
        result: MetricObservation | None = None

        def apply(state: MetricState) -> MetricState:
            nonlocal result
            definition = self._definition(state, metric_id, scope)
            self._validate_value(definition, payload.value)
            unit = payload.unit or definition.unit
            if unit != definition.unit:
                raise MetricValidationError(
                    f"metric unit mismatch: expected {definition.unit}, got {unit}"
                )
            if (
                definition.source_requirements
                and payload.source not in definition.source_requirements
            ):
                raise MetricValidationError("metric observation source is not permitted")

            observed_at = (
                payload.observed_at
                if payload.observed_at is not None
                else time.time()
            )
            existing = next(
                (
                    row
                    for row in state.observations
                    if row.metric_id == metric_id
                    and self._visible(row, scope)
                    and row.idempotency_key == payload.idempotency_key
                ),
                None,
            )
            if existing is not None:
                same_value = (
                    existing.value is payload.value
                    if type(existing.value) is bool or type(payload.value) is bool
                    else float(existing.value) == float(payload.value)
                )
                same_request = (
                    same_value
                    and existing.unit == unit
                    and (
                        payload.observed_at is None
                        or existing.observed_at == payload.observed_at
                    )
                    and existing.window_start == payload.window_start
                    and existing.window_end == payload.window_end
                    and existing.source == payload.source
                    and existing.provider == payload.provider
                    and existing.external_record_ref == payload.external_record_ref
                    and tuple(existing.evidence_ids)
                    == tuple(dict.fromkeys(payload.evidence_ids))
                    and existing.partial == payload.partial
                )
                if not same_request:
                    raise MetricConflictError(
                        "metric idempotency key was reused with different content"
                    )
                result = existing
                return state

            item = MetricObservation(
                organization_id=scope.organization_id,
                workspace_id=scope.workspace_id,
                metric_id=definition.id,
                metric_revision=definition.revision,
                value=payload.value,
                unit=unit,
                observed_at=observed_at,
                window_start=payload.window_start,
                window_end=payload.window_end,
                source=payload.source,
                provider=payload.provider,
                external_record_ref=payload.external_record_ref,
                evidence_ids=tuple(dict.fromkeys(payload.evidence_ids)),
                partial=payload.partial,
                idempotency_key=payload.idempotency_key,
                ingested_by=actor_id,
            )
            state.observations.append(item)
            result = item
            return state

        self.store.update(apply)
        assert result is not None
        return result

    def history(
        self,
        metric_id: str,
        *,
        scope: TenantScope,
        limit: int = 100,
        start_at: float | None = None,
        end_at: float | None = None,
    ) -> tuple[MetricObservation, ...]:
        if limit < 1 or limit > self.MAX_HISTORY:
            raise MetricValidationError(
                f"metric history limit must be between 1 and {self.MAX_HISTORY}"
            )
        state = self.store.load()
        self._definition(state, metric_id, scope)
        rows = [
            row
            for row in state.observations
            if row.metric_id == metric_id and self._visible(row, scope)
        ]
        if start_at is not None:
            rows = [row for row in rows if row.observed_at >= start_at]
        if end_at is not None:
            rows = [row for row in rows if row.observed_at <= end_at]
        rows.sort(key=lambda row: (row.observed_at, row.id), reverse=True)
        return tuple(rows[:limit])

    @staticmethod
    def _aggregate(
        definition: MetricDefinition,
        observations: list[MetricObservation],
    ) -> tuple[float | int | bool | None, tuple[str, ...]]:
        if not observations:
            return None, ()
        rows = sorted(observations, key=lambda row: (row.observed_at, row.id))
        if definition.aggregation == MetricAggregation.LAST:
            newest = rows[-1]
            return newest.value, (newest.id,)
        if definition.aggregation == MetricAggregation.COUNT:
            return len(rows), tuple(row.id for row in rows)

        values = [row.value for row in rows]
        if any(type(value) not in {int, float} for value in values):
            raise MetricValidationError("numeric aggregation received non-numeric value")
        if definition.aggregation == MetricAggregation.SUM:
            value = sum(values)
        elif definition.aggregation == MetricAggregation.AVERAGE:
            value = sum(values) / len(values)
        elif definition.aggregation == MetricAggregation.MINIMUM:
            value = min(values)
        elif definition.aggregation == MetricAggregation.MAXIMUM:
            value = max(values)
        else:
            raise MetricValidationError(
                f"unsupported metric aggregation: {definition.aggregation}"
            )
        return value, tuple(row.id for row in rows)

    def evaluate(
        self,
        metric_id: str,
        *,
        scope: TenantScope,
        at: float | None = None,
        window_start: float | None = None,
        window_end: float | None = None,
    ) -> MetricEvaluation:
        state = self.store.load()
        definition = self._definition(state, metric_id, scope)
        evaluated_at = time.time() if at is None else at
        end = evaluated_at if window_end is None else window_end
        start = window_start
        if start is None and definition.window_seconds is not None:
            start = end - definition.window_seconds
        if start is not None and end < start:
            raise MetricValidationError("metric evaluation window_end must be >= window_start")

        rows = [
            row
            for row in state.observations
            if row.metric_id == metric_id
            and self._visible(row, scope)
            and row.observed_at <= end
            and (start is None or row.observed_at >= start)
        ]
        value, used_ids = self._aggregate(definition, rows)
        used = [row for row in rows if row.id in set(used_ids)]
        newest_at = max((row.observed_at for row in used), default=None)

        if not used:
            freshness = MetricFreshness.MISSING
            reason = "no observation is available in the requested window"
        elif any(row.partial for row in used):
            freshness = MetricFreshness.PARTIAL
            reason = "one or more observations are explicitly partial"
        elif newest_at is not None and evaluated_at - newest_at > definition.freshness_seconds:
            freshness = MetricFreshness.STALE
            reason = (
                f"newest observation is older than the {definition.freshness_seconds}s "
                "freshness policy"
            )
        else:
            freshness = MetricFreshness.FRESH
            reason = "observations satisfy the metric freshness policy"

        return MetricEvaluation(
            metric_id=definition.id,
            metric_revision=definition.revision,
            observation_ids=used_ids,
            window_start=start,
            window_end=end,
            aggregation=definition.aggregation,
            value=value,
            unit=definition.unit,
            freshness=freshness,
            freshness_reason=reason,
            newest_observation_at=newest_at,
            evaluated_at=evaluated_at,
        )

    def capture_snapshot(
        self,
        metric_id: str,
        payload: MetricSnapshotRequest,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> MetricSnapshot:
        evaluation = self.evaluate(
            metric_id,
            scope=scope,
            window_start=payload.window_start,
            window_end=payload.window_end,
        )
        snapshot = MetricSnapshot(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            metric_id=evaluation.metric_id,
            metric_revision=evaluation.metric_revision,
            observation_ids=evaluation.observation_ids,
            window_start=evaluation.window_start,
            window_end=evaluation.window_end,
            aggregation=evaluation.aggregation,
            value=evaluation.value,
            unit=evaluation.unit,
            freshness=evaluation.freshness,
            freshness_reason=evaluation.freshness_reason,
            newest_observation_at=evaluation.newest_observation_at,
            captured_by=actor_id,
            captured_at=evaluation.evaluated_at,
        )

        def apply(state: MetricState) -> MetricState:
            self._definition(state, metric_id, scope)
            state.snapshots.append(snapshot)
            return state

        self.store.update(apply)
        return snapshot

    def capture_observation_snapshot(
        self,
        metric_id: str,
        observation_id: str,
        *,
        scope: TenantScope,
        actor_id: str,
        captured_at: float | None = None,
    ) -> MetricSnapshot:
        state = self.store.load()
        definition = self._definition(state, metric_id, scope)
        observation = next(
            (
                row
                for row in state.observations
                if row.id == observation_id
                and row.metric_id == metric_id
                and self._visible(row, scope)
            ),
            None,
        )
        if observation is None:
            raise MetricNotFoundError("metric observation not found")
        now = time.time() if captured_at is None else float(captured_at)
        if observation.partial:
            freshness = MetricFreshness.PARTIAL
            reason = "selected metric observation is explicitly partial"
        elif now - observation.observed_at > definition.freshness_seconds:
            freshness = MetricFreshness.STALE
            reason = (
                f"selected observation is older than the "
                f"{definition.freshness_seconds}s freshness policy"
            )
        else:
            freshness = MetricFreshness.FRESH
            reason = "selected observation satisfies the metric freshness policy"
        snapshot = MetricSnapshot(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            metric_id=metric_id,
            metric_revision=observation.metric_revision,
            observation_ids=(observation.id,),
            window_start=observation.window_start,
            window_end=observation.window_end,
            aggregation=definition.aggregation,
            value=observation.value,
            unit=observation.unit,
            freshness=freshness,
            freshness_reason=reason,
            newest_observation_at=observation.observed_at,
            captured_by=actor_id,
            captured_at=now,
        )

        def apply(current: MetricState) -> MetricState:
            self._definition(current, metric_id, scope)
            if not any(
                row.id == observation_id
                and row.metric_id == metric_id
                and self._visible(row, scope)
                for row in current.observations
            ):
                raise MetricNotFoundError("metric observation not found")
            current.snapshots.append(snapshot)
            return current

        self.store.update(apply)
        return snapshot

    def get_snapshot(
        self,
        metric_id: str,
        snapshot_id: str,
        *,
        scope: TenantScope,
    ) -> MetricSnapshot:
        state = self.store.load()
        self._definition(state, metric_id, scope)
        snapshot = next(
            (
                row
                for row in state.snapshots
                if row.id == snapshot_id
                and row.metric_id == metric_id
                and self._visible(row, scope)
            ),
            None,
        )
        if snapshot is None:
            raise MetricNotFoundError("metric snapshot not found")
        return snapshot

    def snapshots(
        self,
        metric_id: str,
        *,
        scope: TenantScope,
        limit: int = 100,
    ) -> tuple[MetricSnapshot, ...]:
        if limit < 1 or limit > self.MAX_HISTORY:
            raise MetricValidationError(
                f"metric snapshot limit must be between 1 and {self.MAX_HISTORY}"
            )
        state = self.store.load()
        self._definition(state, metric_id, scope)
        rows = [
            row
            for row in state.snapshots
            if row.metric_id == metric_id and self._visible(row, scope)
        ]
        rows.sort(key=lambda row: (row.captured_at, row.id), reverse=True)
        return tuple(rows[:limit])
