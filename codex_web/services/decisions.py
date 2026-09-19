from __future__ import annotations

import time
from typing import Iterable

from codex_web.approval_requests import (
    ApprovalConsumeRequest,
    ApprovalRequestCreate,
    ApprovalRequestStatus,
    ApprovalTarget,
)
from codex_web.artifact_evidence import EvidenceLifecycle
from codex_web.canonical_events import CanonicalEventType
from codex_web.compatibility import CanonicalEventEnvelope
from codex_web.decisions import (
    Decision,
    DecisionApprovalRequest,
    DecisionBudget,
    DecisionCreate,
    DecisionDissent,
    DecisionEvidenceInput,
    DecisionEvidenceKind,
    DecisionEvidenceRef,
    DecisionEvent,
    DecisionFinalDecision,
    DecisionImportance,
    DecisionPostExecutionReview,
    DecisionPostExecutionReviewCreate,
    DecisionRecommendation,
    DecisionRevision,
    DecisionState,
    DecisionStatus,
    DecisionSupersedeRequest,
    DecisionUpdate,
)
from codex_web.identity import AuthenticationActor, MembershipRole, PrincipalKind
from codex_web.metrics import MetricFreshness
from codex_web.services.approval_requests import (
    ApprovalAtomicMutation,
    ApprovalRequestService,
    ApprovalStateError,
)
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.canonical_events import CanonicalEventIngestionService
from codex_web.services.metrics import MetricError, MetricService
from codex_web.services.goals import GoalError, GoalService
from codex_web.storage.decisions import (
    DecisionConflictError,
    DecisionNotFoundError,
    DecisionStore,
)


class DecisionError(RuntimeError):
    pass


class DecisionStateError(DecisionError):
    pass


class DecisionValidationError(DecisionError):
    pass


class DecisionAuthorizationError(DecisionError):
    pass


DEFAULT_DECISION_BUDGETS: dict[DecisionImportance, DecisionBudget] = {
    DecisionImportance.LOW: DecisionBudget(
        max_input_tokens=12000,
        max_output_tokens=4000,
        max_model_calls=4,
        max_cost_usd=1.0,
    ),
    DecisionImportance.MEDIUM: DecisionBudget(
        max_input_tokens=24000,
        max_output_tokens=8000,
        max_model_calls=6,
        max_cost_usd=2.5,
    ),
    DecisionImportance.HIGH: DecisionBudget(
        max_input_tokens=48000,
        max_output_tokens=16000,
        max_model_calls=8,
        max_cost_usd=6.0,
    ),
    DecisionImportance.CRITICAL: DecisionBudget(
        max_input_tokens=80000,
        max_output_tokens=24000,
        max_model_calls=12,
        max_cost_usd=12.0,
    ),
}


