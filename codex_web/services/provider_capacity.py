from __future__ import annotations

import inspect
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

from codex_web.canonical_events import CanonicalEventType
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.identity import AuthenticationActor
from codex_web.provider_capacity import (
    ProviderCapacityRecord,
    ProviderCapacityReport,
    ProviderCapacityStatus,
    ProviderCapacityWait,
    ProviderCapacityWaitCreate,
    ProviderCapacityWaitStatus,
)
from codex_web.scheduler import MisfirePolicy, ScheduleCreate
from codex_web.services.canonical_events import CanonicalEventBus
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.provider_capacity import ProviderCapacityStore


Clock = Callable[[], float]
CapacityProbe = Callable[[], Awaitable[dict[str, Any]] | dict[str, Any]]
ResumeHandler = Callable[[ProviderCapacityWait], Awaitable[None] | None]


class ProviderCapacityBlockedError(RuntimeError):
    def __init__(self, record: ProviderCapacityRecord) -> None:
        self.record = record
        self.retry_at = record.retry_at
        detail = f"{record.key} capacity is {record.status.value}"
        if record.retry_at is not None:
            detail += f" until {record.retry_at:.3f}"
        if record.reason:
            detail += f": {record.reason}"
        super().__init__(detail)


class ProviderCapacityService:
    RESUME_TRIGGER_TYPE = "provider-capacity.resume"

    def __init__(
        self,
        store: ProviderCapacityStore,
        *,
        scheduler: SchedulerService | None = None,
        clock: Clock = time.time,
        min_probe_interval_seconds: float = 60.0,
        default_retry_seconds: float = 300.0,
    ) -> None:
        self.store = store
        self.scheduler = scheduler
        self.clock = clock
        self.min_probe_interval_seconds = max(1.0, float(min_probe_interval_seconds))
        self.default_retry_seconds = max(1.0, float(default_retry_seconds))
        self._probes: dict[tuple[str, str | None], CapacityProbe] = {}
        self._resume_handlers: list[ResumeHandler] = []

    @staticmethod
    def _same_scope(item: Any, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _key(provider_id: str, runtime_id: str | None) -> tuple[str, str | None]:
        provider = str(provider_id or "").strip()
        runtime = str(runtime_id or "").strip() or None
        if not provider:
            raise ValueError("provider_id must not be empty")
        return provider, runtime

    def register_probe(
        self,
        provider_id: str,
        runtime_id: str | None,
        probe: CapacityProbe,
    ) -> None:
        self._probes[self._key(provider_id, runtime_id)] = probe

    def register_resume_handler(self, handler: ResumeHandler) -> None:
        if handler not in self._resume_handlers:
            self._resume_handlers.append(handler)

    def _record(
        self,
        provider_id: str,
        runtime_id: str | None,
        actor: AuthenticationActor,
    ) -> ProviderCapacityRecord | None:
        provider_id, runtime_id = self._key(provider_id, runtime_id)
        return next(
            (
                item
                for item in self.store.load().records
                if item.provider_id == provider_id
                and item.runtime_id == runtime_id
                and self._same_scope(item, actor)
            ),
            None,
        )

    def _expire_if_due(
        self,
        record: ProviderCapacityRecord | None,
        *,
        actor: AuthenticationActor,
    ) -> ProviderCapacityRecord | None:
        if record is None:
            return None
        now = float(self.clock())
        if (
            record.status == ProviderCapacityStatus.AVAILABLE
            or record.retry_at is None
            or record.retry_at > now
        ):
            return record
        return self.report(
            ProviderCapacityReport(
                provider_id=record.provider_id,
                runtime_id=record.runtime_id,
                status=ProviderCapacityStatus.AVAILABLE,
                reason="capacity cooldown/reset elapsed; provider is eligible for probe",
                source="capacity-expiry",
                metadata={"previous_status": record.status.value},
                observed_at=now,
            ),
            actor=actor,
        )

    def get(
        self,
        provider_id: str,
        runtime_id: str | None,
        *,
        actor: AuthenticationActor,
    ) -> ProviderCapacityRecord | None:
        return self._expire_if_due(
            self._record(provider_id, runtime_id, actor),
            actor=actor,
        )

    def list(self, actor: AuthenticationActor) -> list[ProviderCapacityRecord]:
        records = [
            item
            for item in self.store.load().records
            if self._same_scope(item, actor)
        ]
        refreshed: list[ProviderCapacityRecord] = []
        for item in records:
            refreshed.append(self._expire_if_due(item, actor=actor) or item)
        return sorted(
            refreshed,
            key=lambda item: (item.provider_id, item.runtime_id or ""),
        )

    def list_waits(
        self,
        actor: AuthenticationActor,
        *,
        include_terminal: bool = True,
    ) -> list[ProviderCapacityWait]:
        rows = [
            item
            for item in self.store.load().waits
            if self._same_scope(item, actor)
            and (
                include_terminal
                or item.status == ProviderCapacityWaitStatus.WAITING
            )
        ]
        rows.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return rows

    def report(
        self,
        payload: ProviderCapacityReport,
        *,
        actor: AuthenticationActor,
    ) -> ProviderCapacityRecord:
        provider_id, runtime_id = self._key(
            payload.provider_id,
            payload.runtime_id,
        )
        now = float(self.clock())
        saved: list[ProviderCapacityRecord] = []

        def apply(state):
            existing = next(
                (
                    item
                    for item in state.records
                    if item.provider_id == provider_id
                    and item.runtime_id == runtime_id
                    and self._same_scope(item, actor)
                ),
                None,
            )
            failures = 0
            last_success_at = existing.last_success_at if existing else None
            if payload.status == ProviderCapacityStatus.AVAILABLE:
                last_success_at = payload.observed_at
            else:
                failures = (existing.consecutive_failures if existing else 0) + 1
            record = ProviderCapacityRecord(
                **payload.model_dump(mode="python"),
                provider_id=provider_id,
                runtime_id=runtime_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                consecutive_failures=failures,
                last_success_at=last_success_at,
                updated_at=now,
            )
            state.records = [
                item
                for item in state.records
                if not (
                    item.provider_id == provider_id
                    and item.runtime_id == runtime_id
                    and self._same_scope(item, actor)
                )
            ]
            state.records.append(record)
            saved.append(record)
            return state

        self.store.update(apply)
        return saved[0]

    def mark_available(
        self,
        provider_id: str,
        runtime_id: str | None,
        *,
        actor: AuthenticationActor,
        source: str = "provider-success",
        metadata: dict[str, Any] | None = None,
    ) -> ProviderCapacityRecord:
        return self.report(
            ProviderCapacityReport(
                provider_id=provider_id,
                runtime_id=runtime_id,
                status=ProviderCapacityStatus.AVAILABLE,
                source=source,
                metadata=dict(metadata or {}),
                observed_at=float(self.clock()),
            ),
            actor=actor,
        )

    def blocking_record(
        self,
        provider_id: str,
        runtime_id: str | None,
        *,
        actor: AuthenticationActor,
    ) -> ProviderCapacityRecord | None:
        now = float(self.clock())
        runtime = self.get(provider_id, runtime_id, actor=actor)
        if runtime is not None and runtime.blocks(now):
            return runtime
        provider = self.get(provider_id, None, actor=actor)
        if provider is not None and provider.blocks(now):
            return provider
        return None

    @staticmethod
    def _headers(exc: Exception) -> Mapping[str, Any]:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
        return headers if isinstance(headers, Mapping) else {}

    @staticmethod
    def _parse_retry_value(value: Any, now: float) -> float | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            number = float(text)
            if number > 10_000_000:
                return number
            return now + max(0.0, number)
        except ValueError:
            pass
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)", text.lower())
        if match:
            amount = float(match.group(1))
            unit = match.group(2)
            multiplier = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
            return now + amount * multiplier
        try:
            parsed = parsedate_to_datetime(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    @classmethod
    def retry_at_from_exception(
        cls,
        exc: Exception,
        *,
        now: float,
    ) -> float | None:
        headers = cls._headers(exc)
        normalized = {str(key).lower(): value for key, value in headers.items()}
        candidates: list[float] = []
        for key in (
            "retry-after",
            "x-ratelimit-reset",
            "x-ratelimit-reset-requests",
            "x-ratelimit-reset-tokens",
        ):
            parsed = cls._parse_retry_value(normalized.get(key), now)
            if parsed is not None:
                candidates.append(parsed)
        return max(candidates) if candidates else None

    def classify_exception(
        self,
        exc: Exception,
    ) -> tuple[ProviderCapacityStatus, float | None, str] | None:
        now = float(self.clock())
        response = getattr(exc, "response", None)
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            status_code = getattr(response, "status_code", None)
        text = str(exc).casefold()
        quota_markers = (
            "insufficient_quota",
            "quota exhausted",
            "quota exceeded",
            "usage limit reached",
            "usage_limit_reached",
            "credits depleted",
            "credit balance",
            "token limit reached",
            "tokens depleted",
            "workspace_owner_credits_depleted",
            "workspace_member_credits_depleted",
            "workspace_owner_usage_limit_reached",
            "workspace_member_usage_limit_reached",
        )
        rate_markers = (
            "rate limit",
            "rate_limit",
            "too many requests",
            "throttl",
        )
        is_quota = any(marker in text for marker in quota_markers)
        is_rate = status_code == 429 or any(marker in text for marker in rate_markers)
        if not is_quota and not is_rate:
            return None
        retry_at = self.retry_at_from_exception(exc, now=now)
        if retry_at is None:
            retry_at = now + self.default_retry_seconds
        status = (
            ProviderCapacityStatus.DEPLETED
            if is_quota
            else ProviderCapacityStatus.THROTTLED
        )
        return status, retry_at, str(exc)[:500]

    def report_exception(
        self,
        provider_id: str,
        runtime_id: str | None,
        exc: Exception,
        *,
        actor: AuthenticationActor,
        source: str,
    ) -> ProviderCapacityRecord | None:
        classified = self.classify_exception(exc)
        if classified is None:
            return None
        status, retry_at, reason = classified
        return self.report(
            ProviderCapacityReport(
                provider_id=provider_id,
                runtime_id=runtime_id,
                status=status,
                reason=reason,
                retry_at=retry_at,
                source=source,
                metadata={
                    "error_type": type(exc).__name__,
                    "status_code": getattr(
                        exc,
                        "status_code",
                        getattr(getattr(exc, "response", None), "status_code", None),
                    ),
                },
                observed_at=float(self.clock()),
            ),
            actor=actor,
        )

    @staticmethod
    def _window_retry(window: Any) -> tuple[bool, float | None, int | None]:
        if not isinstance(window, dict):
            return False, None, None
        used = window.get("usedPercent")
        if used is None:
            used = window.get("used_percent")
        try:
            used_percent = int(used) if used is not None else None
        except (TypeError, ValueError):
            used_percent = None
        reset = window.get("resetsAt")
        if reset is None:
            reset = window.get("reset_at")
        try:
            retry_at = float(reset) if reset is not None else None
        except (TypeError, ValueError):
            retry_at = None
        return bool(used_percent is not None and used_percent >= 100), retry_at, used_percent

    def report_codex_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        actor: AuthenticationActor,
        provider_id: str = "openai",
        runtime_id: str = "codex",
    ) -> ProviderCapacityRecord:
        rate_limits = snapshot.get("rateLimits")
        if not isinstance(rate_limits, dict):
            rate_limits = snapshot.get("rate_limits")
        if not isinstance(rate_limits, dict):
            raise ValueError("Codex rate-limit snapshot is missing rateLimits")

        primary_exhausted, primary_reset, primary_used = self._window_retry(
            rate_limits.get("primary")
        )
        secondary_exhausted, secondary_reset, secondary_used = self._window_retry(
            rate_limits.get("secondary")
        )
        individual = rate_limits.get("individualLimit")
        if not isinstance(individual, dict):
            individual = rate_limits.get("individual_limit")
        spend_exhausted = bool(
            rate_limits.get("spendControlReached")
            if "spendControlReached" in rate_limits
            else rate_limits.get("spend_control_reached")
        )
        individual_reset = None
        if isinstance(individual, dict):
            remaining = individual.get("remainingPercent")
            if remaining is None:
                remaining = individual.get("remaining_percent")
            try:
                spend_exhausted = spend_exhausted or int(remaining) <= 0
            except (TypeError, ValueError):
                pass
            raw_reset = individual.get("resetsAt")
            if raw_reset is None:
                raw_reset = individual.get("resets_at")
            try:
                individual_reset = (
                    float(raw_reset) if raw_reset is not None else None
                )
            except (TypeError, ValueError):
                individual_reset = None

        ordinary_allowed = snapshot.get("ordinaryUsageAllowed")
        if ordinary_allowed is None:
            ordinary_allowed = snapshot.get("ordinary_usage_allowed")
        reached_type = rate_limits.get("rateLimitReachedType")
        if reached_type is None:
            reached_type = rate_limits.get("rate_limit_reached_type")
        exhausted = (
            ordinary_allowed is False
            or bool(reached_type)
            or primary_exhausted
            or secondary_exhausted
            or spend_exhausted
        )
        resets = [
            value
            for value in (
                primary_reset if primary_exhausted else None,
                secondary_reset if secondary_exhausted else None,
                individual_reset if spend_exhausted else None,
            )
            if value is not None and value > float(self.clock())
        ]
        retry_at = max(resets) if resets else None
        if exhausted and retry_at is None:
            retry_at = float(self.clock()) + self.default_retry_seconds
        return self.report(
            ProviderCapacityReport(
                provider_id=provider_id,
                runtime_id=runtime_id,
                status=(
                    ProviderCapacityStatus.DEPLETED
                    if exhausted
                    else ProviderCapacityStatus.AVAILABLE
                ),
                reason=(
                    str(reached_type or "Codex account usage is exhausted")
                    if exhausted
                    else "Codex account usage is available"
                ),
                retry_at=retry_at,
                source="codex:account/rateLimits/read",
                metadata={
                    "ordinary_usage_allowed": ordinary_allowed,
                    "primary_used_percent": primary_used,
                    "secondary_used_percent": secondary_used,
                    "spend_control_reached": spend_exhausted,
                    "rate_limit_reached_type": reached_type,
                },
                observed_at=float(self.clock()),
            ),
            actor=actor,
        )

    async def refresh_if_due(
        self,
        provider_id: str,
        runtime_id: str | None,
        *,
        actor: AuthenticationActor,
    ) -> ProviderCapacityRecord | None:
        key = self._key(provider_id, runtime_id)
        probe = self._probes.get(key)
        current = self.get(provider_id, runtime_id, actor=actor)
        now = float(self.clock())
        if probe is None:
            return current
        if current is not None:
            if current.blocks(now):
                return current
            if now - current.observed_at < self.min_probe_interval_seconds:
                return current
        result = probe()
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise ValueError("provider capacity probe returned invalid payload")
        if key == ("openai", "codex"):
            return self.report_codex_snapshot(result, actor=actor)
        return current

    def wait_for_capacity(
        self,
        payload: ProviderCapacityWaitCreate,
        *,
        actor: AuthenticationActor,
    ) -> ProviderCapacityWait:
        now = float(self.clock())
        retry_at = max(float(payload.retry_at), now + 0.001)
        current_waits = self.list_waits(actor, include_terminal=False)
        existing = next(
            (
                item
                for item in current_waits
                if item.thread_id == payload.thread_id
                and item.work_item_ref == payload.work_item_ref
                and item.execution_id == payload.execution_id
            ),
            None,
        )
        if existing is not None and existing.retry_at <= retry_at:
            return existing
        if existing is not None and existing.schedule_id and self.scheduler is not None:
            try:
                self.scheduler.cancel(
                    existing.schedule_id,
                    actor_id=actor.identity_id,
                )
            except Exception:
                pass
            self._transition_wait(
                existing.id,
                actor=actor,
                status=ProviderCapacityWaitStatus.CANCELLED,
                resumed_at=None,
            )

        wait = ProviderCapacityWait(
            **payload.model_dump(mode="python"),
            retry_at=retry_at,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            created_at=now,
            updated_at=now,
        )

        if self.scheduler is not None:
            schedule = self.scheduler.create(
                ScheduleCreate(
                    name=f"Resume capacity wait {wait.id}",
                    tenant_id=actor.organization_id,
                    workspace_id=actor.workspace_id,
                    trigger_type=self.RESUME_TRIGGER_TYPE,
                    payload={"capacity_wait_id": wait.id},
                    due_at=retry_at,
                    misfire_policy=MisfirePolicy.FIRE_ONCE,
                    misfire_grace_seconds=0.0,
                ),
                actor_id=actor.identity_id,
            )
            wait = wait.model_copy(update={"schedule_id": schedule.id})

        def apply(state):
            state.waits.append(wait)
            state.waits = state.waits[-5000:]
            return state

        self.store.update(apply)
        return wait

    def _transition_wait(
        self,
        wait_id: str,
        *,
        actor: AuthenticationActor | None,
        status: ProviderCapacityWaitStatus,
        resumed_at: float | None,
    ) -> ProviderCapacityWait | None:
        updated: list[ProviderCapacityWait] = []

        def apply(state):
            rows = []
            for item in state.waits:
                if item.id != wait_id:
                    rows.append(item)
                    continue
                if actor is not None and not self._same_scope(item, actor):
                    rows.append(item)
                    continue
                changed = item.model_copy(
                    update={
                        "status": status,
                        "resumed_at": resumed_at,
                        "updated_at": float(self.clock()),
                    }
                )
                rows.append(changed)
                updated.append(changed)
            state.waits = rows
            return state

        self.store.update(apply)
        return updated[0] if updated else None

    async def handle_schedule_event(self, event: CanonicalEventEnvelope) -> None:
        if event.event_type != CanonicalEventType.SCHEDULE.value:
            return
        payload = event.payload
        if payload.get("trigger_type") != self.RESUME_TRIGGER_TYPE:
            return
        nested = payload.get("payload")
        if not isinstance(nested, dict):
            return
        wait_id = str(nested.get("capacity_wait_id") or "").strip()
        if not wait_id:
            return
        wait = self._transition_wait(
            wait_id,
            actor=None,
            status=ProviderCapacityWaitStatus.RESUMED,
            resumed_at=float(self.clock()),
        )
        if wait is None:
            return
        for handler in tuple(self._resume_handlers):
            outcome = handler(wait)
            if inspect.isawaitable(outcome):
                await outcome


def install_provider_capacity_event_bridge(
    bus: CanonicalEventBus,
    service: ProviderCapacityService,
):
    return bus.subscribe(
        service.handle_schedule_event,
        event_types=(CanonicalEventType.SCHEDULE,),
    )
