from __future__ import annotations

import time

from codex_web.goals import (
    GoalCompletionEvaluation,
    GoalCompletionEvaluationRequest,
    GoalCreate,
    GoalCriterionEvaluation,
    GoalCriterionKind,
    GoalCriterionOperator,
    GoalEvent,
    GoalHealth,
    GoalHealthSnapshot,
    GoalPriority,
    GoalProgress,
    GoalRecord,
    GoalRevision,
    GoalRiskLevel,
    GoalSnapshot,
    GoalState,
    GoalStatus,
    GoalTransitionRequest,
    GoalUpdate,
    GoalWorkGraphBinding,
    GoalWorkItemCompletionEvaluation,
)
from codex_web.identity import TenantScope
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.services.work_graph import (
    WorkGraphError,
    WorkGraphService,
)
from codex_web.storage.goals import GoalStore
from codex_web.work_graph import WorkGraphRelation, WorkReadinessStatus


class GoalError(RuntimeError):
    pass


class GoalNotFoundError(GoalError):
    pass


class GoalConflictError(GoalError):
    pass


class GoalScopeError(GoalError):
    pass


_ALLOWED_TRANSITIONS: dict[GoalStatus, set[GoalStatus]] = {
    GoalStatus.DRAFT: {GoalStatus.ACTIVE, GoalStatus.CANCELLED},
    GoalStatus.ACTIVE: {
        GoalStatus.PAUSED,
        GoalStatus.COMPLETED,
        GoalStatus.CANCELLED,
    },
    GoalStatus.PAUSED: {GoalStatus.ACTIVE, GoalStatus.CANCELLED},
    GoalStatus.COMPLETED: set(),
    GoalStatus.CANCELLED: set(),
}


