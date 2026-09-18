from __future__ import annotations

import calendar
import datetime as dt
import time
import uuid

from codex_web.entitlements import (
    CapabilityEntitlement,
    CapabilityEntitlementUpdate,
    EntitlementDecision,
    EntitlementMode,
    EntitlementState,
    QuotaBehavior,
    QuotaPolicy,
    QuotaPolicyUpdate,
    QuotaStatus,
    QuotaWindow,
    TenantEntitlementSettings,
    UsageEvent,
    UsageEventCreate,
    UsageRecordResult,
)
from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind
from codex_web.services.identity import AuthorizationError
from codex_web.storage.entitlements import EntitlementStore


class EntitlementError(RuntimeError):
    pass


class EntitlementDeniedError(EntitlementError):
    pass


class QuotaExceededError(EntitlementDeniedError):
    pass


class EntitlementService:
    """Commercial/service entitlement boundary, separate from authorization."""

    def __init__(self, store: EntitlementStore) -> None:
        self.store = store

    @staticmethod
    def _scope(actor: AuthenticationActor) -> tuple[str, str]:
        return actor.organization_id, actor.workspace_id

    @staticmethod
    def _same_scope(item, actor: AuthenticationActor) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _require_admin(actor: AuthenticationActor) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "entitlements:admin" not in actor.service_scopes:
                raise AuthorizationError("entitlements:admin service scope required")
            return
        if not actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN):
            raise AuthorizationError("tenant administrator required")

    @staticmethod
    def _require_meter(actor: AuthenticationActor) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not {"entitlements:meter", "entitlements:admin"}.intersection(
                actor.service_scopes
            ):
                raise AuthorizationError(
                    "entitlements:meter or entitlements:admin service scope required"
                )
            return
        if not actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN):
            raise AuthorizationError("tenant administrator required")

    @staticmethod
    def _settings(
        state: EntitlementState,
        actor: AuthenticationActor,
    ) -> TenantEntitlementSettings | None:
        return next(
            (item for item in state.settings if EntitlementService._same_scope(item, actor)),
            None,
        )

    @staticmethod
    def _mode(state: EntitlementState, actor: AuthenticationActor) -> EntitlementMode:
        item = EntitlementService._settings(state, actor)
        return item.mode if item is not None else EntitlementMode.SELF_HOSTED_UNLIMITED

    @staticmethod
    def _capability(
        state: EntitlementState,
        actor: AuthenticationActor,
        capability: str,
        *,
        at: float,
    ) -> CapabilityEntitlement | None:
        item = next(
            (
                value
                for value in state.capabilities
                if EntitlementService._same_scope(value, actor)
                and value.capability == capability
            ),
            None,
        )
        if item is None:
            return None
        if item.starts_at is not None and at < item.starts_at:
            return None
        if item.expires_at is not None and at >= item.expires_at:
            return None
        return item

    @staticmethod
    def _quota(
        state: EntitlementState,
        actor: AuthenticationActor,
        metric: str,
    ) -> QuotaPolicy | None:
        return next(
            (
                value
                for value in state.quotas
                if EntitlementService._same_scope(value, actor)
                and value.metric == metric
            ),
            None,
        )

    @staticmethod
    def _window_bounds(
        window: QuotaWindow,
        at: float,
    ) -> tuple[float | None, float | None]:
        if window == QuotaWindow.LIFETIME:
            return None, None
        stamp = dt.datetime.fromtimestamp(at, tz=dt.timezone.utc)
        if window == QuotaWindow.HOUR:
            start = stamp.replace(minute=0, second=0, microsecond=0)
            end = start + dt.timedelta(hours=1)
        elif window == QuotaWindow.DAY:
            start = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + dt.timedelta(days=1)
        else:
            start = stamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            _, days = calendar.monthrange(start.year, start.month)
            end = start + dt.timedelta(days=days)
        return start.timestamp(), end.timestamp()

    @classmethod
    def _usage_in_window(
        cls,
        state: EntitlementState,
        actor: AuthenticationActor,
        metric: str,
        *,
        at: float,
        window: QuotaWindow,
    ) -> tuple[float, float | None, float | None]:
        start, end = cls._window_bounds(window, at)
        usage = 0.0
        for event in state.usage:
            if not cls._same_scope(event, actor) or event.metric != metric:
                continue
            if start is not None and event.occurred_at < start:
                continue
            if end is not None and event.occurred_at >= end:
                continue
            usage += event.amount
        return usage, start, end

    @classmethod
    def _decision(
        cls,
        state: EntitlementState,
        actor: AuthenticationActor,
        capability: str,
        *,
        metric: str | None = None,
        projected_amount: float = 0.0,
        at: float | None = None,
    ) -> EntitlementDecision:
        current = time.time() if at is None else at
        mode = cls._mode(state, actor)
        if mode == EntitlementMode.SELF_HOSTED_UNLIMITED:
            return EntitlementDecision(
                allowed=True,
                mode=mode,
                capability=capability,
                reason="self_hosted_unlimited",
            )

        entitlement = cls._capability(state, actor, capability, at=current)
        if entitlement is None:
            return EntitlementDecision(
                allowed=False,
                mode=mode,
                capability=capability,
                reason="capability_not_entitled",
            )
        if not entitlement.enabled:
            return EntitlementDecision(
                allowed=False,
                mode=mode,
                capability=capability,
                reason="capability_disabled",
            )

        if not metric:
            return EntitlementDecision(
                allowed=True,
                mode=mode,
                capability=capability,
                reason="capability_entitled",
            )

        policy = cls._quota(state, actor, metric)
        if policy is None:
            return EntitlementDecision(
                allowed=True,
                mode=mode,
                capability=capability,
                reason="capability_entitled_no_quota",
            )

        usage, start, end = cls._usage_in_window(
            state,
            actor,
            metric,
            at=current,
            window=policy.window,
        )
        projected = usage + max(0.0, projected_amount)
        exceeded = projected > policy.limit
        warning = projected >= policy.limit * policy.warning_fraction
        quota = QuotaStatus(
            metric=metric,
            limit=policy.limit,
            usage=usage,
            projected_usage=projected,
            remaining=max(0.0, policy.limit - usage),
            window=policy.window,
            window_start=start,
            window_end=end,
            behavior=policy.behavior,
            warning=warning,
            exceeded=exceeded,
        )
        if exceeded and policy.behavior == QuotaBehavior.HARD_STOP:
            return EntitlementDecision(
                allowed=False,
                mode=mode,
                capability=capability,
                reason="quota_exceeded_hard_stop",
                quota=quota,
            )
        reason = (
            f"quota_exceeded_{policy.behavior.value}"
            if exceeded
            else "quota_available"
        )
        return EntitlementDecision(
            allowed=True,
            mode=mode,
            capability=capability,
            reason=reason,
            quota=quota,
        )

    def mode(self, actor: AuthenticationActor) -> EntitlementMode:
        return self._mode(self.store.load(), actor)

    def set_mode(
        self,
        mode: EntitlementMode,
        *,
        actor: AuthenticationActor,
    ) -> TenantEntitlementSettings:
        self._require_admin(actor)
        updated: list[TenantEntitlementSettings] = []

        def apply(state: EntitlementState) -> EntitlementState:
            now = time.time()
            replacement = TenantEntitlementSettings(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                mode=mode,
                updated_by=actor.identity_id,
                updated_at=now,
            )
            state.settings = [
                item for item in state.settings if not self._same_scope(item, actor)
            ]
            state.settings.append(replacement)
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def set_capability(
        self,
        capability: str,
        payload: CapabilityEntitlementUpdate,
        *,
        actor: AuthenticationActor,
    ) -> CapabilityEntitlement:
        self._require_admin(actor)
        key = capability.strip()
        if not key:
            raise ValueError("capability is required")
        updated: list[CapabilityEntitlement] = []

        def apply(state: EntitlementState) -> EntitlementState:
            existing = next(
                (
                    item
                    for item in state.capabilities
                    if self._same_scope(item, actor) and item.capability == key
                ),
                None,
            )
            item = CapabilityEntitlement(
                id=existing.id if existing else f"entitlement-{uuid.uuid4().hex}",
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                capability=key,
                enabled=payload.enabled,
                source=payload.source,
                starts_at=payload.starts_at,
                expires_at=payload.expires_at,
                updated_by=actor.identity_id,
            )
            state.capabilities = [
                value
                for value in state.capabilities
                if not (self._same_scope(value, actor) and value.capability == key)
            ]
            state.capabilities.append(item)
            updated.append(item)
            return state

        self.store.update(apply)
        return updated[0]

    def set_quota(
        self,
        metric: str,
        payload: QuotaPolicyUpdate,
        *,
        actor: AuthenticationActor,
    ) -> QuotaPolicy:
        self._require_admin(actor)
        key = metric.strip()
        if not key:
            raise ValueError("metric is required")
        updated: list[QuotaPolicy] = []

        def apply(state: EntitlementState) -> EntitlementState:
            existing = next(
                (
                    item
                    for item in state.quotas
                    if self._same_scope(item, actor) and item.metric == key
                ),
                None,
            )
            item = QuotaPolicy(
                id=existing.id if existing else f"quota-{uuid.uuid4().hex}",
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                metric=key,
                limit=payload.limit,
                window=payload.window,
                behavior=payload.behavior,
                warning_fraction=payload.warning_fraction,
                source=payload.source,
                updated_by=actor.identity_id,
            )
            state.quotas = [
                value
                for value in state.quotas
                if not (self._same_scope(value, actor) and value.metric == key)
            ]
            state.quotas.append(item)
            updated.append(item)
            return state

        self.store.update(apply)
        return updated[0]

    def capabilities(self, actor: AuthenticationActor) -> list[CapabilityEntitlement]:
        return sorted(
            [
                item
                for item in self.store.load().capabilities
                if self._same_scope(item, actor)
            ],
            key=lambda item: item.capability,
        )

    def quotas(self, actor: AuthenticationActor) -> list[QuotaPolicy]:
        return sorted(
            [item for item in self.store.load().quotas if self._same_scope(item, actor)],
            key=lambda item: item.metric,
        )

    def check(
        self,
        capability: str,
        *,
        actor: AuthenticationActor,
        metric: str | None = None,
        projected_amount: float = 0.0,
        at: float | None = None,
    ) -> EntitlementDecision:
        return self._decision(
            self.store.load(),
            actor,
            capability,
            metric=metric,
            projected_amount=projected_amount,
            at=at,
        )

    def require_capability(
        self,
        capability: str,
        *,
        actor: AuthenticationActor,
    ) -> EntitlementDecision:
        decision = self.check(capability, actor=actor)
        if not decision.allowed:
            raise EntitlementDeniedError(decision.reason)
        return decision

    @staticmethod
    def _event_from_payload(
        payload: UsageEventCreate,
        actor: AuthenticationActor,
    ) -> UsageEvent:
        return UsageEvent(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            idempotency_key=payload.idempotency_key,
            metric=payload.metric,
            amount=payload.amount,
            occurred_at=payload.occurred_at,
            source=payload.source,
            actor_id=actor.identity_id,
            project_id=payload.project_id,
            resource_id=payload.resource_id,
            work_item_ref=payload.work_item_ref,
            action_intent_id=payload.action_intent_id,
        )

    def record_usage(
        self,
        payload: UsageEventCreate,
        *,
        actor: AuthenticationActor,
    ) -> UsageRecordResult:
        self._require_meter(actor)
        result: list[UsageRecordResult] = []

        def apply(state: EntitlementState) -> EntitlementState:
            duplicate = next(
                (
                    item
                    for item in state.usage
                    if self._same_scope(item, actor)
                    and item.idempotency_key == payload.idempotency_key
                ),
                None,
            )
            if duplicate is not None:
                result.append(UsageRecordResult(event=duplicate, duplicate=True))
                return state
            event = self._event_from_payload(payload, actor)
            state.usage.append(event)
            result.append(UsageRecordResult(event=event, duplicate=False))
            return state

        self.store.update(apply)
        return result[0]

    def consume(
        self,
        capability: str,
        payload: UsageEventCreate,
        *,
        actor: AuthenticationActor,
    ) -> UsageRecordResult:
        """Atomically enforce entitlement/quota and append idempotent usage."""
        result: list[UsageRecordResult] = []

        def apply(state: EntitlementState) -> EntitlementState:
            duplicate = next(
                (
                    item
                    for item in state.usage
                    if self._same_scope(item, actor)
                    and item.idempotency_key == payload.idempotency_key
                ),
                None,
            )
            projected = 0.0 if duplicate is not None else payload.amount
            decision = self._decision(
                state,
                actor,
                capability,
                metric=payload.metric,
                projected_amount=projected,
                at=payload.occurred_at,
            )
            if not decision.allowed:
                if decision.reason == "quota_exceeded_hard_stop":
                    raise QuotaExceededError(decision.reason)
                raise EntitlementDeniedError(decision.reason)
            if duplicate is not None:
                result.append(
                    UsageRecordResult(
                        event=duplicate,
                        duplicate=True,
                        decision=decision,
                    )
                )
                return state
            event = self._event_from_payload(payload, actor)
            state.usage.append(event)
            result.append(
                UsageRecordResult(
                    event=event,
                    duplicate=False,
                    decision=decision,
                )
            )
            return state

        self.store.update(apply)
        return result[0]

    def usage(
        self,
        actor: AuthenticationActor,
        *,
        metric: str | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> list[UsageEvent]:
        items = [
            item
            for item in self.store.load().usage
            if self._same_scope(item, actor)
        ]
        if metric is not None:
            items = [item for item in items if item.metric == metric]
        if start is not None:
            items = [item for item in items if item.occurred_at >= start]
        if end is not None:
            items = [item for item in items if item.occurred_at < end]
        return sorted(items, key=lambda item: (item.occurred_at, item.id))

    def usage_total(
        self,
        actor: AuthenticationActor,
        *,
        metric: str,
        start: float | None = None,
        end: float | None = None,
    ) -> float:
        return sum(
            item.amount
            for item in self.usage(actor, metric=metric, start=start, end=end)
        )
