from __future__ import annotations

import time

from codex_web.goal_decomposition import (
    GoalDecompositionCommitItem,
    GoalDecompositionCommitState,
    GoalDecompositionEvent,
    GoalDecompositionProposal,
    GoalDecompositionProposalCreate,
    GoalDecompositionProposalRevise,
    GoalDecompositionRevision,
    GoalDecompositionReview,
    GoalDecompositionReviewDecision,
    GoalDecompositionState,
    GoalDecompositionStatus,
    GoalProposedWorkItem,
)
from codex_web.goals import GoalStatus
from codex_web.identity import TenantScope
from codex_web.services.goals import GoalNotFoundError, GoalService
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.storage.goal_decompositions import GoalDecompositionStore


class GoalDecompositionError(RuntimeError):
    pass


class GoalDecompositionNotFoundError(GoalDecompositionError):
    pass


class GoalDecompositionConflictError(GoalDecompositionError):
    pass


class GoalDecompositionScopeError(GoalDecompositionError):
    pass


class GoalDecompositionService:
    """Canonical bounded proposal/review state before any work is created."""

    def __init__(
        self,
        store: GoalDecompositionStore,
        goals: GoalService,
        projects: ProjectService,
    ) -> None:
        self.store = store
        self.goals = goals
        self.projects = projects

    @staticmethod
    def _visible(
        proposal: GoalDecompositionProposal,
        scope: TenantScope,
    ) -> bool:
        return (
            proposal.organization_id == scope.organization_id
            and proposal.workspace_id == scope.workspace_id
        )

    def _proposal(
        self,
        state: GoalDecompositionState,
        proposal_id: str,
        scope: TenantScope,
    ) -> GoalDecompositionProposal:
        proposal = next(
            (item for item in state.proposals if item.id == proposal_id),
            None,
        )
        if proposal is None or not self._visible(proposal, scope):
            raise GoalDecompositionNotFoundError(
                "goal decomposition proposal not found"
            )
        return proposal

    @staticmethod
    def _assert_nonterminal_goal(status: GoalStatus) -> None:
        if status in {GoalStatus.COMPLETED, GoalStatus.CANCELLED}:
            raise GoalDecompositionConflictError(
                f"goal decomposition is unavailable for terminal goal status {status.value}"
            )

    def _validate_items(
        self,
        items: tuple[GoalProposedWorkItem, ...],
        *,
        max_items: int,
        max_depth: int,
        scope: TenantScope,
    ) -> None:
        if not items:
            raise GoalDecompositionConflictError(
                "goal decomposition must propose at least one work item"
            )
        if len(items) > max_items:
            raise GoalDecompositionConflictError(
                f"goal decomposition proposes {len(items)} items but max_items is {max_items}"
            )

        by_id = {item.id: item for item in items}
        if len(by_id) != len(items):
            raise GoalDecompositionConflictError(
                "goal decomposition item IDs must be unique"
            )

        for item in items:
            try:
                self.projects.get(item.project_id, scope)
            except ProjectNotFoundError as exc:
                raise GoalDecompositionScopeError(
                    f"proposal project not found: {item.project_id}"
                ) from exc
            if (
                item.parent_item_id is not None
                and item.parent_item_id not in by_id
            ):
                raise GoalDecompositionConflictError(
                    f"proposal parent item not found: {item.parent_item_id}"
                )
            if (
                item.parent_item_id is not None
                and by_id[item.parent_item_id].project_id != item.project_id
            ):
                raise GoalDecompositionConflictError(
                    "proposal parent relationships must stay within one project"
                )
            missing_blockers = [
                value
                for value in item.blocked_by_item_ids
                if value not in by_id
            ]
            if missing_blockers:
                raise GoalDecompositionConflictError(
                    "proposal blocker item not found: "
                    + ", ".join(sorted(missing_blockers))
                )
            if any(
                by_id[blocker].project_id != item.project_id
                for blocker in item.blocked_by_item_ids
            ):
                raise GoalDecompositionConflictError(
                    "proposal blocking relationships must stay within one project"
                )

        depths: dict[str, int] = {}
        visiting: set[str] = set()

        def depth(item_id: str) -> int:
            cached = depths.get(item_id)
            if cached is not None:
                return cached
            if item_id in visiting:
                raise GoalDecompositionConflictError(
                    "goal decomposition parent relationships contain a cycle"
                )
            visiting.add(item_id)
            parent_id = by_id[item_id].parent_item_id
            value = 1 if parent_id is None else depth(parent_id) + 1
            visiting.remove(item_id)
            depths[item_id] = value
            return value

        observed_depth = max(depth(item.id) for item in items)
        if observed_depth > max_depth:
            raise GoalDecompositionConflictError(
                f"goal decomposition depth {observed_depth} exceeds max_depth {max_depth}"
            )

        adjacency: dict[str, set[str]] = {
            item.id: set() for item in items
        }
        for item in items:
            for blocker in item.blocked_by_item_ids:
                adjacency[blocker].add(item.id)

        visiting.clear()
        visited: set[str] = set()

        def visit(item_id: str) -> None:
            if item_id in visited:
                return
            if item_id in visiting:
                raise GoalDecompositionConflictError(
                    "goal decomposition blocking relationships contain a cycle"
                )
            visiting.add(item_id)
            for child in sorted(adjacency[item_id]):
                visit(child)
            visiting.remove(item_id)
            visited.add(item_id)

        for item_id in sorted(adjacency):
            visit(item_id)

    @staticmethod
    def _revision(
        proposal: GoalDecompositionProposal,
        *,
        reason: str,
        actor_id: str,
        revised_at: float,
    ) -> GoalDecompositionRevision:
        return GoalDecompositionRevision(
            proposal_id=proposal.id,
            revision=proposal.revision,
            snapshot=proposal.model_copy(deep=True),
            reason=reason,
            revised_by=actor_id,
            revised_at=revised_at,
        )

    @staticmethod
    def _event(
        proposal: GoalDecompositionProposal,
        *,
        event_type: str,
        reason: str,
        actor_id: str,
        occurred_at: float,
    ) -> GoalDecompositionEvent:
        return GoalDecompositionEvent(
            proposal_id=proposal.id,
            goal_id=proposal.goal_id,
            event_type=event_type,
            revision=proposal.revision,
            actor_id=actor_id,
            reason=reason,
            occurred_at=occurred_at,
        )

    def create(
        self,
        goal_id: str,
        payload: GoalDecompositionProposalCreate,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> GoalDecompositionProposal:
        try:
            goal = self.goals.get(goal_id, scope=scope)
        except GoalNotFoundError as exc:
            raise GoalDecompositionNotFoundError("goal not found") from exc
        self._assert_nonterminal_goal(goal.status)
        if (
            payload.expected_goal_revision is not None
            and payload.expected_goal_revision != goal.revision
        ):
            raise GoalDecompositionConflictError(
                "goal changed while decomposition proposal was being generated"
            )
        self._validate_items(
            payload.items,
            max_items=payload.limits.max_items,
            max_depth=payload.limits.max_depth,
            scope=scope,
        )

        now = time.time()
        proposal = GoalDecompositionProposal(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            goal_id=goal.id,
            goal_revision=goal.revision,
            items=payload.items,
            limits=payload.limits,
            reasoning_budget=goal.budget.model_copy(deep=True),
            model_invocation_id=payload.model_invocation_id,
            created_by=actor_id,
            updated_by=actor_id,
            created_at=now,
            updated_at=now,
        )

        def apply(state: GoalDecompositionState) -> GoalDecompositionState:
            state.proposals.append(proposal)
            state.revisions.append(
                self._revision(
                    proposal,
                    reason=payload.reason,
                    actor_id=actor_id,
                    revised_at=now,
                )
            )
            state.events.append(
                self._event(
                    proposal,
                    event_type="goal_decomposition.proposed",
                    reason=payload.reason,
                    actor_id=actor_id,
                    occurred_at=now,
                )
            )
            return state

        self.store.update(apply)
        return proposal

    def get(
        self,
        proposal_id: str,
        *,
        scope: TenantScope,
    ) -> GoalDecompositionProposal:
        return self._proposal(self.store.load(), proposal_id, scope)

    def list(
        self,
        goal_id: str,
        *,
        scope: TenantScope,
    ) -> tuple[GoalDecompositionProposal, ...]:
        try:
            self.goals.get(goal_id, scope=scope)
        except GoalNotFoundError as exc:
            raise GoalDecompositionNotFoundError("goal not found") from exc
        rows = [
            item
            for item in self.store.load().proposals
            if item.goal_id == goal_id and self._visible(item, scope)
        ]
        rows.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return tuple(rows)

    def revise(
        self,
        proposal_id: str,
        payload: GoalDecompositionProposalRevise,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> GoalDecompositionProposal:
        current = self.get(proposal_id, scope=scope)
        if current.status not in {
            GoalDecompositionStatus.PROPOSED,
            GoalDecompositionStatus.REJECTED,
        }:
            raise GoalDecompositionConflictError(
                f"cannot revise decomposition in {current.status.value} state"
            )
        try:
            goal = self.goals.get(current.goal_id, scope=scope)
        except GoalNotFoundError as exc:
            raise GoalDecompositionNotFoundError("goal not found") from exc
        self._assert_nonterminal_goal(goal.status)
        limits = payload.limits or current.limits
        self._validate_items(
            payload.items,
            max_items=limits.max_items,
            max_depth=limits.max_depth,
            scope=scope,
        )
        now = time.time()
        updated = current.model_copy(
            update={
                "goal_revision": goal.revision,
                "revision": current.revision + 1,
                "status": GoalDecompositionStatus.PROPOSED,
                "items": payload.items,
                "limits": limits,
                "reasoning_budget": goal.budget.model_copy(deep=True),
                "model_invocation_id": payload.model_invocation_id,
                "updated_by": actor_id,
                "updated_at": now,
                "reviewed_by": None,
                "reviewed_at": None,
                "review_reason": None,
                "commit_items": (),
                "commit_started_by": None,
                "commit_started_at": None,
                "commit_error": None,
                "committed_at": None,
                "committed_work_item_refs": (),
            },
            deep=True,
        )

        def apply(state: GoalDecompositionState) -> GoalDecompositionState:
            stored = self._proposal(state, proposal_id, scope)
            index = state.proposals.index(stored)
            state.proposals[index] = updated
            state.revisions.append(
                self._revision(
                    updated,
                    reason=payload.reason,
                    actor_id=actor_id,
                    revised_at=now,
                )
            )
            state.events.append(
                self._event(
                    updated,
                    event_type="goal_decomposition.revised",
                    reason=payload.reason,
                    actor_id=actor_id,
                    occurred_at=now,
                )
            )
            return state

        self.store.update(apply)
        return updated

    def review(
        self,
        proposal_id: str,
        payload: GoalDecompositionReview,
        *,
        scope: TenantScope,
        actor_id: str,
    ) -> GoalDecompositionProposal:
        current = self.get(proposal_id, scope=scope)
        if current.status != GoalDecompositionStatus.PROPOSED:
            raise GoalDecompositionConflictError(
                f"cannot review decomposition in {current.status.value} state"
            )
        try:
            goal = self.goals.get(current.goal_id, scope=scope)
        except GoalNotFoundError as exc:
            raise GoalDecompositionNotFoundError("goal not found") from exc

        if (
            payload.decision == GoalDecompositionReviewDecision.ACCEPT
            and goal.revision != current.goal_revision
        ):
            raise GoalDecompositionConflictError(
                "goal changed after decomposition proposal; revise the proposal before accepting"
            )

        now = time.time()
        status = (
            GoalDecompositionStatus.ACCEPTED
            if payload.decision == GoalDecompositionReviewDecision.ACCEPT
            else GoalDecompositionStatus.REJECTED
        )
        updated = current.model_copy(
            update={
                "revision": current.revision + 1,
                "status": status,
                "updated_by": actor_id,
                "updated_at": now,
                "reviewed_by": actor_id,
                "reviewed_at": now,
                "review_reason": payload.reason,
            },
            deep=True,
        )

        def apply(state: GoalDecompositionState) -> GoalDecompositionState:
            stored = self._proposal(state, proposal_id, scope)
            index = state.proposals.index(stored)
            state.proposals[index] = updated
            state.revisions.append(
                self._revision(
                    updated,
                    reason=payload.reason,
                    actor_id=actor_id,
                    revised_at=now,
                )
            )
            state.events.append(
                self._event(
                    updated,
                    event_type=f"goal_decomposition.{status.value}",
                    reason=payload.reason,
                    actor_id=actor_id,
                    occurred_at=now,
                )
            )
            return state

        self.store.update(apply)
        return updated

    @staticmethod
    def _validate_commit_plan(
        proposal: GoalDecompositionProposal,
        commit_items: tuple[GoalDecompositionCommitItem, ...],
    ) -> None:
        proposed = {item.id: item for item in proposal.items}
        planned = {item.proposal_item_id: item for item in commit_items}
        if set(planned) != set(proposed) or len(planned) != len(commit_items):
            raise GoalDecompositionConflictError(
                "goal decomposition commit plan must cover every proposed item exactly once"
            )
        correlations: set[str] = set()
        for item_id, record in planned.items():
            item = proposed[item_id]
            if record.project_id != item.project_id:
                raise GoalDecompositionConflictError(
                    f"commit project mismatch for proposed item {item_id}"
                )
            if record.correlation_id in correlations:
                raise GoalDecompositionConflictError(
                    "goal decomposition commit correlation IDs must be unique"
                )
            correlations.add(record.correlation_id)
            if (
                record.state != GoalDecompositionCommitState.PLANNED
                or record.intent_id is not None
                or record.work_item_ref is not None
            ):
                raise GoalDecompositionConflictError(
                    "new goal decomposition commit records must start planned"
                )

    def begin_commit(
        self,
        proposal_id: str,
        commit_items: tuple[GoalDecompositionCommitItem, ...],
        *,
        scope: TenantScope,
        actor_id: str,
        reason: str,
    ) -> GoalDecompositionProposal:
        current = self.get(proposal_id, scope=scope)
        if current.status != GoalDecompositionStatus.ACCEPTED:
            raise GoalDecompositionConflictError(
                f"cannot commit decomposition in {current.status.value} state"
            )
        try:
            goal = self.goals.get(current.goal_id, scope=scope)
        except GoalNotFoundError as exc:
            raise GoalDecompositionNotFoundError("goal not found") from exc
        self._assert_nonterminal_goal(goal.status)
        if goal.revision != current.goal_revision:
            raise GoalDecompositionConflictError(
                "goal changed after decomposition acceptance; revise the proposal before commit"
            )
        self._validate_commit_plan(current, commit_items)
        now = time.time()
        updated = current.model_copy(
            update={
                "revision": current.revision + 1,
                "status": GoalDecompositionStatus.COMMITTING,
                "commit_items": commit_items,
                "commit_started_by": actor_id,
                "commit_started_at": now,
                "commit_error": None,
                "updated_by": actor_id,
                "updated_at": now,
            },
            deep=True,
        )

        def apply(state: GoalDecompositionState) -> GoalDecompositionState:
            stored = self._proposal(state, proposal_id, scope)
            if stored.status != GoalDecompositionStatus.ACCEPTED:
                raise GoalDecompositionConflictError(
                    f"cannot commit decomposition in {stored.status.value} state"
                )
            index = state.proposals.index(stored)
            state.proposals[index] = updated
            state.revisions.append(
                self._revision(
                    updated,
                    reason=reason,
                    actor_id=actor_id,
                    revised_at=now,
                )
            )
            state.events.append(
                self._event(
                    updated,
                    event_type="goal_decomposition.committing",
                    reason=reason,
                    actor_id=actor_id,
                    occurred_at=now,
                )
            )
            return state

        self.store.update(apply)
        return updated

    @staticmethod
    def _validate_commit_progress(
        current: GoalDecompositionProposal,
        commit_items: tuple[GoalDecompositionCommitItem, ...],
    ) -> None:
        existing = {item.proposal_item_id: item for item in current.commit_items}
        incoming = {item.proposal_item_id: item for item in commit_items}
        if set(existing) != set(incoming) or len(incoming) != len(commit_items):
            raise GoalDecompositionConflictError(
                "goal decomposition commit progress must preserve the commit plan"
            )
        for item_id, before in existing.items():
            after = incoming[item_id]
            if (
                after.project_id != before.project_id
                or after.binding_id != before.binding_id
                or after.correlation_id != before.correlation_id
            ):
                raise GoalDecompositionConflictError(
                    f"commit plan identity changed for proposed item {item_id}"
                )
            if (
                after.work_item_ref is not None
                and after.state != GoalDecompositionCommitState.SUCCEEDED
            ):
                raise GoalDecompositionConflictError(
                    "only succeeded commit items may carry a Work Item ref"
                )

    def record_commit_progress(
        self,
        proposal_id: str,
        commit_items: tuple[GoalDecompositionCommitItem, ...],
        *,
        scope: TenantScope,
        actor_id: str,
        reason: str,
        error: str | None = None,
    ) -> GoalDecompositionProposal:
        current = self.get(proposal_id, scope=scope)
        if current.status != GoalDecompositionStatus.COMMITTING:
            raise GoalDecompositionConflictError(
                f"cannot update commit progress in {current.status.value} state"
            )
        self._validate_commit_progress(current, commit_items)
        if current.commit_items == commit_items and current.commit_error == error:
            return current
        now = time.time()
        updated = current.model_copy(
            update={
                "revision": current.revision + 1,
                "commit_items": commit_items,
                "commit_error": error,
                "updated_by": actor_id,
                "updated_at": now,
            },
            deep=True,
        )

        def apply(state: GoalDecompositionState) -> GoalDecompositionState:
            stored = self._proposal(state, proposal_id, scope)
            if stored.status != GoalDecompositionStatus.COMMITTING:
                raise GoalDecompositionConflictError(
                    f"cannot update commit progress in {stored.status.value} state"
                )
            index = state.proposals.index(stored)
            state.proposals[index] = updated
            state.revisions.append(
                self._revision(
                    updated,
                    reason=reason,
                    actor_id=actor_id,
                    revised_at=now,
                )
            )
            state.events.append(
                self._event(
                    updated,
                    event_type="goal_decomposition.commit_progress",
                    reason=reason,
                    actor_id=actor_id,
                    occurred_at=now,
                )
            )
            return state

        self.store.update(apply)
        return updated

    def complete_commit(
        self,
        proposal_id: str,
        *,
        scope: TenantScope,
        actor_id: str,
        reason: str,
    ) -> GoalDecompositionProposal:
        current = self.get(proposal_id, scope=scope)
        if current.status != GoalDecompositionStatus.COMMITTING:
            raise GoalDecompositionConflictError(
                f"cannot complete commit in {current.status.value} state"
            )
        if not current.commit_items or any(
            item.state != GoalDecompositionCommitState.SUCCEEDED
            or not item.work_item_ref
            for item in current.commit_items
        ):
            raise GoalDecompositionConflictError(
                "goal decomposition commit cannot complete until every item succeeded"
            )
        refs_by_id = {
            item.proposal_item_id: item.work_item_ref
            for item in current.commit_items
        }
        ordered_refs = tuple(refs_by_id[item.id] for item in current.items)
        if len(set(ordered_refs)) != len(ordered_refs):
            raise GoalDecompositionConflictError(
                "goal decomposition commit produced duplicate Work Item refs"
            )
        now = time.time()
        updated = current.model_copy(
            update={
                "revision": current.revision + 1,
                "status": GoalDecompositionStatus.COMMITTED,
                "commit_error": None,
                "committed_at": now,
                "committed_work_item_refs": ordered_refs,
                "updated_by": actor_id,
                "updated_at": now,
            },
            deep=True,
        )

        def apply(state: GoalDecompositionState) -> GoalDecompositionState:
            stored = self._proposal(state, proposal_id, scope)
            if stored.status != GoalDecompositionStatus.COMMITTING:
                raise GoalDecompositionConflictError(
                    f"cannot complete commit in {stored.status.value} state"
                )
            index = state.proposals.index(stored)
            state.proposals[index] = updated
            state.revisions.append(
                self._revision(
                    updated,
                    reason=reason,
                    actor_id=actor_id,
                    revised_at=now,
                )
            )
            state.events.append(
                self._event(
                    updated,
                    event_type="goal_decomposition.committed",
                    reason=reason,
                    actor_id=actor_id,
                    occurred_at=now,
                )
            )
            return state

        self.store.update(apply)
        return updated

    def revisions(
        self,
        proposal_id: str,
        *,
        scope: TenantScope,
    ) -> tuple[GoalDecompositionRevision, ...]:
        self.get(proposal_id, scope=scope)
        rows = [
            item
            for item in self.store.load().revisions
            if item.proposal_id == proposal_id
        ]
        rows.sort(key=lambda item: item.revision)
        return tuple(rows)

    def events(
        self,
        goal_id: str,
        *,
        scope: TenantScope,
        limit: int = 100,
    ) -> tuple[GoalDecompositionEvent, ...]:
        try:
            self.goals.get(goal_id, scope=scope)
        except GoalNotFoundError as exc:
            raise GoalDecompositionNotFoundError("goal not found") from exc
        bounded = max(1, min(int(limit), 500))
        visible_ids = {
            item.id
            for item in self.store.load().proposals
            if item.goal_id == goal_id and self._visible(item, scope)
        }
        rows = [
            item
            for item in self.store.load().events
            if item.goal_id == goal_id and item.proposal_id in visible_ids
        ]
        rows.sort(key=lambda item: item.occurred_at, reverse=True)
        return tuple(rows[:bounded])
