from __future__ import annotations

import time
from collections import defaultdict

from codex_web.action_intents import ActionIntentCreate, ActionIntentStatus
from codex_web.action_providers import ActionProviderBinding, ActionRequest
from codex_web.decisions import (
    Decision,
    DecisionStatus,
    DecisionWorkCommitRequest,
    DecisionWorkItemRequest,
    DecisionWorkLink,
    DecisionWorkState,
)
from codex_web.goals import GoalUpdate, GoalWorkGraphBinding
from codex_web.identity import AuthenticationActor
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import (
    ActionExecutionService,
    ActionProviderRegistry,
)
from codex_web.services.decisions import (
    DecisionConflictError,
    DecisionService,
    DecisionStateError,
    DecisionValidationError,
)
from codex_web.services.goals import GoalService
from codex_web.services.task_source_action_provider import (
    TASK_SOURCE_ACTION_PROVIDER_INSTANCE,
    TASK_SOURCE_ACTION_PROVIDER_TYPE,
    TASK_SOURCE_CREATE_ACTION_ID,
)
from codex_web.services.work_graph import WorkGraphService
from codex_web.services.work_items import WorkItemService
from codex_web.work_graph import WorkGraphEdgeCreate, WorkGraphRelation


class DecisionWorkError(RuntimeError):
    pass


class DecisionWorkBindingError(DecisionWorkError):
    pass


