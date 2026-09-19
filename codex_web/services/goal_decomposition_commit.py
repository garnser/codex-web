from __future__ import annotations

import time
from collections import defaultdict

from codex_web.action_intents import (
    ActionDecisionOutcome,
    ActionDecisionSnapshot,
    ActionIntentCreate,
    ActionIntentStatus,
)
from codex_web.action_providers import ActionProviderBinding, ActionRequest
from codex_web.goal_decomposition import (
    GoalDecompositionCommitItem,
    GoalDecompositionCommitState,
    GoalDecompositionProposal,
    GoalDecompositionStatus,
    GoalProposedWorkItem,
)
from codex_web.goals import GoalUpdate, GoalWorkGraphBinding
from codex_web.identity import AuthenticationActor
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import (
    ActionExecutionService,
    ActionProviderRegistry,
)
from codex_web.services.goal_decompositions import (
    GoalDecompositionConflictError,
    GoalDecompositionError,
    GoalDecompositionService,
)
from codex_web.services.goals import GoalService
from codex_web.services.task_source_action_provider import (
    TASK_SOURCE_ACTION_PROVIDER_INSTANCE,
    TASK_SOURCE_ACTION_PROVIDER_TYPE,
    TASK_SOURCE_CREATE_ACTION_ID,
)
from codex_web.services.work_graph import WorkGraphService
from codex_web.work_graph import WorkGraphEdgeCreate, WorkGraphRelation


class GoalDecompositionCommitError(GoalDecompositionError):
    pass


class GoalDecompositionCommitBindingError(GoalDecompositionCommitError):
    pass