class DecisionService:
    """Canonical Decision lifecycle with exact evidence and ApprovalRequest binding."""

    def __init__(
        self,
        store: DecisionStore,
        approvals: ApprovalRequestService,
        metrics: MetricService,
        artifact_evidence: ArtifactEvidenceService,
        canonical_events: CanonicalEventIngestionService | None = None,
        goals: GoalService | None = None,
        *,
        clock=time.time,
    ) -> None:
        self.store = store
        self.approvals = approvals
        self.metrics = metrics
        self.artifact_evidence = artifact_evidence
        self.canonical_events = canonical_events
        self.goals = goals
        self.clock = clock

    @staticmethod
    def _same_scope(decision: Decision, actor: AuthenticationActor) -> bool:
        return (
            decision.organization_id == actor.organization_id
            and decision.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _admin(actor: AuthenticationActor) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return "decisions:admin" in actor.service_scopes
        return actor.has_role(MembershipRole.OWNER, MembershipRole.ADMIN)

    def _require_edit(self, decision: Decision, actor: AuthenticationActor) -> None:
        if not self._same_scope(decision, actor):
            raise DecisionNotFoundError(decision.id)
        if decision.initiator_identity_id == actor.identity_id or self._admin(actor):
            return
        raise DecisionAuthorizationError(
            "Decision initiator or administrator required"
        )

    def _validate_goal(
        self,
        goal_id: str | None,
        *,
        actor: AuthenticationActor,
    ) -> None:
        if goal_id is None:
            return
        if self.goals is None:
            raise DecisionValidationError(
                "Goal-linked Decisions require the canonical Goal service"
            )
        try:
            self.goals.get(goal_id, scope=actor.tenant)
        except GoalError as exc:
            raise DecisionValidationError(str(exc)) from exc

    @staticmethod
    def _budget(
        importance: DecisionImportance,
        explicit: DecisionBudget | None,
    ) -> DecisionBudget:
        return explicit or DEFAULT_DECISION_BUDGETS[importance]

    @staticmethod
    def _normalize_texts(values: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.strip() for item in values if item.strip()))

    def _resolve_evidence(
        self,
        rows: tuple[DecisionEvidenceInput, ...],
        *,
        actor: AuthenticationActor,
    ) -> tuple[DecisionEvidenceRef, ...]:
        canonical = {
            item.id: item
            for item in self.artifact_evidence.list_evidence(
                actor,
                include_inactive=True,
            )
        }
        resolved: list[DecisionEvidenceRef] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            if row.kind == DecisionEvidenceKind.EVIDENCE:
                assert row.evidence_id is not None
                evidence = canonical.get(row.evidence_id)
                if evidence is None:
                    raise DecisionValidationError(
                        f"canonical Evidence not found: {row.evidence_id}"
                    )
                if evidence.lifecycle != EvidenceLifecycle.VALID:
                    raise DecisionValidationError(
                        f"canonical Evidence is not valid: {row.evidence_id}"
                    )
                key = (row.kind.value, row.evidence_id)
                if key in seen:
                    continue
                seen.add(key)
                resolved.append(
                    DecisionEvidenceRef(
                        kind=row.kind,
                        evidence_id=evidence.id,
                        summary=row.summary or evidence.summary,
                    )
                )
                continue

            assert row.metric_id is not None
            assert row.metric_snapshot_id is not None
            try:
                snapshot = self.metrics.get_snapshot(
                    row.metric_id,
                    row.metric_snapshot_id,
                    scope=actor.tenant,
                )
            except MetricError as exc:
                raise DecisionValidationError(str(exc)) from exc
            key = (row.kind.value, snapshot.id)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(
                DecisionEvidenceRef(
                    kind=row.kind,
                    metric_id=snapshot.metric_id,
                    metric_snapshot_id=snapshot.id,
                    metric_revision=snapshot.metric_revision,
                    metric_freshness=snapshot.freshness,
                    observed_value=snapshot.value,
                    unit=snapshot.unit,
                    observation_ids=snapshot.observation_ids,
                    window_start=snapshot.window_start,
                    window_end=snapshot.window_end,
                    summary=row.summary or snapshot.freshness_reason,
                )
            )
        return tuple(resolved)

    @staticmethod
    def _append_history(
        state: DecisionState,
        updated: Decision,
        *,
        actor_id: str,
        reason: str,
        event_type: str,
    ) -> DecisionState:
        state.decisions = [
            updated if item.id == updated.id else item
            for item in state.decisions
        ]
        state.revisions.append(
            DecisionRevision(
                decision_id=updated.id,
                revision=updated.revision,
                snapshot=updated.model_copy(deep=True),
                reason=reason,
                revised_by=actor_id,
                revised_at=updated.updated_at,
            )
        )
        state.events.append(
            DecisionEvent(
                decision_id=updated.id,
                event_type=event_type,
                revision=updated.revision,
                actor_id=actor_id,
                reason=reason,
                occurred_at=updated.updated_at,
            )
        )
        return state

    async def _emit(self, decision: Decision, *, transition: str, actor_id: str) -> None:
        if self.canonical_events is None:
            return
        await self.canonical_events.ingest(
            event_type=CanonicalEventType.DECISION,
            source=f"decision:{decision.id}",
            idempotency_key=f"{decision.id}:{decision.revision}:{transition}",
            payload={
                "decision_id": decision.id,
                "revision": decision.revision,
                "status": decision.status.value,
                "transition": transition,
                "project_id": decision.project_id,
                "importance": decision.importance.value,
                "approval_request_id": decision.approval_request_id,
                "actor_id": actor_id,
            },
            occurred_at=decision.updated_at,
            tenant_id=decision.organization_id,
            workspace_id=decision.workspace_id,
        )

    async def create(
        self,
        payload: DecisionCreate,
        *,
        actor: AuthenticationActor,
    ) -> Decision:
        self._validate_goal(payload.goal_id, actor=actor)
        evidence = self._resolve_evidence(payload.evidence, actor=actor)
        now = float(self.clock())
        decision = Decision(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=payload.project_id,
            goal_id=payload.goal_id,
            title=payload.title,
            question=payload.question,
            initiator_identity_id=actor.identity_id,
            participants=payload.participants,
            evidence=evidence,
            assumptions=self._normalize_texts(payload.assumptions),
            constraints=self._normalize_texts(payload.constraints),
            options=payload.options,
            importance=payload.importance,
            budget=self._budget(payload.importance, payload.budget),
            limits=payload.limits,
            review_at=payload.review_at,
            expires_at=payload.expires_at,
            created_at=now,
            updated_at=now,
        )
        self.store.create(decision)

        def record_initial(state: DecisionState, current: Decision):
            return (
                self._append_history(
                    state,
                    current,
                    actor_id=actor.identity_id,
                    reason="Decision created",
                    event_type="decision.created",
                ),
                current,
            )

        stored = self.store.update(decision.id, record_initial)
        await self._emit(stored, transition="created", actor_id=actor.identity_id)
        return stored

    def list(self, *, actor: AuthenticationActor) -> tuple[Decision, ...]:
        return self.store.list(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
        )

    def get(self, decision_id: str, *, actor: AuthenticationActor) -> Decision:
        item = self.store.get(decision_id)
        if not self._same_scope(item, actor):
            raise DecisionNotFoundError(decision_id)
        return item

    def revisions(
        self,
        decision_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[DecisionRevision, ...]:
        self.get(decision_id, actor=actor)
        rows = [
            item
            for item in self.store.load().revisions
            if item.decision_id == decision_id
        ]
        rows.sort(key=lambda item: item.revision)
        return tuple(rows)

    def events(
        self,
        decision_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[DecisionEvent, ...]:
        self.get(decision_id, actor=actor)
        rows = [
            item
            for item in self.store.load().events
            if item.decision_id == decision_id
        ]
        rows.sort(key=lambda item: (item.occurred_at, item.id))
        return tuple(rows)

    async def revise(
        self,
        decision_id: str,
        payload: DecisionUpdate,
        *,
        actor: AuthenticationActor,
    ) -> Decision:
        current = self.get(decision_id, actor=actor)
        self._require_edit(current, actor)
        if current.status not in {DecisionStatus.DRAFT, DecisionStatus.ANALYSIS}:
            raise DecisionStateError(
                f"Decision content cannot be revised in {current.status.value}"
            )
        if (
            payload.expected_revision is not None
            and payload.expected_revision != current.revision
        ):
            raise DecisionConflictError(
                f"Decision revision changed: expected {payload.expected_revision}, "
                f"found {current.revision}"
            )

        goal_id = (
            payload.goal_id
            if "goal_id" in payload.model_fields_set
            else current.goal_id
        )
        self._validate_goal(goal_id, actor=actor)
        evidence = (
            self._resolve_evidence(payload.evidence, actor=actor)
            if payload.evidence is not None
            else current.evidence
        )
        importance = payload.importance or current.importance
        budget = (
            payload.budget
            if payload.budget is not None
            else (
                self._budget(importance, None)
                if payload.importance is not None
                and payload.importance != current.importance
                else current.budget
            )
        )
        changes = {
            "title": payload.title if payload.title is not None else current.title,
            "question": payload.question if payload.question is not None else current.question,
            "goal_id": goal_id,
            "participants": (
                payload.participants
                if payload.participants is not None
                else current.participants
            ),
            "evidence": evidence,
            "assumptions": (
                self._normalize_texts(payload.assumptions)
                if payload.assumptions is not None
                else current.assumptions
            ),
            "constraints": (
                self._normalize_texts(payload.constraints)
                if payload.constraints is not None
                else current.constraints
            ),
            "options": payload.options if payload.options is not None else current.options,
            "importance": importance,
            "budget": budget,
            "limits": payload.limits if payload.limits is not None else current.limits,
            "review_at": payload.review_at if "review_at" in payload.model_fields_set else current.review_at,
            "expires_at": payload.expires_at if "expires_at" in payload.model_fields_set else current.expires_at,
        }

        def apply(state: DecisionState, stored: Decision):
            self._require_edit(stored, actor)
            if stored.revision != current.revision:
                raise DecisionConflictError("Decision changed while revision was being applied")
            now = float(self.clock())
            updated = stored.model_copy(
                update={
                    **changes,
                    "recommendation": None,
                    "dissent": (),
                    "deliberation_rounds": (),
                    "status": DecisionStatus.DRAFT,
                    "updated_at": now,
                    "revision": stored.revision + 1,
                }
            )
            updated = Decision.model_validate(updated.model_dump(mode="python"))
            return (
                self._append_history(
                    state,
                    updated,
                    actor_id=actor.identity_id,
                    reason=payload.reason,
                    event_type="decision.revised",
                ),
                updated,
            )

        updated = self.store.update(decision_id, apply)
        await self._emit(updated, transition="revised", actor_id=actor.identity_id)
        return updated

    async def record_deliberation(
        self,
        decision_id: str,
        *,
        round_record,
        recommendation: DecisionRecommendation,
        dissent: tuple[DecisionDissent, ...],
        actor: AuthenticationActor,
    ) -> Decision:
        current = self.get(decision_id, actor=actor)
        self._require_edit(current, actor)
        if current.status not in {DecisionStatus.DRAFT, DecisionStatus.ANALYSIS}:
            raise DecisionStateError(
                f"Decision deliberation is unavailable in {current.status.value}"
            )
        if len(current.deliberation_rounds) >= current.limits.max_rounds:
            raise DecisionStateError("Decision deliberation-round limit is exhausted")
        option_ids = {item.id for item in current.options}
        participant_ids = {item.id for item in current.participants}
        if recommendation.option_id not in option_ids:
            raise DecisionValidationError("recommendation references unknown option")
        if {item.participant_id for item in round_record.analyses} != participant_ids:
            raise DecisionValidationError(
                "deliberation round must contain exactly one analysis per participant"
            )

        def apply(state: DecisionState, stored: Decision):
            if stored.revision != current.revision:
                raise DecisionConflictError("Decision changed during deliberation")
            now = float(self.clock())
            updated = stored.model_copy(
                update={
                    "status": DecisionStatus.ANALYSIS,
                    "deliberation_rounds": (
                        *stored.deliberation_rounds,
                        round_record,
                    ),
                    "recommendation": recommendation,
                    "dissent": dissent,
                    "updated_at": now,
                    "revision": stored.revision + 1,
                }
            )
            return (
                self._append_history(
                    state,
                    updated,
                    actor_id=actor.identity_id,
                    reason=f"bounded deliberation round {round_record.round_number}",
                    event_type="decision.deliberated",
                ),
                updated,
            )

        updated = self.store.update(decision_id, apply)
        await self._emit(updated, transition="deliberated", actor_id=actor.identity_id)
        return updated

    @staticmethod
    def _approval_target(decision: Decision) -> ApprovalTarget:
        return ApprovalTarget(
            operation="decision.approve",
            object_type="decision",
            object_id=decision.id,
            target_version=str(decision.revision),
            target_digest=decision.approval_digest(),
        )

    async def request_approval(
        self,
        decision_id: str,
        payload: DecisionApprovalRequest,
        *,
        actor: AuthenticationActor,
    ) -> Decision:
        current = self.get(decision_id, actor=actor)
        self._require_edit(current, actor)
        if current.status == DecisionStatus.AWAITING_APPROVAL:
            return current
        if current.status not in {DecisionStatus.DRAFT, DecisionStatus.ANALYSIS}:
            raise DecisionStateError(
                f"Decision approval cannot be requested in {current.status.value}"
            )
        if current.recommendation is None:
            raise DecisionStateError(
                "Decision requires a recommendation before approval can be requested"
            )
        now = float(self.clock())
        if current.expires_at is not None and now >= current.expires_at:
            raise DecisionStateError("Decision is expired")

        target_revision = current.revision + 1
        approval_id = f"approval-decision-{current.id}-r{target_revision}"
        awaiting = current.model_copy(
            update={
                "status": DecisionStatus.AWAITING_APPROVAL,
                "approval_request_id": approval_id,
                "approval_target_revision": target_revision,
                "updated_at": now,
                "revision": target_revision,
            }
        )
        target = self._approval_target(awaiting)
        evidence_refs = tuple(
            item.evidence_id or item.metric_snapshot_id or item.id
            for item in awaiting.evidence
        )
        approval_payload = ApprovalRequestCreate(
            target=target,
            project_id=awaiting.project_id,
            reason=payload.reason,
            policy_source="decision-domain",
            authority_source="canonical-approval-request",
            requirement=payload.requirement,
            expires_at=payload.expires_at,
            evidence_refs=evidence_refs,
        )
        try:
            approval = await self.approvals.create(
                approval_payload,
                requester=actor,
                request_id=approval_id,
            )
        except DecisionConflictError:
            raise
        except Exception:
            try:
                approval = self.approvals.get(approval_id, actor=actor)
            except Exception:
                raise
            if (
                approval.target.fingerprint() != target.fingerprint()
                or approval.requester_identity_id != actor.identity_id
            ):
                raise DecisionConflictError(
                    "existing Decision approval request does not match this revision"
                )

        def apply(state: DecisionState, stored: Decision):
            if stored.revision != current.revision:
                raise DecisionConflictError(
                    "Decision changed before approval request could be bound"
                )
            return (
                self._append_history(
                    state,
                    awaiting,
                    actor_id=actor.identity_id,
                    reason=payload.reason,
                    event_type="decision.awaiting_approval",
                ),
                awaiting,
            )

        updated = self.store.update(decision_id, apply)
        await self._emit(
            updated,
            transition="awaiting_approval",
            actor_id=actor.identity_id,
        )
        return updated

    async def finalize_approval(
        self,
        decision_id: str,
        *,
        actor: AuthenticationActor,
    ) -> Decision:
        current = self.get(decision_id, actor=actor)
        if current.status == DecisionStatus.APPROVED:
            return current
        self._require_edit(current, actor)
        if current.status != DecisionStatus.AWAITING_APPROVAL:
            raise DecisionStateError(
                f"Decision is not awaiting approval: {current.status.value}"
            )
        if current.approval_request_id is None:
            raise DecisionStateError("Decision is missing canonical ApprovalRequest")
        if current.approval_target_revision != current.revision:
            raise DecisionStateError("Decision approval target revision is inconsistent")
        approval = self.approvals.get(current.approval_request_id, actor=actor)
        expected_target = self._approval_target(current)
        if approval.target.fingerprint() != expected_target.fingerprint():
            raise DecisionStateError(
                "canonical ApprovalRequest no longer matches Decision revision"
            )

        if approval.status in {
            ApprovalRequestStatus.PENDING,
            ApprovalRequestStatus.PARTIALLY_APPROVED,
        }:
            raise DecisionStateError(
                f"canonical ApprovalRequest is {approval.status.value}"
            )

        if approval.status != ApprovalRequestStatus.APPROVED:
            now = float(self.clock())

            def reject(state: DecisionState, stored: Decision):
                if stored.revision != current.revision:
                    raise DecisionConflictError("Decision changed during approval sync")
                updated = stored.model_copy(
                    update={
                        "status": DecisionStatus.REJECTED,
                        "updated_at": now,
                        "revision": stored.revision + 1,
                    }
                )
                return (
                    self._append_history(
                        state,
                        updated,
                        actor_id=actor.identity_id,
                        reason=f"canonical ApprovalRequest ended as {approval.status.value}",
                        event_type="decision.rejected",
                    ),
                    updated,
                )

            rejected = self.store.update(decision_id, reject)
            await self._emit(
                rejected,
                transition="rejected",
                actor_id=actor.identity_id,
            )
            return rejected

        recommendation = current.recommendation
        if recommendation is None:
            raise DecisionStateError("awaiting Decision has no recommendation")

        consume = ApprovalConsumeRequest(
            target=expected_target,
            idempotency_key=f"decision:{current.id}:approve:r{current.revision}",
            resulting_operation_reference=(
                f"decision:{current.id}:approved:r{current.revision + 1}"
            ),
        )

        def mutate(raw):
            def approve(state: DecisionState, stored: Decision):
                if stored.status != DecisionStatus.AWAITING_APPROVAL:
                    raise DecisionStateError(
                        "Decision changed before approval consumption"
                    )
                if stored.revision != current.revision:
                    raise DecisionConflictError(
                        "Decision revision changed before approval consumption"
                    )
                now = float(self.clock())
                final = DecisionFinalDecision(
                    option_id=recommendation.option_id,
                    rationale=recommendation.rationale,
                    confidence=recommendation.confidence,
                    uncertainty=recommendation.uncertainty,
                    approval_request_id=current.approval_request_id or "",
                    decided_at=now,
                )
                updated = stored.model_copy(
                    update={
                        "status": DecisionStatus.APPROVED,
                        "final_decision": final,
                        "updated_at": now,
                        "revision": stored.revision + 1,
                    }
                )
                return (
                    self._append_history(
                        state,
                        updated,
                        actor_id=actor.identity_id,
                        reason="canonical ApprovalRequest consumed",
                        event_type="decision.approved",
                    ),
                    updated,
                )

            return self.store.mutate_document(raw, decision_id, approve)

        consumed, updated = await self.approvals.consume_with_document(
            current.approval_request_id,
            consume,
            actor=actor,
            mutation=ApprovalAtomicMutation(
                namespace=self.store.namespace,
                default=self.store.default_document(),
                apply=mutate,
            ),
        )
        if consumed.status != ApprovalRequestStatus.CONSUMED:
            raise ApprovalStateError(
                f"approval consumption did not complete: {consumed.status.value}"
            )
        await self._emit(
            updated,
            transition="approved",
            actor_id=actor.identity_id,
        )
        return updated

    async def supersede(
        self,
        decision_id: str,
        payload: DecisionSupersedeRequest,
        *,
        actor: AuthenticationActor,
    ) -> Decision:
        current = self.get(decision_id, actor=actor)
        self._require_edit(current, actor)
        if payload.replacement_decision_id == decision_id:
            raise DecisionValidationError("Decision cannot supersede itself")
        replacement = self.get(payload.replacement_decision_id, actor=actor)
        if replacement.status == DecisionStatus.SUPERSEDED:
            raise DecisionStateError("replacement Decision is already superseded")
        if current.status == DecisionStatus.SUPERSEDED:
            if current.superseded_by_decision_id == replacement.id:
                return current
            raise DecisionStateError("Decision is already superseded")
        if current.status == DecisionStatus.AWAITING_APPROVAL and current.approval_request_id:
            try:
                await self.approvals.cancel(
                    current.approval_request_id,
                    actor=actor,
                )
            except Exception as exc:
                raise DecisionStateError(
                    f"cannot supersede Decision while approval is active: {exc}"
                ) from exc

        def apply(state: DecisionState, stored: Decision):
            now = float(self.clock())
            updated = stored.model_copy(
                update={
                    "status": DecisionStatus.SUPERSEDED,
                    "superseded_by_decision_id": replacement.id,
                    "updated_at": now,
                    "revision": stored.revision + 1,
                }
            )
            return (
                self._append_history(
                    state,
                    updated,
                    actor_id=actor.identity_id,
                    reason=payload.reason,
                    event_type="decision.superseded",
                ),
                updated,
            )

        updated = self.store.update(decision_id, apply)
        await self._emit(
            updated,
            transition="superseded",
            actor_id=actor.identity_id,
        )
        return updated

    async def add_post_execution_review(
        self,
        decision_id: str,
        payload: DecisionPostExecutionReviewCreate,
        *,
        actor: AuthenticationActor,
    ) -> Decision:
        current = self.get(decision_id, actor=actor)
        self._require_edit(current, actor)
        if current.status not in {
            DecisionStatus.APPROVED,
            DecisionStatus.REJECTED,
            DecisionStatus.SUPERSEDED,
        }:
            raise DecisionStateError(
                "post-execution review requires a terminal Decision state"
            )
        valid_evidence = {
            item.id
            for item in self.artifact_evidence.list_evidence(
                actor,
                include_inactive=False,
            )
        }
        missing = [
            item
            for item in payload.evidence_refs
            if item not in valid_evidence
        ]
        if missing:
            raise DecisionValidationError(
                "post-execution review references unavailable Evidence: "
                + ", ".join(sorted(missing))
            )
        review = DecisionPostExecutionReview(
            outcome=payload.outcome,
            summary=payload.summary,
            evidence_refs=tuple(dict.fromkeys(payload.evidence_refs)),
            actions=payload.actions,
            reviewed_by=actor.identity_id,
            reviewed_at=float(self.clock()),
        )

        def apply(state: DecisionState, stored: Decision):
            now = float(self.clock())
            updated = stored.model_copy(
                update={
                    "post_execution_reviews": (
                        *stored.post_execution_reviews,
                        review,
                    ),
                    "updated_at": now,
                    "revision": stored.revision + 1,
                }
            )
            return (
                self._append_history(
                    state,
                    updated,
                    actor_id=actor.identity_id,
                    reason=f"post-execution review: {review.outcome.value}",
                    event_type="decision.post_execution_reviewed",
                ),
                updated,
            )

        updated = self.store.update(decision_id, apply)
        await self._emit(
            updated,
            transition="post_execution_reviewed",
            actor_id=actor.identity_id,
        )
        return updated
