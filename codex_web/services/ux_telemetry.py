from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict
from typing import Any

from codex_web.configuration import ConfigurationContext
from codex_web.identity import TenantScope
from codex_web.services.configuration import ConfigurationService
from codex_web.services.ux_telemetry_configuration import (
    UX_TELEMETRY_ENABLED_CONFIG,
    UX_TELEMETRY_RETENTION_DAYS_CONFIG,
)
from codex_web.storage.ux_telemetry import UxTelemetryStore
from codex_web.ux_telemetry import (
    UX_TELEMETRY_TAXONOMY_VERSION,
    UxEventName,
    UxTelemetryEvent,
    UxTelemetryEventCreate,
    UxWorkflow,
)


class UxTelemetryService:
    """Privacy-bounded product analytics, separate from canonical application state."""

    MAX_EVENTS = 20_000

    def __init__(
        self,
        store: UxTelemetryStore,
        configuration: ConfigurationService,
    ) -> None:
        self.store = store
        self.configuration = configuration

    @staticmethod
    def _context(scope: TenantScope) -> ConfigurationContext:
        return ConfigurationContext(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
        )

    def policy(self, scope: TenantScope) -> dict[str, Any]:
        context = self._context(scope)
        enabled = self.configuration.resolve(
            UX_TELEMETRY_ENABLED_CONFIG,
            context,
        )
        retention = self.configuration.resolve(
            UX_TELEMETRY_RETENTION_DAYS_CONFIG,
            context,
        )
        return {
            "schema_version": UX_TELEMETRY_TAXONOMY_VERSION,
            "enabled": bool(enabled.value),
            "retention_days": int(retention.value),
            "enabled_source": enabled.source,
            "retention_source": retention.source,
        }

    @staticmethod
    def _visible(event: UxTelemetryEvent, scope: TenantScope) -> bool:
        return (
            event.organization_id == scope.organization_id
            and event.workspace_id == scope.workspace_id
        )

    def _cutoff(self, scope: TenantScope, now: float) -> float:
        return now - (self.policy(scope)["retention_days"] * 86_400)

    def ingest(
        self,
        payload: UxTelemetryEventCreate,
        *,
        scope: TenantScope,
        now: float | None = None,
    ) -> UxTelemetryEvent | None:
        policy = self.policy(scope)
        if not policy["enabled"]:
            return None
        recorded_at = time.time() if now is None else float(now)
        cutoff = recorded_at - (policy["retention_days"] * 86_400)
        event = UxTelemetryEvent(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            recorded_at=recorded_at,
            **payload.model_dump(mode="python"),
        )

        def apply(state):
            state.events = [
                item
                for item in state.events
                if not self._visible(item, scope) or item.recorded_at >= cutoff
            ]
            state.events.append(event)
            if len(state.events) > self.MAX_EVENTS:
                state.events = sorted(state.events, key=lambda item: item.recorded_at)[-self.MAX_EVENTS:]
            return state

        self.store.update(apply)
        return event

    def events(
        self,
        *,
        scope: TenantScope,
        start_at: float | None = None,
        end_at: float | None = None,
        limit: int = 500,
        now: float | None = None,
    ) -> list[UxTelemetryEvent]:
        current = time.time() if now is None else float(now)
        cutoff = self._cutoff(scope, current)
        rows = [
            item
            for item in self.store.load().events
            if self._visible(item, scope)
            and item.recorded_at >= cutoff
            and (start_at is None or item.recorded_at >= start_at)
            and (end_at is None or item.recorded_at <= end_at)
        ]
        return sorted(rows, key=lambda item: item.recorded_at, reverse=True)[:limit]

    def summary(
        self,
        *,
        scope: TenantScope,
        start_at: float | None = None,
        end_at: float | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        rows = self.events(
            scope=scope,
            start_at=start_at,
            end_at=end_at,
            limit=self.MAX_EVENTS,
            now=now,
        )
        names = Counter(item.event_name.value for item in rows)
        workflows: dict[str, dict[str, Any]] = {}
        for workflow in UxWorkflow:
            selected = [item for item in rows if item.workflow == workflow]
            counts = Counter(item.event_name.value for item in selected)
            started = counts[UxEventName.WORKFLOW_STARTED.value]
            if workflow == UxWorkflow.ONBOARDING:
                started += counts[UxEventName.ONBOARDING_STARTED.value]
                completed = counts[UxEventName.ONBOARDING_COMPLETED.value]
                abandoned = counts[UxEventName.ONBOARDING_SKIPPED.value]
            else:
                completed = counts[UxEventName.WORKFLOW_COMPLETED.value]
                abandoned = counts[UxEventName.WORKFLOW_ABANDONED.value]
            durations = [
                item.duration_ms
                for item in selected
                if item.duration_ms is not None
                and item.event_name in {UxEventName.WORKFLOW_COMPLETED, UxEventName.ONBOARDING_COMPLETED}
            ]
            workflows[workflow.value] = {
                "started": started,
                "completed": completed,
                "abandoned": abandoned,
                "failed_actions": counts[UxEventName.ACTION_FAILED.value],
                "validation_errors": counts[UxEventName.VALIDATION_ERROR_SHOWN.value],
                "recoveries": counts[UxEventName.RECOVERY_ACTION_USED.value],
                "completion_rate": (completed / started) if started else None,
                "median_completion_ms": statistics.median(durations) if durations else None,
            }

        friction: Counter[tuple[str, str]] = Counter()
        friction_names = {
            UxEventName.WORKFLOW_ABANDONED,
            UxEventName.ONBOARDING_SKIPPED,
            UxEventName.ACTION_FAILED,
            UxEventName.VALIDATION_ERROR_SHOWN,
            UxEventName.RECOVERY_ACTION_USED,
        }
        for item in rows:
            if item.event_name in friction_names:
                friction[(item.workflow.value, item.step or "unspecified")] += 1
        top = friction.most_common(1)
        onboarding_durations = [
            item.duration_ms
            for item in rows
            if item.event_name == UxEventName.ONBOARDING_COMPLETED
            and item.duration_ms is not None
        ]
        activation_started = names[UxEventName.ONBOARDING_STARTED.value]
        activation_completed = names[UxEventName.ONBOARDING_COMPLETED.value]

        navigation_rows = [
            item
            for item in rows
            if item.event_name == UxEventName.ROUTE_TRANSITION
            and item.route_group is not None
            and item.journey_id is not None
        ]
        journeys: dict[str, list[UxTelemetryEvent]] = defaultdict(list)
        for item in navigation_rows:
            journeys[item.journey_id].append(item)
        backtracks = 0
        for journey_rows in journeys.values():
            ordered = sorted(journey_rows, key=lambda item: item.recorded_at)
            routes = [item.route_group.value for item in ordered if item.route_group]
            backtracks += sum(
                1
                for index in range(2, len(routes))
                if routes[index] == routes[index - 2] and routes[index] != routes[index - 1]
            )
        route_counts = Counter(
            item.route_group.value for item in navigation_rows if item.route_group
        )

        return {
            "schema_version": UX_TELEMETRY_TAXONOMY_VERSION,
            "start_at": start_at,
            "end_at": end_at,
            "event_count": len(rows),
            "activation": {
                "started": activation_started,
                "completed": activation_completed,
                "skipped": names[UxEventName.ONBOARDING_SKIPPED.value],
                "completion_rate": (
                    activation_completed / activation_started
                    if activation_started else None
                ),
                "median_time_to_first_meaningful_outcome_ms": (
                    statistics.median(onboarding_durations)
                    if onboarding_durations else None
                ),
            },
            "workflows": workflows,
            "navigation": {
                "transitions": len(navigation_rows),
                "backtracks": backtracks,
                "route_counts": dict(sorted(route_counts.items())),
            },
            "highest_friction_step": (
                {
                    "workflow": top[0][0][0],
                    "step": top[0][0][1],
                    "friction_events": top[0][1],
                }
                if top else None
            ),
        }
