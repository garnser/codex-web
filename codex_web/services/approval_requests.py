from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from codex_web.approval_requests import (
    ApprovalConsumeRequest,
    ApprovalDecisionOutcome,
    ApprovalDecisionRecord,
    ApprovalDecisionSubmit,
    ApprovalRequest,
    ApprovalRequestCreate,
    ApprovalRequestStatus,
    ApprovalTarget,
    TERMINAL_APPROVAL_REQUEST_STATUSES,
)
from codex_web.canonical_events import CanonicalEventType
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.identity import (
    ASSURANCE_RANK,
    AuthenticationActor,
    AuthenticationAssurance,
    PrincipalKind,
    TenantScope,
)
from codex_web.scheduler import (
    MisfirePolicy,
    ScheduleCreate,
    ScheduleRecord,
)
from codex_web.services.authority_roles import AuthorityRoleService
from codex_web.services.canonical_events import CanonicalEventIngestionService
from codex_web.services.identity import (
    AuthenticationError,
    AuthorizationError,
    IdentityService,
)
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.approval_requests import (
    ApprovalRequestConflictError,
    ApprovalRequestNotFoundError,
    ApprovalRequestStore,
)


class ApprovalRequestError(RuntimeError):
    pass


class ApprovalEligibilityError(ApprovalRequestError):
    pass


class ApprovalStateError(ApprovalRequestError):
    pass


class StaleApprovalTargetError(ApprovalRequestError):
    pass


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ApprovalAtomicMutation:
    namespace: str
    default: Any
    apply: Callable[[Any], tuple[Any, T]]