class GoalDecompositionCommitService:
    """Queue accepted Goal work as ActionIntents and reconcile canonical results."""

    def __init__(
        self,
        proposals: GoalDecompositionService,
        goals: GoalService,
        work_graph: WorkGraphService,
        action_intents: ActionIntentService,
        action_execution: ActionExecutionService,
        action_providers: ActionProviderRegistry,
    ) -> None:
        self.proposals = proposals
        self.goals = goals
        self.work_graph = work_graph
        self.action_intents = action_intents
        self.action_execution = action_execution
        self.action_providers = action_providers

    @staticmethod
    def _correlation_id(proposal_id: str, item_id: str) -> str:
        return f"goal-decomposition:{proposal_id}:{item_id}"

    def _binding(
        self,
        project_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ActionProviderBinding:
        candidates = [
            item
            for item in self.action_providers.list_bindings(actor)
            if item.enabled
            and item.provider_type == TASK_SOURCE_ACTION_PROVIDER_TYPE
            and item.provider_instance == TASK_SOURCE_ACTION_PROVIDER_INSTANCE
            and item.project_id in {None, project_id}
        ]
        exact = [item for item in candidates if item.project_id == project_id]
        eligible = exact if exact else [item for item in candidates if item.project_id is None]
        if not eligible:
            raise GoalDecompositionCommitBindingError(
                "no enabled task-source/authoritative ActionProvider binding "
                f"permits project {project_id}"
            )
        if len(eligible) != 1:
            raise GoalDecompositionCommitBindingError(
                "multiple task-source/authoritative ActionProvider bindings "
                f"match project {project_id}; bind the project unambiguously"
            )
        return eligible[0]

    @staticmethod
    def _body(item: GoalProposedWorkItem) -> str:
        text = item.description.strip()
        if item.expected_result:
            text += "\n\nExpected result:\n" + item.expected_result.strip()
        return text

    def _request(
        self,
        proposal: GoalDecompositionProposal,
        item: GoalProposedWorkItem,
        *,
        actor: AuthenticationActor,
        correlation_id: str,
    ) -> ActionRequest:
        return ActionRequest(
            action_id=TASK_SOURCE_CREATE_ACTION_ID,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=item.project_id,
            parameters={
                "title": item.title,
                "body": self._body(item),
                "owners": (
                    [item.owner_identity_id]
                    if item.owner_identity_id is not None
                    else []
                ),
                "labels": list(item.labels),
            },
            correlation_id=correlation_id,
            requested_by=actor.identity_id,
        )

    async def _plan(
        self,
        proposal: GoalDecompositionProposal,
        *,
        actor: AuthenticationActor,
    ) -> tuple[GoalDecompositionCommitItem, ...]:
        rows: list[GoalDecompositionCommitItem] = []
        for item in proposal.items:
            binding = self._binding(item.project_id, actor=actor)
            correlation_id = self._correlation_id(proposal.id, item.id)
            request = self._request(
                proposal,
                item,
                actor=actor,
                correlation_id=correlation_id,
            )
            await self.action_execution.prepare(
                binding.id,
                request,
                actor=actor,
            )
            rows.append(
                GoalDecompositionCommitItem(
                    proposal_item_id=item.id,
                    project_id=item.project_id,
                    binding_id=binding.id,
                    correlation_id=correlation_id,
                )
            )
        return tuple(rows)

    @staticmethod
    def _commit_state(status: ActionIntentStatus) -> GoalDecompositionCommitState:
        if status in {
            ActionIntentStatus.PENDING,
            ActionIntentStatus.CLAIMED,
            ActionIntentStatus.EXECUTING,
        }:
            return GoalDecompositionCommitState.QUEUED
        if status == ActionIntentStatus.SUCCEEDED:
            return GoalDecompositionCommitState.SUCCEEDED
        if status == ActionIntentStatus.FAILED:
            return GoalDecompositionCommitState.FAILED
        if status == ActionIntentStatus.CANCELLED:
            return GoalDecompositionCommitState.CANCELLED
        if status == ActionIntentStatus.UNCERTAIN:
            return GoalDecompositionCommitState.UNCERTAIN
        if status == ActionIntentStatus.REQUIRES_RECONCILIATION:
            return GoalDecompositionCommitState.REQUIRES_RECONCILIATION
        if status == ActionIntentStatus.ROLLED_BACK:
            return GoalDecompositionCommitState.ROLLED_BACK
        raise GoalDecompositionCommitError(
            f"unsupported ActionIntent status: {status}"
        )

    def _queue_missing(
        self,
        proposal: GoalDecompositionProposal,
        *,
        actor: AuthenticationActor,
        reason: str,
    ) -> GoalDecompositionProposal:
        by_id = {item.id: item for item in proposal.items}
        changed: list[GoalDecompositionCommitItem] = []
        for record in proposal.commit_items:
            if record.intent_id is not None:
                changed.append(record)
                continue
            item = by_id[record.proposal_item_id]
            request = self._request(
                proposal,
                item,
                actor=actor,
                correlation_id=record.correlation_id,
            )
            try:
                # Re-resolve the binding to fail closed if it was disabled or
                # re-scoped after the side-effect-free preflight.
                self.action_providers.binding(record.binding_id, actor)
                intent = self.action_intents.create(
                    ActionIntentCreate(
                        binding_id=record.binding_id,
                        request=request,
                        goal_id=proposal.goal_id,
                        authority_decision=ActionDecisionSnapshot(
                            decision_id=f"{proposal.id}:review",
                            outcome=ActionDecisionOutcome.ALLOW,
                            source="approval:goal-decomposition-review",
                            reason=proposal.review_reason or reason,
                            evaluated_at=proposal.reviewed_at,
                        ),
                        verification_required=False,
                    ),
                    actor=actor,
                )
            except Exception as exc:
                changed.append(
                    record.model_copy(
                        update={
                            "state": GoalDecompositionCommitState.PLANNED,
                            "last_error": str(exc)[:500],
                            "updated_at": time.time(),
                        }
                    )
                )
                continue
            changed.append(
                record.model_copy(
                    update={
                        "intent_id": intent.id,
                        "state": self._commit_state(intent.status),
                        "last_error": intent.last_error,
                        "updated_at": time.time(),
                    }
                )
            )
        return self.proposals.record_commit_progress(
            proposal.id,
            tuple(changed),
            scope=actor.tenant,
            actor_id=actor.identity_id,
            reason=reason,
            error=(
                "one or more task creation intents could not be queued"
                if any(item.intent_id is None for item in changed)
                else None
            ),
        )

    async def commit(
        self,
        proposal_id: str,
        *,
        actor: AuthenticationActor,
        reason: str,
    ) -> GoalDecompositionProposal:
        current = self.proposals.get(proposal_id, scope=actor.tenant)
        if current.status == GoalDecompositionStatus.COMMITTED:
            return current
        if current.status == GoalDecompositionStatus.ACCEPTED:
            plan = await self._plan(current, actor=actor)
            current = self.proposals.begin_commit(
                proposal_id,
                plan,
                scope=actor.tenant,
                actor_id=actor.identity_id,
                reason=reason,
            )
        elif current.status != GoalDecompositionStatus.COMMITTING:
            raise GoalDecompositionConflictError(
                f"cannot commit decomposition in {current.status.value} state"
            )
        return self._queue_missing(
            current,
            actor=actor,
            reason=reason,
        )

    @staticmethod
    def _successful_work_item_ref(history: dict) -> str | None:
        receipts = history.get("receipts") or []
        for receipt in reversed(receipts):
            result = receipt.get("result") or {}
            if result.get("status") != "succeeded":
                continue
            output = result.get("output") or {}
            ref = str(output.get("work_item_ref") or "").strip()
            if ref:
                return ref
        return None

    def _work_item_exists(
        self,
        project_id: str,
        work_item_ref: str,
        *,
        actor: AuthenticationActor,
    ) -> bool:
        snapshot = self.work_graph.snapshot(
            project_id,
            scope=actor.tenant,
        )
        return any(node.ref == work_item_ref for node in snapshot.nodes)

    def _reconciled_records(
        self,
        proposal: GoalDecompositionProposal,
        *,
        actor: AuthenticationActor,
    ) -> tuple[GoalDecompositionCommitItem, ...]:
        rows: list[GoalDecompositionCommitItem] = []
        for record in proposal.commit_items:
            if record.intent_id is None:
                rows.append(record)
                continue
            intent = self.action_intents.get(record.intent_id, actor)
            state = self._commit_state(intent.status)
            work_item_ref = record.work_item_ref
            error = intent.last_error
            if intent.status == ActionIntentStatus.SUCCEEDED:
                history = self.action_intents.history(intent.id, actor)
                work_item_ref = self._successful_work_item_ref(history)
                if not work_item_ref or not self._work_item_exists(
                    record.project_id,
                    work_item_ref,
                    actor=actor,
                ):
                    state = GoalDecompositionCommitState.REQUIRES_RECONCILIATION
                    error = (
                        "ActionIntent succeeded without a tenant-visible canonical "
                        "Work Item projection"
                    )
                    work_item_ref = None
                else:
                    error = None
            elif state != GoalDecompositionCommitState.SUCCEEDED:
                work_item_ref = None
            rows.append(
                record.model_copy(
                    update={
                        "state": state,
                        "work_item_ref": work_item_ref,
                        "last_error": error,
                        "updated_at": time.time(),
                    }
                )
            )
        return tuple(rows)

    def _materialize_graph(
        self,
        proposal: GoalDecompositionProposal,
        *,
        actor: AuthenticationActor,
    ) -> None:
        refs = {
            item.proposal_item_id: item.work_item_ref
            for item in proposal.commit_items
        }
        for item in proposal.items:
            target_ref = refs[item.id]
            assert target_ref is not None
            if item.parent_item_id is not None:
                source_ref = refs[item.parent_item_id]
                assert source_ref is not None
                self.work_graph.add_edge(
                    WorkGraphEdgeCreate(
                        relation=WorkGraphRelation.PARENT,
                        source_ref=source_ref,
                        target_ref=target_ref,
                        reason=f"Goal decomposition {proposal.id}",
                    ),
                    scope=actor.tenant,
                    actor_id=actor.identity_id,
                )
            for blocker_id in item.blocked_by_item_ids:
                source_ref = refs[blocker_id]
                assert source_ref is not None
                self.work_graph.add_edge(
                    WorkGraphEdgeCreate(
                        relation=WorkGraphRelation.BLOCKS,
                        source_ref=source_ref,
                        target_ref=target_ref,
                        reason=f"Goal decomposition {proposal.id}",
                    ),
                    scope=actor.tenant,
                    actor_id=actor.identity_id,
                )

    def _bind_goal_roots(
        self,
        proposal: GoalDecompositionProposal,
        *,
        actor: AuthenticationActor,
    ) -> None:
        current = self.goals.get(proposal.goal_id, scope=actor.tenant)
        refs = {
            item.proposal_item_id: item.work_item_ref
            for item in proposal.commit_items
        }
        roots: dict[str, list[str]] = defaultdict(list)
        for item in proposal.items:
            if item.parent_item_id is None:
                ref = refs[item.id]
                assert ref is not None
                roots[item.project_id].append(ref)

        bindings = list(current.work_graph_bindings)
        by_project = {
            binding.project_id: index
            for index, binding in enumerate(bindings)
        }
        changed = False
        for project_id, generated_roots in roots.items():
            index = by_project.get(project_id)
            if index is None:
                bindings.append(
                    GoalWorkGraphBinding(
                        project_id=project_id,
                        root_work_item_refs=tuple(generated_roots),
                    )
                )
                by_project[project_id] = len(bindings) - 1
                changed = True
                continue
            existing = bindings[index]
            if not existing.root_work_item_refs:
                # Whole-project binding already includes generated work.
                continue
            merged = tuple(
                dict.fromkeys(
                    (*existing.root_work_item_refs, *generated_roots)
                )
            )
            if merged != existing.root_work_item_refs:
                bindings[index] = GoalWorkGraphBinding(
                    project_id=project_id,
                    root_work_item_refs=merged,
                )
                changed = True

        if changed:
            self.goals.revise(
                current.id,
                GoalUpdate(
                    work_graph_bindings=tuple(bindings),
                    reason=f"Bind committed Goal decomposition {proposal.id}",
                ),
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )

    def _finalize(
        self,
        proposal: GoalDecompositionProposal,
        *,
        actor: AuthenticationActor,
        reason: str,
    ) -> GoalDecompositionProposal:
        self._materialize_graph(proposal, actor=actor)
        self._bind_goal_roots(proposal, actor=actor)
        return self.proposals.complete_commit(
            proposal.id,
            scope=actor.tenant,
            actor_id=actor.identity_id,
            reason=reason,
        )

    def reconcile(
        self,
        proposal_id: str,
        *,
        actor: AuthenticationActor,
        reason: str,
    ) -> GoalDecompositionProposal:
        current = self.proposals.get(proposal_id, scope=actor.tenant)
        if current.status == GoalDecompositionStatus.COMMITTED:
            return current
        if current.status != GoalDecompositionStatus.COMMITTING:
            raise GoalDecompositionConflictError(
                f"cannot reconcile commit in {current.status.value} state"
            )

        records = self._reconciled_records(current, actor=actor)
        current = self.proposals.record_commit_progress(
            proposal_id,
            records,
            scope=actor.tenant,
            actor_id=actor.identity_id,
            reason=reason,
            error=(
                None
                if all(
                    item.state == GoalDecompositionCommitState.SUCCEEDED
                    for item in records
                )
                else "goal decomposition commit has unresolved ActionIntents"
            ),
        )
        if not all(
            item.state == GoalDecompositionCommitState.SUCCEEDED
            and item.work_item_ref
            for item in current.commit_items
        ):
            return current

        try:
            return self._finalize(
                current,
                actor=actor,
                reason=reason,
            )
        except Exception as exc:
            return self.proposals.record_commit_progress(
                proposal_id,
                current.commit_items,
                scope=actor.tenant,
                actor_id=actor.identity_id,
                reason=reason,
                error=f"commit finalization failed: {str(exc)[:450]}",
            )