class GoalService:
    """Canonical deterministic Goal lifecycle and Work Graph projection."""

    def __init__(
        self,
        store: GoalStore,
        projects: ProjectService,
        work_graph: WorkGraphService,
    ) -> None:
        self.store = store
        self.projects = projects
        self.work_graph = work_graph

    @staticmethod
    def _visible(goal: GoalRecord, scope: TenantScope) -> bool:
        return (
            goal.organization_id == scope.organization_id
            and goal.workspace_id == scope.workspace_id
        )

    def _goal(self, state: GoalState, goal_id: str, scope: TenantScope) -> GoalRecord:
        goal = next((item for item in state.goals if item.id == goal_id), None)
        if goal is None or not self._visible(goal, scope):
            raise GoalNotFoundError("goal not found")
        return goal

    def _validate_bindings(
        self,
        bindings: tuple[GoalWorkGraphBinding, ...],
        scope: TenantScope,
    ) -> None:
        seen_projects: set[str] = set()
        for binding in bindings:
            try:
                self.projects.get(binding.project_id, scope)
            except ProjectNotFoundError as exc:
                raise GoalScopeError(
                    f"goal work-graph project not found: {binding.project_id}"
                ) from exc
            if binding.project_id in seen_projects:
                raise GoalConflictError(
                    f"goal cannot bind project more than once: {binding.project_id}"
                )
            seen_projects.add(binding.project_id)
            if not binding.root_work_item_refs:
                continue
            try:
                snapshot = self.work_graph.snapshot(binding.project_id, scope=scope)
            except WorkGraphError as exc:
                raise GoalScopeError(str(exc)) from exc
            refs = {node.ref for node in snapshot.nodes}
            missing = [
                ref for ref in binding.root_work_item_refs if ref not in refs
            ]
            if missing:
                raise GoalScopeError(
                    "goal work-graph roots are not in bound project: "
                    + ", ".join(missing)
                )

    @staticmethod
    def _revision(
        goal: GoalRecord,
        *,
        actor_id: str,
        reason: str,
        revised_at: float,
    ) -> GoalRevision:
        return GoalRevision(
            goal_id=goal.id,
            revision=goal.revision,
            snapshot=goal.model_copy(deep=True),
            reason=reason,
            revised_by=actor_id,
            revised_at=revised_at,
        )

    @staticmethod
    def _event(
        goal: GoalRecord,
        *,
        event_type: str,
        actor_id: str,
        reason: str,
        occurred_at: float,
    ) -> GoalEvent:
        return GoalEvent(
            goal_id=goal.id,
            event_type=event_type,
            revision=goal.revision,
            actor_id=actor_id,
            reason=reason,
            occurred_at=occurred_at,
        )

    def create(
        self,
        payload: GoalCreate,
        *,
        scope: TenantScope,
        actor_id: str,
        originating_executive_activation_id: str | None = None,
        originating_executive_proposal_id: str | None = None,
    ) -> GoalRecord:
        self._validate_bindings(payload.work_graph_bindings, scope)
        now = time.time()
        goal = GoalRecord(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            title=payload.title,
            description=payload.description,
            owner_identity_id=payload.owner_identity_id,
            priority=payload.priority,
            target_date=payload.target_date,
            success_criteria=payload.success_criteria,
            constraints=payload.constraints,
            risks=payload.risks,
            budget=payload.budget,
            approval_requirements=payload.approval_requirements,
            work_graph_bindings=payload.work_graph_bindings,
            originating_executive_activation_id=originating_executive_activation_id,
            originating_executive_proposal_id=originating_executive_proposal_id,
            created_by=actor_id,
            updated_by=actor_id,
            created_at=now,
            updated_at=now,
        )

        def apply(state: GoalState) -> GoalState:
            state.goals.append(goal)
            state.revisions.append(
                self._revision(
                    goal,
                    actor_id=actor_id,
                    reason="goal created",
                    revised_at=now,
                )
            )
            state.events.append(
                self._event(
                    goal,
                    event_type="goal_created",
                    actor_id=actor_id,
                    reason="goal created",
                    occurred_at=now,
                )
            )
            return state

        self.store.update(apply)
        return goal

    def get(self, goal_id: str, *, scope: TenantScope) -> GoalRecord:
        return self._goal(self.store.load(), goal_id, scope)

    def list(
        self,
        *,
        scope: TenantScope,
        status: GoalStatus | None = None,
        owner_identity_id: str | None = None,
        priority: GoalPriority | None = None,
    ) -> tuple[GoalRecord, ...]:
        rows = [
            item for item in self.store.load().goals if self._visible(item, scope)
        ]
        if status is not None:
            rows = [item for item in rows if item.status == status]
        if owner_identity_id is not None:
            rows = [
                item for item in rows if item.owner_identity_id == owner_identity_id
            ]
        if priority is not None:
            rows = [item for item in rows if item.priority == priority]
        rows.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return tuple(rows)

    def revise(
        self,
        goal_id: str,
        payload: GoalUpdate,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> GoalRecord:
        result: GoalRecord | None = None

        def apply(state: GoalState) -> GoalState:
            nonlocal result
            current = self._goal(state, goal_id, scope)
            changes = payload.model_dump(
                mode="python",
                exclude={"reason"},
                exclude_unset=True,
            )
            if not changes:
                raise GoalConflictError("goal revision contains no changes")
            candidate_data = current.model_dump(mode="python")
            candidate_data.update(changes)
            candidate_data.update(
                {
                    "revision": current.revision + 1,
                    "updated_by": actor_id,
                    "updated_at": time.time(),
                }
            )
            candidate = GoalRecord.model_validate(candidate_data)
            self._validate_bindings(candidate.work_graph_bindings, scope)
            state.goals = [
                candidate if item.id == current.id else item
                for item in state.goals
            ]
            state.revisions.append(
                self._revision(
                    candidate,
                    actor_id=actor_id,
                    reason=payload.reason,
                    revised_at=candidate.updated_at,
                )
            )
            state.events.append(
                self._event(
                    candidate,
                    event_type="goal_revised",
                    actor_id=actor_id,
                    reason=payload.reason,
                    occurred_at=candidate.updated_at,
                )
            )
            result = candidate
            return state

        self.store.update(apply)
        assert result is not None
        return result

    @staticmethod
    def _numeric(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    @classmethod
    def _criterion_result(
        cls,
        criterion,
        observation,
    ) -> GoalCriterionEvaluation:
        findings: list[str] = []
        passed = False
        observed_value = observation.observed_value if observation is not None else None
        source = observation.source if observation is not None else None
        reference = observation.reference if observation is not None else None
        observed_at = observation.observed_at if observation is not None else None

        if observation is None:
            findings.append("no verification observation supplied")
        elif criterion.kind == GoalCriterionKind.MANUAL:
            passed = observation.verified is True
            if observation.verified is None:
                findings.append("manual criterion requires explicit verified=true/false")
            elif not passed:
                findings.append("manual criterion was explicitly not verified")
        else:
            if observed_value is None:
                findings.append("metric criterion requires observed_value")
            elif criterion.operator == GoalCriterionOperator.EQ:
                target = criterion.target_value
                observed_numeric = cls._numeric(observed_value)
                target_numeric = cls._numeric(target)
                if observed_numeric is not None and target_numeric is not None:
                    passed = observed_numeric == target_numeric
                else:
                    passed = type(observed_value) is type(target) and observed_value == target
                if not passed:
                    findings.append("observed metric does not equal target")
            elif criterion.operator in {
                GoalCriterionOperator.GTE,
                GoalCriterionOperator.LTE,
            }:
                observed_numeric = cls._numeric(observed_value)
                target_numeric = cls._numeric(criterion.target_value)
                if observed_numeric is None or target_numeric is None:
                    findings.append("ordered metric comparison requires numeric values")
                else:
                    passed = (
                        observed_numeric >= target_numeric
                        if criterion.operator == GoalCriterionOperator.GTE
                        else observed_numeric <= target_numeric
                    )
                    if not passed:
                        findings.append(
                            "observed metric is below target"
                            if criterion.operator == GoalCriterionOperator.GTE
                            else "observed metric is above target"
                        )
            else:
                findings.append("metric criterion has no supported operator")

        return GoalCriterionEvaluation(
            criterion_id=criterion.id,
            kind=criterion.kind,
            required=criterion.required,
            passed=passed,
            description=criterion.description,
            metric_key=criterion.metric_key,
            operator=criterion.operator,
            target_value=criterion.target_value,
            observed_value=observed_value,
            source=source,
            reference=reference,
            observed_at=observed_at,
            findings=tuple(findings),
        )

    def _completion_work_items(
        self,
        goal: GoalRecord,
        *,
        scope: TenantScope,
    ) -> tuple[GoalWorkItemCompletionEvaluation, ...]:
        bound_refs = set(self._bound_refs(goal, scope=scope))
        rows: list[GoalWorkItemCompletionEvaluation] = []
        for project_id in sorted({project for project, _ in bound_refs}):
            snapshot = self.work_graph.snapshot(project_id, scope=scope)
            by_ref = {node.ref: node for node in snapshot.nodes}
            for _project, ref in sorted(
                item for item in bound_refs if item[0] == project_id
            ):
                node = by_ref.get(ref)
                if node is None:
                    rows.append(
                        GoalWorkItemCompletionEvaluation(
                            project_id=project_id,
                            work_item_ref=ref,
                            passed=False,
                            findings=("bound Work Item is missing from canonical graph",),
                        )
                    )
                    continue
                outcome = node.terminal_outcome
                if outcome == "completed":
                    rows.append(
                        GoalWorkItemCompletionEvaluation(
                            project_id=project_id,
                            work_item_ref=ref,
                            terminal_outcome=outcome,
                            passed=True,
                        )
                    )
                else:
                    finding = (
                        f"bound Work Item terminal outcome is {outcome}"
                        if outcome in {"failed", "cancelled"}
                        else "bound Work Item is not completed"
                    )
                    rows.append(
                        GoalWorkItemCompletionEvaluation(
                            project_id=project_id,
                            work_item_ref=ref,
                            terminal_outcome=outcome,
                            passed=False,
                            findings=(finding,),
                        )
                    )
        return tuple(rows)

    def evaluate_completion(
        self,
        goal_id: str,
        payload: GoalCompletionEvaluationRequest,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> GoalCompletionEvaluation:
        goal = self.get(goal_id, scope=scope)
        if goal.status == GoalStatus.CANCELLED:
            raise GoalConflictError("cancelled goal cannot be completion-verified")

        observations = {}
        for item in payload.observations:
            if item.criterion_id in observations:
                raise GoalConflictError(
                    f"duplicate completion observation: {item.criterion_id}"
                )
            observations[item.criterion_id] = item

        criterion_ids = {item.id for item in goal.success_criteria}
        unknown = sorted(set(observations) - criterion_ids)
        if unknown:
            raise GoalConflictError(
                "completion observations reference unknown criteria: "
                + ", ".join(unknown)
            )

        criteria = tuple(
            self._criterion_result(item, observations.get(item.id))
            for item in goal.success_criteria
        )
        work_items = self._completion_work_items(goal, scope=scope)
        blockers = [
            f"work_item:{item.work_item_ref}:{item.findings[0]}"
            for item in work_items
            if not item.passed
        ]
        blockers.extend(
            f"criterion:{item.criterion_id}:{item.findings[0] if item.findings else 'not satisfied'}"
            for item in criteria
            if item.required and not item.passed
        )
        if not work_items and not any(item.required for item in criteria):
            blockers.append(
                "goal has no bound work and no required success criterion to verify"
            )

        evaluation = GoalCompletionEvaluation(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            goal_id=goal.id,
            goal_revision=goal.revision,
            eligible=not blockers,
            work_items=work_items,
            criteria=criteria,
            blockers=tuple(blockers),
            evaluated_by=actor_id,
            reason=payload.reason,
        )

        def apply(state: GoalState) -> GoalState:
            current = self._goal(state, goal_id, scope)
            if current.revision != evaluation.goal_revision:
                raise GoalConflictError(
                    "goal changed during completion evaluation; evaluate current revision again"
                )
            state.completion_evaluations.append(evaluation)
            state.events.append(
                self._event(
                    current,
                    event_type="goal_completion_evaluated",
                    actor_id=actor_id,
                    reason=payload.reason,
                    occurred_at=evaluation.evaluated_at,
                )
            )
            return state

        self.store.update(apply)
        return evaluation

    def completion_evaluations(
        self,
        goal_id: str,
        *,
        scope: TenantScope,
    ) -> tuple[GoalCompletionEvaluation, ...]:
        state = self.store.load()
        self._goal(state, goal_id, scope)
        rows = [
            item
            for item in state.completion_evaluations
            if item.goal_id == goal_id
            and item.organization_id == scope.organization_id
            and item.workspace_id == scope.workspace_id
        ]
        rows.sort(key=lambda item: (item.evaluated_at, item.id), reverse=True)
        return tuple(rows)

    def completion_evaluation(
        self,
        goal_id: str,
        *,
        scope: TenantScope,
    ) -> GoalCompletionEvaluation | None:
        goal = self.get(goal_id, scope=scope)
        return next(
            (
                item
                for item in self.completion_evaluations(goal_id, scope=scope)
                if item.goal_revision == goal.revision
            ),
            None,
        )

    def transition(
        self,
        goal_id: str,
        payload: GoalTransitionRequest,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> GoalRecord:
        result: GoalRecord | None = None

        def apply(state: GoalState) -> GoalState:
            nonlocal result
            current = self._goal(state, goal_id, scope)
            if payload.status == current.status:
                result = current
                return state
            if payload.status not in _ALLOWED_TRANSITIONS[current.status]:
                raise GoalConflictError(
                    f"goal transition {current.status.value} -> "
                    f"{payload.status.value} is not allowed"
                )
            completion_evaluation_id = None
            completed_at = None
            if payload.status == GoalStatus.COMPLETED:
                if not payload.completion_evaluation_id:
                    raise GoalConflictError(
                        "completion transition requires completion_evaluation_id"
                    )
                evaluation = next(
                    (
                        item
                        for item in state.completion_evaluations
                        if item.id == payload.completion_evaluation_id
                        and item.goal_id == current.id
                        and item.organization_id == scope.organization_id
                        and item.workspace_id == scope.workspace_id
                    ),
                    None,
                )
                if evaluation is None:
                    raise GoalConflictError("completion evaluation not found")
                if evaluation.goal_revision != current.revision:
                    raise GoalConflictError(
                        "completion evaluation is stale for current goal revision"
                    )
                latest = next(
                    (
                        item
                        for item in reversed(state.completion_evaluations)
                        if item.goal_id == current.id
                        and item.organization_id == scope.organization_id
                        and item.workspace_id == scope.workspace_id
                        and item.goal_revision == current.revision
                    ),
                    None,
                )
                if latest is None or latest.id != evaluation.id:
                    raise GoalConflictError(
                        "completion transition requires the latest current evaluation"
                    )
                if not evaluation.eligible:
                    raise GoalConflictError(
                        "completion evaluation has unresolved blockers"
                    )
                current_work = self._completion_work_items(current, scope=scope)
                current_blockers = [
                    item.work_item_ref for item in current_work if not item.passed
                ]
                if current_blockers:
                    raise GoalConflictError(
                        "bound Work Items changed after completion evaluation: "
                        + ", ".join(current_blockers)
                    )
                completion_evaluation_id = evaluation.id
                completed_at = time.time()
            elif payload.completion_evaluation_id is not None:
                raise GoalConflictError(
                    "completion_evaluation_id is valid only for completed transition"
                )
            now = completed_at or time.time()
            candidate = current.model_copy(
                update={
                    "status": payload.status,
                    "completion_evaluation_id": completion_evaluation_id,
                    "completed_at": completed_at,
                    "revision": current.revision + 1,
                    "updated_by": actor_id,
                    "updated_at": now,
                }
            )
            state.goals = [
                candidate if item.id == current.id else item
                for item in state.goals
            ]
            state.revisions.append(
                self._revision(
                    candidate,
                    actor_id=actor_id,
                    reason=payload.reason,
                    revised_at=now,
                )
            )
            state.events.append(
                self._event(
                    candidate,
                    event_type="goal_status_changed",
                    actor_id=actor_id,
                    reason=payload.reason,
                    occurred_at=now,
                )
            )
            result = candidate
            return state

        self.store.update(apply)
        assert result is not None
        return result

    def revisions(
        self,
        goal_id: str,
        *,
        scope: TenantScope,
    ) -> tuple[GoalRevision, ...]:
        state = self.store.load()
        self._goal(state, goal_id, scope)
        rows = [item for item in state.revisions if item.goal_id == goal_id]
        rows.sort(key=lambda item: item.revision)
        return tuple(rows)

    def events(
        self,
        *,
        scope: TenantScope,
        goal_id: str | None = None,
        limit: int = 100,
    ) -> tuple[GoalEvent, ...]:
        visible_ids = {
            item.id for item in self.store.load().goals if self._visible(item, scope)
        }
        rows = [
            item
            for item in self.store.load().events
            if item.goal_id in visible_ids
            and (goal_id is None or item.goal_id == goal_id)
        ]
        rows.sort(key=lambda item: item.occurred_at, reverse=True)
        return tuple(rows[: max(1, min(limit, 500))])

    def _bound_refs(
        self,
        goal: GoalRecord,
        *,
        scope: TenantScope,
    ) -> tuple[tuple[str, str], ...]:
        refs: set[tuple[str, str]] = set()
        for binding in goal.work_graph_bindings:
            snapshot = self.work_graph.snapshot(binding.project_id, scope=scope)
            if not binding.root_work_item_refs:
                refs.update((binding.project_id, node.ref) for node in snapshot.nodes)
                continue
            for root in binding.root_work_item_refs:
                refs.add((binding.project_id, root))
                descendants = self.work_graph.traverse(
                    root,
                    scope=scope,
                    relation=WorkGraphRelation.PARENT,
                    direction="downstream",
                )
                refs.update((binding.project_id, ref) for ref in descendants)
        return tuple(sorted(refs))

    def bound_refs(
        self,
        goal_id: str,
        *,
        scope: TenantScope,
    ) -> tuple[tuple[str, str], ...]:
        """Return the exact canonical Work Graph subgraph authorized by a Goal."""
        return self._bound_refs(self.get(goal_id, scope=scope), scope=scope)

    def goals_for_work_item(
        self,
        work_item_ref: str,
        *,
        scope: TenantScope,
    ) -> tuple[GoalRecord, ...]:
        """Return Goals whose canonical graph/subgraph contains one Work Item."""
        rows: list[GoalRecord] = []
        for goal in self.list(scope=scope):
            if any(
                ref == work_item_ref
                for _project_id, ref in self._bound_refs(goal, scope=scope)
            ):
                rows.append(goal)
        rows.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return tuple(rows)

    def progress(self, goal_id: str, *, scope: TenantScope) -> GoalProgress:
        goal = self.get(goal_id, scope=scope)
        bound_refs = set(self._bound_refs(goal, scope=scope))
        completed = failed = cancelled = blocked = runnable = 0

        for project_id in sorted({project for project, _ in bound_refs}):
            snapshot = self.work_graph.snapshot(project_id, scope=scope)
            for node in snapshot.nodes:
                if (project_id, node.ref) not in bound_refs:
                    continue
                if node.terminal_outcome == "completed":
                    completed += 1
                elif node.terminal_outcome == "failed":
                    failed += 1
                elif node.terminal_outcome == "cancelled":
                    cancelled += 1
                elif node.readiness.status == WorkReadinessStatus.BLOCKED:
                    blocked += 1
                elif node.readiness.status == WorkReadinessStatus.RUNNABLE:
                    runnable += 1

        total = len(bound_refs)
        active = total - completed - failed - cancelled
        return GoalProgress(
            project_count=len({item.project_id for item in goal.work_graph_bindings}),
            work_item_count=total,
            completed=completed,
            failed=failed,
            cancelled=cancelled,
            active=active,
            runnable=runnable,
            blocked=blocked,
            completion_fraction=(completed / total if total else 0.0),
        )

    @staticmethod
    def _health_from_progress(
        goal: GoalRecord,
        progress: GoalProgress,
        *,
        now: float,
    ) -> GoalHealthSnapshot:
        if goal.status == GoalStatus.PAUSED:
            return GoalHealthSnapshot(
                health=GoalHealth.BLOCKED,
                reasons=("goal status is paused",),
            )
        if goal.status == GoalStatus.COMPLETED:
            return GoalHealthSnapshot(
                health=GoalHealth.ON_TRACK,
                reasons=("goal is completed",),
            )
        if goal.status == GoalStatus.CANCELLED:
            return GoalHealthSnapshot(
                health=GoalHealth.AT_RISK,
                reasons=("goal is cancelled",),
            )
        if (
            progress.active > 0
            and progress.blocked > 0
            and progress.runnable == 0
        ):
            return GoalHealthSnapshot(
                health=GoalHealth.BLOCKED,
                reasons=("all non-terminal bound work is blocked",),
            )

        risks: list[str] = []
        if progress.failed:
            risks.append(f"{progress.failed} bound work item(s) failed")
        if progress.cancelled:
            risks.append(f"{progress.cancelled} bound work item(s) cancelled")
        if (
            goal.target_date is not None
            and now > goal.target_date
            and progress.completion_fraction < 1.0
        ):
            risks.append("goal target date has passed")
        severe = [
            item
            for item in goal.risks
            if item.level in {GoalRiskLevel.HIGH, GoalRiskLevel.CRITICAL}
        ]
        if severe:
            risks.append(f"{len(severe)} high/critical risk(s) recorded")
        if risks:
            return GoalHealthSnapshot(
                health=GoalHealth.AT_RISK,
                reasons=tuple(risks),
            )
        if progress.work_item_count == 0:
            return GoalHealthSnapshot(
                health=GoalHealth.UNKNOWN,
                reasons=("goal has no bound subordinate work",),
            )
        return GoalHealthSnapshot(
            health=GoalHealth.ON_TRACK,
            reasons=("bound work has no deterministic blocking/risk signal",),
        )

    def health(
        self,
        goal_id: str,
        *,
        scope: TenantScope,
        now: float | None = None,
    ) -> GoalHealthSnapshot:
        goal = self.get(goal_id, scope=scope)
        progress = self.progress(goal_id, scope=scope)
        return self._health_from_progress(
            goal,
            progress,
            now=time.time() if now is None else now,
        )

    def snapshot(
        self,
        goal_id: str,
        *,
        scope: TenantScope,
        now: float | None = None,
    ) -> GoalSnapshot:
        goal = self.get(goal_id, scope=scope)
        progress = self.progress(goal_id, scope=scope)
        return GoalSnapshot(
            goal=goal,
            progress=progress,
            health=self._health_from_progress(
                goal,
                progress,
                now=time.time() if now is None else now,
            ),
        )