class ApprovalRequestService:
    """Canonical deterministic approval lifecycle and consumption boundary."""

    EXPIRY_TRIGGER_TYPE = "approval.expire"

    def __init__(
        self,
        store: ApprovalRequestStore,
        identity: IdentityService,
        canonical_events: CanonicalEventIngestionService,
        *,
        scheduler: SchedulerService | None = None,
        authority_roles: AuthorityRoleService | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.identity = identity
        self.canonical_events = canonical_events
        self.scheduler = scheduler
        self.authority_roles = authority_roles
        self.clock = clock
        self._unsubscribe_schedule = canonical_events.bus.subscribe(
            self._handle_schedule_event,
            event_types=(CanonicalEventType.SCHEDULE,),
            predicate=lambda event: (
                event.payload.get("trigger_type") == self.EXPIRY_TRIGGER_TYPE
            ),
        )

    @staticmethod
    def _scope(request: ApprovalRequest) -> TenantScope:
        return TenantScope(
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
        )

    def _current_actor(
        self,
        actor: AuthenticationActor,
        *,
        scope: TenantScope,
        now: float,
    ) -> AuthenticationActor:
        IdentityService.require_scope(actor, scope)
        live = self.identity.actor_for_identity(
            actor.identity_id,
            scope=scope,
        )
        if live.principal_kind != actor.principal_kind:
            raise ApprovalEligibilityError("identity principal kind changed")

        if actor.principal_kind == PrincipalKind.HUMAN:
            if actor.session_id is None:
                if actor.assurance != AuthenticationAssurance.LOCAL_TRUSTED:
                    raise ApprovalEligibilityError(
                        "current human session is required"
                    )
                assurance = actor.assurance
            else:
                state = self.identity.store.load()
                session = next(
                    (
                        item
                        for item in state.sessions
                        if item.id == actor.session_id
                        and item.identity_id == actor.identity_id
                        and item.organization_id == scope.organization_id
                        and item.workspace_id == scope.workspace_id
                    ),
                    None,
                )
                if (
                    session is None
                    or session.revoked_at is not None
                    or now >= session.idle_expires_at
                    or now >= session.absolute_expires_at
                ):
                    raise ApprovalEligibilityError(
                        "approval session is revoked or expired"
                    )
                assurance = (
                    AuthenticationAssurance.MFA
                    if session.step_up_until is not None
                    and session.step_up_until > now
                    else session.assurance
                )
            return live.model_copy(
                update={
                    "assurance": assurance,
                    "session_id": actor.session_id,
                }
            )

        if actor.service_token_id is not None:
            state = self.identity.store.load()
            token = next(
                (
                    item
                    for item in state.service_tokens
                    if item.id == actor.service_token_id
                    and item.service_identity_id == actor.identity_id
                    and item.organization_id == scope.organization_id
                    and item.workspace_id == scope.workspace_id
                ),
                None,
            )
            if (
                token is None
                or token.revoked_at is not None
                or (token.expires_at is not None and now >= token.expires_at)
            ):
                raise ApprovalEligibilityError(
                    "service token is revoked or expired"
                )
            return live.model_copy(
                update={
                    "service_token_id": token.id,
                    "service_scopes": tuple(token.scopes),
                }
            )
        return live.model_copy(
            update={"service_scopes": actor.service_scopes}
        )

    def _authority_role_ids(
        self,
        request: ApprovalRequest,
        actor: AuthenticationActor,
        *,
        now: float,
    ) -> tuple[str, ...]:
        if self.authority_roles is None:
            if request.requirement.authority_role_ids:
                raise ApprovalEligibilityError(
                    "authority Role service is unavailable"
                )
            return ()
        return self.authority_roles.role_ids_for_actor(
            actor,
            project_id=request.project_id,
            now=now,
        )

    def _eligible_actor(
        self,
        request: ApprovalRequest,
        actor: AuthenticationActor,
        *,
        now: float,
    ) -> tuple[AuthenticationActor, tuple[str, ...]]:
        live = self._current_actor(
            actor,
            scope=self._scope(request),
            now=now,
        )
        if live.principal_kind != PrincipalKind.HUMAN:
            raise ApprovalEligibilityError(
                "approval decisions require a human identity"
            )
        if (
            not request.requirement.allow_self_approval
            and live.identity_id == request.requester_identity_id
        ):
            raise ApprovalEligibilityError(
                "requester cannot approve this request"
            )
        try:
            IdentityService.require_assurance(
                live,
                request.requirement.required_assurance,
            )
        except AuthorizationError as exc:
            raise ApprovalEligibilityError(str(exc)) from exc

        authority_role_ids = self._authority_role_ids(
            request,
            live,
            now=now,
        )
        selectors_configured = bool(
            request.requirement.membership_roles
            or request.requirement.team_ids
            or request.requirement.authority_role_ids
        )
        eligible = (
            bool(set(live.roles) & set(request.requirement.membership_roles))
            or bool(set(live.team_ids) & set(request.requirement.team_ids))
            or bool(
                set(authority_role_ids)
                & set(request.requirement.authority_role_ids)
            )
        )
        if selectors_configured and not eligible:
            raise ApprovalEligibilityError(
                "identity does not match required approver roles or groups"
            )
        return live, authority_role_ids

    async def _emit(
        self,
        request: ApprovalRequest,
        *,
        transition: str,
        actor_id: str,
    ) -> None:
        approval_count = len(
            {
                item.identity_id
                for item in request.decisions
                if item.outcome == ApprovalDecisionOutcome.APPROVE
            }
        )
        await self.canonical_events.ingest(
            event_type=CanonicalEventType.APPROVAL,
            source=f"approval:{request.id}",
            idempotency_key=(
                f"{request.id}:{request.revision}:{transition}"
            ),
            payload={
                "approval_request_id": request.id,
                "project_id": request.project_id,
                "transition": transition,
                "status": request.status.value,
                "operation": request.target.operation,
                "object_type": request.target.object_type,
                "object_id": request.target.object_id,
                "target_fingerprint": request.target_fingerprint,
                "quorum": request.requirement.quorum,
                "approval_count": approval_count,
                "actor_id": actor_id,
                "expires_at": request.expires_at,
                "resulting_operation_reference": (
                    request.resulting_operation_reference
                ),
            },
            occurred_at=request.updated_at,
            tenant_id=request.organization_id,
            workspace_id=request.workspace_id,
        )

    def _expiry_schedule(
        self,
        request: ApprovalRequest,
        *,
        actor_id: str,
        now: float,
    ) -> ScheduleRecord | None:
        if request.expires_at is None:
            return None
        if request.expires_at <= now:
            raise ApprovalStateError(
                "approval request expiry must be in the future"
            )
        return ScheduleRecord.from_create(
            ScheduleCreate(
                name=f"Expire approval {request.id}",
                tenant_id=request.organization_id,
                workspace_id=request.workspace_id,
                trigger_type=self.EXPIRY_TRIGGER_TYPE,
                payload={"approval_request_id": request.id},
                due_at=request.expires_at,
                misfire_policy=MisfirePolicy.FIRE_ONCE,
                misfire_grace_seconds=0.0,
            ),
            actor_id=actor_id,
            now=now,
        )

    async def _create_bound(
        self,
        payload: ApprovalRequestCreate,
        *,
        requester: AuthenticationActor,
        request_id: str | None = None,
        require_current_session: bool,
    ) -> ApprovalRequest:
        now = float(self.clock())
        if require_current_session:
            current = self._current_actor(
                requester,
                scope=requester.tenant,
                now=now,
            )
        else:
            current = self.identity.actor_for_identity(
                requester.identity_id,
                scope=requester.tenant,
            )
            if current.principal_kind != requester.principal_kind:
                raise ApprovalEligibilityError(
                    "requester principal kind changed"
                )
        request = ApprovalRequest.from_create(
            payload,
            organization_id=current.organization_id,
            workspace_id=current.workspace_id,
            requester_identity_id=current.identity_id,
            requester_principal_kind=current.principal_kind,
            requester_session_id=(
                current.session_id if require_current_session else None
            ),
            now=now,
            request_id=request_id,
        )
        expiry_schedule = self._expiry_schedule(
            request,
            actor_id=current.identity_id,
            now=now,
        )
        stored = self.store.create(
            request,
            expiry_schedule=expiry_schedule,
        )
        if expiry_schedule is not None and self.scheduler is not None:
            self.scheduler.notify_state_changed()
        await self._emit(
            stored,
            transition="created",
            actor_id=current.identity_id,
        )
        return stored

    async def create_for_identity(
        self,
        payload: ApprovalRequestCreate,
        *,
        requester_identity_id: str,
        scope: TenantScope,
        request_id: str | None = None,
    ) -> ApprovalRequest:
        """Create a policy-generated request bound to a live canonical identity.

        This is for server-owned bridges such as sensitive Definition
        publication. It re-resolves current identity/membership but does not
        fabricate a requester login session or authentication assurance.
        """

        requester = self.identity.actor_for_identity(
            requester_identity_id,
            scope=scope,
        )
        return await self._create_bound(
            payload,
            requester=requester,
            request_id=request_id,
            require_current_session=False,
        )

    async def create(
        self,
        payload: ApprovalRequestCreate,
        *,
        requester: AuthenticationActor,
        request_id: str | None = None,
    ) -> ApprovalRequest:
        return await self._create_bound(
            payload,
            requester=requester,
            request_id=request_id,
            require_current_session=True,
        )

    def list(
        self,
        actor: AuthenticationActor,
    ) -> list[ApprovalRequest]:
        return self.store.list(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )

    def get(
        self,
        request_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ApprovalRequest:
        request = self.store.get(request_id)
        IdentityService.require_scope(actor, self._scope(request))
        return request

    async def decide(
        self,
        request_id: str,
        payload: ApprovalDecisionSubmit,
        *,
        actor: AuthenticationActor,
    ) -> ApprovalRequest:
        now = float(self.clock())
        original = self.store.get(request_id)
        live, authority_role_ids = self._eligible_actor(
            original,
            actor,
            now=now,
        )
        changed = False
        expired = False

        def apply(current: ApprovalRequest) -> ApprovalRequest:
            nonlocal changed, expired
            if current.expires_at is not None and now >= current.expires_at:
                if current.status not in TERMINAL_APPROVAL_REQUEST_STATUSES:
                    changed = True
                    expired = True
                    return current.model_copy(
                        update={
                            "status": ApprovalRequestStatus.EXPIRED,
                            "updated_at": now,
                            "revision": current.revision + 1,
                        }
                    )
                return current

            if current.status in TERMINAL_APPROVAL_REQUEST_STATUSES:
                raise ApprovalStateError(
                    f"approval request is {current.status.value}"
                )
            if current.status == ApprovalRequestStatus.APPROVED:
                raise ApprovalStateError("approval quorum is already satisfied")

            for decision in current.decisions:
                if decision.idempotency_key == payload.idempotency_key:
                    if (
                        decision.identity_id != live.identity_id
                        or decision.outcome != payload.outcome
                        or decision.reason != payload.reason
                    ):
                        raise ApprovalRequestConflictError(
                            "approval decision idempotency key was reused "
                            "with different semantics"
                        )
                    return current
                if decision.identity_id == live.identity_id:
                    if (
                        decision.outcome == payload.outcome
                        and decision.reason == payload.reason
                    ):
                        return current
                    raise ApprovalRequestConflictError(
                        "approver already submitted a different decision"
                    )

            decision = ApprovalDecisionRecord(
                request_id=current.id,
                identity_id=live.identity_id,
                principal_kind=live.principal_kind,
                outcome=payload.outcome,
                reason=payload.reason,
                idempotency_key=payload.idempotency_key,
                assurance=live.assurance,
                session_id=live.session_id,
                membership_roles=live.roles,
                team_ids=live.team_ids,
                authority_role_ids=authority_role_ids,
                decided_at=now,
            )
            decisions = (*current.decisions, decision)
            if payload.outcome == ApprovalDecisionOutcome.REJECT:
                status = ApprovalRequestStatus.REJECTED
            else:
                approvals = len(
                    {
                        item.identity_id
                        for item in decisions
                        if item.outcome == ApprovalDecisionOutcome.APPROVE
                    }
                )
                status = (
                    ApprovalRequestStatus.APPROVED
                    if approvals >= current.requirement.quorum
                    else ApprovalRequestStatus.PARTIALLY_APPROVED
                )
            changed = True
            return current.model_copy(
                update={
                    "decisions": decisions,
                    "status": status,
                    "updated_at": now,
                    "revision": current.revision + 1,
                }
            )

        updated = self.store.update_request(request_id, apply)
        if changed:
            await self._emit(
                updated,
                transition="expired" if expired else "decision_submitted",
                actor_id=live.identity_id,
            )
        if expired:
            raise ApprovalStateError("approval request is expired")
        return updated

    async def expire(
        self,
        request_id: str,
        *,
        now: float | None = None,
        actor_id: str = "scheduler",
    ) -> ApprovalRequest:
        timestamp = float(self.clock()) if now is None else float(now)
        changed = False

        def apply(current: ApprovalRequest) -> ApprovalRequest:
            nonlocal changed
            if current.status in TERMINAL_APPROVAL_REQUEST_STATUSES:
                return current
            if current.expires_at is None or timestamp < current.expires_at:
                return current
            changed = True
            return current.model_copy(
                update={
                    "status": ApprovalRequestStatus.EXPIRED,
                    "updated_at": timestamp,
                    "revision": current.revision + 1,
                }
            )

        updated = self.store.update_request(request_id, apply)
        if changed:
            await self._emit(
                updated,
                transition="expired",
                actor_id=actor_id,
            )
        return updated

    async def cancel(
        self,
        request_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ApprovalRequest:
        now = float(self.clock())
        request = self.store.get(request_id)
        current_actor = self._current_actor(
            actor,
            scope=self._scope(request),
            now=now,
        )
        if (
            current_actor.identity_id != request.requester_identity_id
            and not current_actor.has_role(*tuple(requester_admin_roles()))
        ):
            raise ApprovalEligibilityError(
                "only the requester or an administrator can cancel approval"
            )

        def apply(current: ApprovalRequest) -> ApprovalRequest:
            if current.status in TERMINAL_APPROVAL_REQUEST_STATUSES:
                raise ApprovalStateError(
                    f"approval request is {current.status.value}"
                )
            return current.model_copy(
                update={
                    "status": ApprovalRequestStatus.CANCELLED,
                    "cancelled_by_identity_id": current_actor.identity_id,
                    "cancelled_at": now,
                    "updated_at": now,
                    "revision": current.revision + 1,
                }
            )

        updated = self.store.update_request(request_id, apply)
        await self._emit(
            updated,
            transition="cancelled",
            actor_id=current_actor.identity_id,
        )
        return updated

    async def supersede(
        self,
        request_id: str,
        *,
        replacement_request_id: str,
        actor: AuthenticationActor,
    ) -> ApprovalRequest:
        now = float(self.clock())
        request = self.store.get(request_id)
        current_actor = self._current_actor(
            actor,
            scope=self._scope(request),
            now=now,
        )
        if (
            current_actor.identity_id != request.requester_identity_id
            and not current_actor.has_role(*tuple(requester_admin_roles()))
        ):
            raise ApprovalEligibilityError(
                "only the requester or an administrator can supersede approval"
            )
        if not replacement_request_id.strip() or replacement_request_id == request_id:
            raise ApprovalStateError("valid replacement approval request id required")

        def apply(current: ApprovalRequest) -> ApprovalRequest:
            if current.status in TERMINAL_APPROVAL_REQUEST_STATUSES:
                raise ApprovalStateError(
                    f"approval request is {current.status.value}"
                )
            return current.model_copy(
                update={
                    "status": ApprovalRequestStatus.SUPERSEDED,
                    "superseded_by_request_id": replacement_request_id,
                    "updated_at": now,
                    "revision": current.revision + 1,
                }
            )

        updated = self.store.update_request(request_id, apply)
        await self._emit(
            updated,
            transition="superseded",
            actor_id=current_actor.identity_id,
        )
        return updated

    def _validate_approval_quorum_live(
        self,
        request: ApprovalRequest,
        *,
        now: float,
    ) -> None:
        qualifying: set[str] = set()
        for decision in request.decisions:
            if decision.outcome != ApprovalDecisionOutcome.APPROVE:
                continue
            try:
                live = self.identity.actor_for_identity(
                    decision.identity_id,
                    scope=self._scope(request),
                )
            except (AuthenticationError, AuthorizationError):
                continue
            if live.principal_kind != PrincipalKind.HUMAN:
                continue
            if (
                not request.requirement.allow_self_approval
                and live.identity_id == request.requester_identity_id
            ):
                continue
            try:
                authority_role_ids = self._authority_role_ids(
                    request,
                    live,
                    now=now,
                )
            except ApprovalEligibilityError:
                continue
            selectors_configured = bool(
                request.requirement.membership_roles
                or request.requirement.team_ids
                or request.requirement.authority_role_ids
            )
            eligible = (
                bool(set(live.roles) & set(request.requirement.membership_roles))
                or bool(set(live.team_ids) & set(request.requirement.team_ids))
                or bool(
                    set(authority_role_ids)
                    & set(request.requirement.authority_role_ids)
                )
            )
            if not selectors_configured or eligible:
                qualifying.add(live.identity_id)
        if len(qualifying) < request.requirement.quorum:
            raise ApprovalStateError(
                "approval quorum is no longer eligible at consumption time"
            )

    def _consumed_request(
        self,
        current: ApprovalRequest,
        payload: ApprovalConsumeRequest,
        *,
        consumer: AuthenticationActor,
        now: float,
    ) -> tuple[ApprovalRequest, str | None]:
        if current.status == ApprovalRequestStatus.CONSUMED:
            if (
                current.consumption_idempotency_key == payload.idempotency_key
                and current.resulting_operation_reference
                == payload.resulting_operation_reference
                and current.target_fingerprint == payload.target.fingerprint()
            ):
                return current, None
            raise ApprovalStateError("approval request was already consumed")
        if current.expires_at is not None and now >= current.expires_at:
            return (
                current.model_copy(
                    update={
                        "status": ApprovalRequestStatus.EXPIRED,
                        "updated_at": now,
                        "revision": current.revision + 1,
                    }
                ),
                "expired",
            )
        if current.status != ApprovalRequestStatus.APPROVED:
            raise ApprovalStateError(
                f"approval request is {current.status.value}, not approved"
            )
        if current.target_fingerprint != payload.target.fingerprint():
            return (
                current.model_copy(
                    update={
                        "status": ApprovalRequestStatus.INVALIDATED,
                        "invalidation_reason": (
                            "consumption target does not match approved target"
                        ),
                        "updated_at": now,
                        "revision": current.revision + 1,
                    }
                ),
                "invalidated",
            )

        self._validate_approval_quorum_live(current, now=now)
        return (
            current.model_copy(
                update={
                    "status": ApprovalRequestStatus.CONSUMED,
                    "resulting_operation_reference": (
                        payload.resulting_operation_reference
                    ),
                    "consumption_idempotency_key": payload.idempotency_key,
                    "consumed_by_identity_id": consumer.identity_id,
                    "consumed_at": now,
                    "updated_at": now,
                    "revision": current.revision + 1,
                }
            ),
            "consumed",
        )

    async def consume(
        self,
        request_id: str,
        payload: ApprovalConsumeRequest,
        *,
        actor: AuthenticationActor,
    ) -> ApprovalRequest:
        now = float(self.clock())
        original = self.store.get(request_id)
        consumer = self._current_actor(
            actor,
            scope=self._scope(original),
            now=now,
        )
        transition: str | None = None

        def apply(current: ApprovalRequest) -> ApprovalRequest:
            nonlocal transition
            updated, transition = self._consumed_request(
                current,
                payload,
                consumer=consumer,
                now=now,
            )
            return updated

        updated = self.store.update_request(request_id, apply)
        if transition is not None:
            await self._emit(
                updated,
                transition=transition,
                actor_id=consumer.identity_id,
            )
        if transition == "expired":
            raise ApprovalStateError("approval request is expired")
        if transition == "invalidated":
            raise StaleApprovalTargetError(
                "approval target/version no longer matches"
            )
        return updated

    async def consume_with_document(
        self,
        request_id: str,
        payload: ApprovalConsumeRequest,
        *,
        actor: AuthenticationActor,
        mutation: ApprovalAtomicMutation,
    ) -> tuple[ApprovalRequest, T]:
        now = float(self.clock())
        original = self.store.get(request_id)
        consumer = self._current_actor(
            actor,
            scope=self._scope(original),
            now=now,
        )
        transition: str | None = None

        def apply(
            current: ApprovalRequest,
            guarded: Any,
        ) -> tuple[ApprovalRequest, Any, T]:
            nonlocal transition
            updated, transition = self._consumed_request(
                current,
                payload,
                consumer=consumer,
                now=now,
            )
            if transition != "consumed":
                return updated, guarded, None  # type: ignore[return-value]
            updated_guarded, mutation_result = mutation.apply(guarded)
            return updated, updated_guarded, mutation_result

        updated, result = self.store.consume_with_document(
            request_id,
            guarded_namespace=mutation.namespace,
            guarded_default=mutation.default,
            updater=apply,
        )
        if transition is not None:
            await self._emit(
                updated,
                transition=transition,
                actor_id=consumer.identity_id,
            )
        if transition == "expired":
            raise ApprovalStateError("approval request is expired")
        if transition == "invalidated":
            raise StaleApprovalTargetError(
                "approval target/version no longer matches"
            )
        return updated, result

    async def _handle_schedule_event(
        self,
        event: CanonicalEventEnvelope,
    ) -> None:
        nested = event.payload.get("payload")
        if not isinstance(nested, dict):
            return
        request_id = nested.get("approval_request_id")
        if not isinstance(request_id, str) or not request_id:
            return
        try:
            await self.expire(
                request_id,
                now=float(event.payload.get("scheduled_for") or event.occurred_at),
                actor_id="scheduler",
            )
        except ApprovalRequestNotFoundError:
            return


def requester_admin_roles():
    from codex_web.identity import MembershipRole

    return (MembershipRole.OWNER, MembershipRole.ADMIN)