class DecisionWorkService:
    """Materialize approved Decision consequences through canonical action/work paths."""

    def __init__(
        self,
        decisions: DecisionService,
        goals: GoalService,
        work_items: WorkItemService,
        work_graph: WorkGraphService,
        action_intents: ActionIntentService,
        action_execution: ActionExecutionService,
        action_providers: ActionProviderRegistry,
    ) -> None:
        self.decisions = decisions
        self.goals = goals
        self.work_items = work_items
        self.work_graph = work_graph
        self.action_intents = action_intents
        self.action_execution = action_execution
        self.action_providers = action_providers

    @staticmethod
    def _correlation_id(decision_id: str, item_id: str) -> str:
        return f"decision:{decision_id}:work:{item_id}"

    @staticmethod
    def _idempotency_key(decision_id: str, item_id: str) -> str:
        return f"decision-work:{decision_id}:{item_id}"

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
            raise DecisionWorkBindingError(
                "no enabled authoritative task-source ActionProvider binding "
                f"permits project {project_id}"
            )
        if len(eligible) != 1:
            raise DecisionWorkBindingError(
                "multiple authoritative task-source ActionProvider bindings "
                f"match project {project_id}"
            )
        return eligible[0]

    @staticmethod
    def _body(decision: Decision, item: DecisionWorkItemRequest | DecisionWorkLink) -> str:
        parts = [
            item.description.strip(),
            "",
            f"Canonical Decision: {decision.id}",
        ]
        if decision.goal_id:
            parts.append(f"Canonical Goal: {decision.goal_id}")
        if item.expected_result:
            parts.extend(("", "Expected result:", item.expected_result.strip()))
        return "\n".join(parts).strip()

    def _request(
        self,
        decision: Decision,
        item: DecisionWorkItemRequest | DecisionWorkLink,
        *,
        actor: AuthenticationActor,
    ) -> ActionRequest:
        correlation_id = self._correlation_id(decision.id, item.id if isinstance(item, DecisionWorkItemRequest) else item.item_id)
        item_id = item.id if isinstance(item, DecisionWorkItemRequest) else item.item_id
        return ActionRequest(
            action_id=TASK_SOURCE_CREATE_ACTION_ID,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            project_id=item.project_id,
            parameters={
                "title": item.title,
                "body": self._body(decision, item),
                "owners": (
                    [item.owner_identity_id]
                    if item.owner_identity_id is not None
                    else []
                ),
                "labels": list(item.labels),
            },
            idempotency_key=self._idempotency_key(decision.id, item_id),
            correlation_id=correlation_id,
            requested_by=actor.identity_id,
        )

    @staticmethod
    def _state(status: ActionIntentStatus) -> DecisionWorkState:
        if status in {
            ActionIntentStatus.PENDING,
            ActionIntentStatus.CLAIMED,
            ActionIntentStatus.EXECUTING,
        }:
            return DecisionWorkState.QUEUED
        if status == ActionIntentStatus.SUCCEEDED:
            return DecisionWorkState.SUCCEEDED
        if status == ActionIntentStatus.FAILED:
            return DecisionWorkState.FAILED
        if status == ActionIntentStatus.CANCELLED:
            return DecisionWorkState.CANCELLED
        if status == ActionIntentStatus.UNCERTAIN:
            return DecisionWorkState.UNCERTAIN
        if status == ActionIntentStatus.REQUIRES_RECONCILIATION:
            return DecisionWorkState.REQUIRES_RECONCILIATION
        if status == ActionIntentStatus.ROLLED_BACK:
            return DecisionWorkState.FAILED
        raise DecisionWorkError(f"unsupported ActionIntent status: {status}")

    @staticmethod
    def _validate_graph_items(payload: DecisionWorkCommitRequest) -> None:
        by_id = {item.id: item for item in payload.items}
        for item in payload.items:
            related = tuple(
                ref
                for ref in (item.parent_item_id, *item.blocked_by_item_ids)
                if ref is not None
            )
            for related_id in related:
                other = by_id[related_id]
                if other.project_id != item.project_id:
                    raise DecisionValidationError(
                        "Decision work graph relationships cannot cross projects"
                    )

    async def _plan(
        self,
        decision: Decision,
        payload: DecisionWorkCommitRequest,
        *,
        actor: AuthenticationActor,
    ) -> tuple[DecisionWorkLink, ...]:
        self._validate_graph_items(payload)
        rows: list[DecisionWorkLink] = []
        for item in payload.items:
            binding = self._binding(item.project_id, actor=actor)
            request = self._request(decision, item, actor=actor)
            await self.action_execution.prepare(
                binding.id,
                request,
                actor=actor,
            )
            now = time.time()
            rows.append(
                DecisionWorkLink(
                    item_id=item.id,
                    project_id=item.project_id,
                    title=item.title,
                    description=item.description,
                    expected_result=item.expected_result,
                    owner_identity_id=item.owner_identity_id,
                    labels=item.labels,
                    correlation_id=self._correlation_id(decision.id, item.id),
                    binding_id=binding.id,
                    parent_item_id=item.parent_item_id,
                    blocked_by_item_ids=item.blocked_by_item_ids,
                    created_at=now,
                    updated_at=now,
                )
            )
        return tuple(rows)

    @staticmethod
    def _same_spec(
        links: tuple[DecisionWorkLink, ...],
        payload: DecisionWorkCommitRequest,
    ) -> bool:
        if {item.item_id for item in links} != {item.id for item in payload.items}:
            return False
        requested = {item.id: item for item in payload.items}
        for link in links:
            item = requested[link.item_id]
            if (
                link.project_id != item.project_id
                or link.title != item.title
                or link.description != item.description
                or link.expected_result != item.expected_result
                or link.owner_identity_id != item.owner_identity_id
                or link.labels != item.labels
                or link.parent_item_id != item.parent_item_id
                or link.blocked_by_item_ids != item.blocked_by_item_ids
            ):
                return False
        return True

    async def commit(
        self,
        decision_id: str,
        payload: DecisionWorkCommitRequest,
        *,
        actor: AuthenticationActor,
    ) -> Decision:
        decision = self.decisions.get(decision_id, actor=actor)
        self.decisions._require_edit(decision, actor)
        if decision.status != DecisionStatus.APPROVED:
            raise DecisionStateError(
                "Decision work can only be generated after canonical approval"
            )
        if decision.work_links:
            if not self._same_spec(decision.work_links, payload):
                raise DecisionConflictError(
                    "Decision already has a different durable work specification"
                )
            planned = decision
        else:
            links = await self._plan(decision, payload, actor=actor)
            planned = await self.decisions.record_work_links(
                decision.id,
                links,
                actor=actor,
                reason=f"{payload.reason}: preflighted Decision work",
            )

        changed: list[DecisionWorkLink] = []
        for link in planned.work_links:
            if link.action_intent_id is not None:
                changed.append(link)
                continue
            request = self._request(planned, link, actor=actor)
            try:
                self.action_providers.binding(link.binding_id, actor)
                intent = self.action_intents.create(
                    ActionIntentCreate(
                        binding_id=link.binding_id,
                        request=request,
                        goal_id=planned.goal_id,
                        decision_id=planned.id,
                        verification_required=False,
                    ),
                    actor=actor,
                )
                changed.append(
                    link.model_copy(
                        update={
                            "action_intent_id": intent.id,
                            "state": self._state(intent.status),
                            "last_error": intent.last_error,
                            "updated_at": time.time(),
                        }
                    )
                )
            except Exception as exc:
                changed.append(
                    link.model_copy(
                        update={
                            "state": DecisionWorkState.PLANNED,
                            "last_error": str(exc)[:500],
                            "updated_at": time.time(),
                        }
                    )
                )

        return await self.decisions.record_work_links(
            planned.id,
            tuple(changed),
            actor=actor,
            reason=f"{payload.reason}: queued Decision work through ActionIntent",
        )

    @staticmethod
    def _successful_work_item_ref(history: dict) -> str | None:
        for receipt in reversed(history.get("receipts") or []):
            result = receipt.get("result") or {}
            if result.get("status") != "succeeded":
                continue
            output = result.get("output") or {}
            ref = str(output.get("work_item_ref") or "").strip()
            if ref:
                return ref
        return None

    def _visible_work_item(
        self,
        ref: str,
        *,
        actor: AuthenticationActor,
    ):
        state = self.work_items.state_machine._work_item_state(ref)
        if (
            state.organization_id != actor.organization_id
            or state.workspace_id != actor.workspace_id
        ):
            raise DecisionWorkError("ActionIntent result Work Item is outside Decision tenant")
        return state

    def _materialize_graph(
        self,
        decision: Decision,
        *,
        actor: AuthenticationActor,
    ) -> None:
        refs = {item.item_id: item.work_item_ref for item in decision.work_links}
        for item in decision.work_links:
            target_ref = refs[item.item_id]
            if target_ref is None:
                continue
            if item.parent_item_id is not None:
                source_ref = refs.get(item.parent_item_id)
                if source_ref:
                    self.work_graph.add_edge(
                        WorkGraphEdgeCreate(
                            relation=WorkGraphRelation.PARENT,
                            source_ref=source_ref,
                            target_ref=target_ref,
                            reason=f"Decision {decision.id}",
                        ),
                        scope=actor.tenant,
                        actor_id=actor.identity_id,
                    )
            for blocker_id in item.blocked_by_item_ids:
                source_ref = refs.get(blocker_id)
                if source_ref:
                    self.work_graph.add_edge(
                        WorkGraphEdgeCreate(
                            relation=WorkGraphRelation.BLOCKS,
                            source_ref=source_ref,
                            target_ref=target_ref,
                            reason=f"Decision {decision.id}",
                        ),
                        scope=actor.tenant,
                        actor_id=actor.identity_id,
                    )

    def _bind_goal_roots(
        self,
        decision: Decision,
        *,
        actor: AuthenticationActor,
    ) -> None:
        if decision.goal_id is None:
            return
        goal = self.goals.get(decision.goal_id, scope=actor.tenant)
        roots: dict[str, list[str]] = defaultdict(list)
        for item in decision.work_links:
            if item.parent_item_id is None and item.work_item_ref:
                roots[item.project_id].append(item.work_item_ref)

        bindings = list(goal.work_graph_bindings)
        by_project = {item.project_id: index for index, item in enumerate(bindings)}
        changed = False
        for project_id, generated in roots.items():
            index = by_project.get(project_id)
            if index is None:
                bindings.append(
                    GoalWorkGraphBinding(
                        project_id=project_id,
                        root_work_item_refs=tuple(generated),
                    )
                )
                changed = True
                continue
            existing = bindings[index]
            if not existing.root_work_item_refs:
                continue
            merged = tuple(dict.fromkeys((*existing.root_work_item_refs, *generated)))
            if merged != existing.root_work_item_refs:
                bindings[index] = GoalWorkGraphBinding(
                    project_id=project_id,
                    root_work_item_refs=merged,
                )
                changed = True

        if changed:
            self.goals.revise(
                goal.id,
                GoalUpdate(
                    work_graph_bindings=tuple(bindings),
                    reason=f"Bind approved Decision work from {decision.id}",
                ),
                scope=actor.tenant,
                actor_id=actor.identity_id,
            )

    async def reconcile(
        self,
        decision_id: str,
        *,
        actor: AuthenticationActor,
        reason: str,
    ) -> Decision:
        decision = self.decisions.get(decision_id, actor=actor)
        self.decisions._require_edit(decision, actor)
        if decision.status != DecisionStatus.APPROVED:
            raise DecisionStateError(
                "Decision work reconciliation requires an approved Decision"
            )

        changed: list[DecisionWorkLink] = []
        for link in decision.work_links:
            if link.action_intent_id is None:
                changed.append(link)
                continue
            intent = self.action_intents.get(link.action_intent_id, actor)
            state = self._state(intent.status)
            work_item_ref = link.work_item_ref
            error = intent.last_error
            if intent.status == ActionIntentStatus.SUCCEEDED:
                history = self.action_intents.history(intent.id, actor)
                work_item_ref = self._successful_work_item_ref(history)
                if not work_item_ref:
                    state = DecisionWorkState.REQUIRES_RECONCILIATION
                    error = "ActionIntent succeeded without a canonical Work Item reference"
                else:
                    try:
                        self._visible_work_item(work_item_ref, actor=actor)
                        self.work_items.state_machine.bind_provenance(
                            work_item_ref,
                            goal_id=decision.goal_id,
                            decision_id=decision.id,
                            action_intent_id=intent.id,
                            actor_id=actor.identity_id,
                        )
                        error = None
                    except Exception as exc:
                        state = DecisionWorkState.REQUIRES_RECONCILIATION
                        work_item_ref = None
                        error = str(exc)[:500]
            elif state != DecisionWorkState.SUCCEEDED:
                work_item_ref = None

            changed.append(
                link.model_copy(
                    update={
                        "state": state,
                        "work_item_ref": work_item_ref,
                        "last_error": error,
                        "updated_at": time.time(),
                    }
                )
            )

        updated = await self.decisions.record_work_links(
            decision.id,
            tuple(changed),
            actor=actor,
            reason=reason,
        )
        if updated.work_links and all(
            item.state == DecisionWorkState.SUCCEEDED and item.work_item_ref
            for item in updated.work_links
        ):
            self._materialize_graph(updated, actor=actor)
            self._bind_goal_roots(updated, actor=actor)
        return updated

    def trace(
        self,
        decision_id: str,
        *,
        actor: AuthenticationActor,
    ) -> dict:
        decision = self.decisions.get(decision_id, actor=actor)
        goal = (
            self.goals.snapshot(decision.goal_id, scope=actor.tenant)
            if decision.goal_id is not None
            else None
        )
        work_items = []
        intents = []
        for link in decision.work_links:
            if link.work_item_ref:
                state = self._visible_work_item(link.work_item_ref, actor=actor)
                work_items.append(
                    self.work_items.state_machine._work_item_state_public(state)
                )
            if link.action_intent_id:
                intents.append(
                    self.action_intents.history(link.action_intent_id, actor)
                )
        return {
            "goal": goal.model_dump(mode="json") if goal is not None else None,
            "decision": decision.model_dump(mode="json"),
            "work_items": work_items,
            "action_intents": intents,
            "post_execution_reviews": [
                item.model_dump(mode="json")
                for item in decision.post_execution_reviews
            ],
        }
