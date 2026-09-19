from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from codex_web.action_intents import ActionIntentClaimRequest, ActionIntentStatus
from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.goal_decomposition import (
    GoalDecompositionCommitState,
    GoalDecompositionLimits,
    GoalDecompositionProposalCreate,
    GoalDecompositionReview,
    GoalDecompositionReviewDecision,
    GoalDecompositionStatus,
    GoalProposedWorkItem,
)
from codex_web.goals import GoalBudget, GoalCreate, GoalUpdate
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    PrincipalKind,
    TenantScope,
)
from codex_web.models import Project, TaskSourceConfiguration, WorkItemState
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.action_providers import (
    ActionExecutionService,
    ActionProviderRegistry,
)
from codex_web.services.goal_decomposition_commit import (
    GoalDecompositionCommitBindingError,
    GoalDecompositionCommitService,
)
from codex_web.services.goal_decompositions import (
    GoalDecompositionConflictError,
    GoalDecompositionService,
)
from codex_web.services.goals import GoalService
from codex_web.services.identity import IdentityService
from codex_web.services.reference_task_source import ReferenceTaskSource
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.task_source_action_provider import (
    TASK_SOURCE_ACTION_PROVIDER_INSTANCE,
    TASK_SOURCE_ACTION_PROVIDER_TYPE,
    TaskSourceActionProvider,
)
from codex_web.services.task_source_runtime import TaskSourceRegistry
from codex_web.services.work_graph import WorkGraphService
from codex_web.services.work_items import WorkItemService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.goal_decompositions import GoalDecompositionStore
from codex_web.storage.goals import GoalStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.work_graph import WorkGraphStore
from codex_web.work_graph import WorkGraphRelation


class _Projects:
    def __init__(self, projects: tuple[Project, ...]) -> None:
        self.projects = {item.id: item for item in projects}

    def get(self, project_id, scope):
        project = self.projects.get(project_id)
        if (
            project is None
            or project.organization_id != scope.organization_id
            or project.workspace_id != scope.workspace_id
        ):
            raise LookupError("Project not found")
        return project


class _Host:
    def __init__(self, projects: tuple[Project, ...]) -> None:
        self.projects = list(projects)
        self.states: dict[str, WorkItemState] = {}

    def _load_projects(self):
        return list(self.projects)

    def _load_work_item_states(self):
        return {
            ref: state.model_copy(deep=True)
            for ref, state in self.states.items()
        }

    def _save_work_item_states(self, states):
        self.states = {
            ref: state.model_copy(deep=True)
            for ref, state in states.items()
        }


class _Projector:
    def __init__(self, host: _Host) -> None:
        self.host = host

    def upsert(self, source, snapshot, *, project_id):
        now = time.time()
        ref = (
            f"{project_id}:{snapshot.identity.source_type}:"
            f"{snapshot.identity.external_id}"
        )
        state = WorkItemState(
            ref=ref,
            organization_id="local",
            workspace_id="default",
            project_id=project_id,
            project_path=project_id,
            source_identity=snapshot.identity,
            title=snapshot.title,
            url=snapshot.identity.external_url,
            current_owner=(
                snapshot.owners[0]
                if snapshot.owners
                else None
            ),
            current_stage="implementation_active",
            labels=list(snapshot.labels),
            last_meaningful_update_at=now,
            updated_at=now,
            created_at=now,
        )
        self.host.states[ref] = state
        return state


class _FailingCreateSource(ReferenceTaskSource):
    async def create(self, request, *, scope):
        del request, scope
        raise RuntimeError("connection dropped after create request")


class GoalDecompositionCommitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(sqlite))
        identity.bootstrap_local()
        self.identity = identity
        self.actor = identity.local_trusted_actor()
        self.worker = AuthenticationActor(
            identity_id="goal-action-worker",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="local",
            workspace_id="default",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("action-intent:worker",),
        )
        self.scope = TenantScope(
            organization_id="local",
            workspace_id="default",
        )

        configuration_a = TaskSourceConfiguration(
            source_type="reference",
            source_instance="goal-commit-a",
            scope="project-a",
        )
        configuration_b = TaskSourceConfiguration(
            source_type="reference",
            source_instance="goal-commit-b",
            scope="project-b",
        )
        self.projects = (
            Project(
                id="project-a",
                organization_id="local",
                workspace_id="default",
                name="Project A",
                path="/tmp/project-a",
                authoritative_task_source=configuration_a,
            ),
            Project(
                id="project-b",
                organization_id="local",
                workspace_id="default",
                name="Project B",
                path="/tmp/project-b",
                authoritative_task_source=configuration_b,
            ),
        )
        self.project_service = _Projects(self.projects)
        self.host = _Host(self.projects)
        self.sources = {
            "project-a": ReferenceTaskSource("goal-commit-a"),
            "project-b": ReferenceTaskSource("goal-commit-b"),
        }
        registry = TaskSourceRegistry()
        registry.register_project(
            "reference",
            lambda configuration, project_id, scope: self.sources[project_id],
        )
        work_items = object.__new__(WorkItemService)
        work_items.host = self.host
        work_items.task_source_registry = registry
        work_items.task_source_projector = _Projector(self.host)

        self.resources = ResourceCatalogService(ResourceCatalogStore(sqlite))
        self.definition_registry = DefinitionRegistryService(
            DefinitionRegistryStore(sqlite)
        )
        self.authority = install_authority_roles(
            self.definition_registry,
            self.resources,
        )
        self.action_providers = ActionProviderRegistry(
            ActionProviderStateStore(sqlite)
        )
        self.task_source_provider = TaskSourceActionProvider(work_items)
        self.action_providers.register(self.task_source_provider)
        self.action_execution = ActionExecutionService(
            self.action_providers,
            self.resources,
        )
        self.action_intents = ActionIntentService(
            ActionIntentStore(sqlite),
            self.action_execution,
            authority=self.authority,
            identity=self.identity,
        )

        self.binding_a = self._bind("project-a")
        self.binding_b = self._bind("project-b")

        self.work_graph = WorkGraphService(
            WorkGraphStore(sqlite),
            self.host._load_work_item_states,
        )
        self.goals = GoalService(
            GoalStore(sqlite),
            self.project_service,
            self.work_graph,
        )
        self.proposals = GoalDecompositionService(
            GoalDecompositionStore(sqlite),
            self.goals,
            self.project_service,
        )
        self.commit_service = GoalDecompositionCommitService(
            self.proposals,
            self.goals,
            self.work_graph,
            self.action_intents,
            self.action_execution,
            self.action_providers,
        )
        self.goal = self.goals.create(
            GoalCreate(
                title="Deliver accepted Goal work",
                description="Create reviewed work only through durable intents.",
                owner_identity_id="owner-a",
                budget=GoalBudget(
                    max_input_tokens=10000,
                    max_output_tokens=2000,
                    max_model_calls=1,
                    max_cost_usd=1.0,
                ),
            ),
            scope=self.scope,
            actor_id="bootstrap",
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _bind(self, project_id: str):
        return self.action_providers.bind(
            ActionProviderBindingCreate(
                provider_type=TASK_SOURCE_ACTION_PROVIDER_TYPE,
                provider_instance=TASK_SOURCE_ACTION_PROVIDER_INSTANCE,
                project_id=project_id,
            ),
            actor=self.actor,
            resources=self.resources,
        )

    @staticmethod
    def _items():
        return (
            GoalProposedWorkItem(
                id="root",
                project_id="project-a",
                title="Implement foundation",
                description="Build the accepted Goal foundation.",
                labels=("goal::generated",),
                expected_result="Foundation is testable.",
            ),
            GoalProposedWorkItem(
                id="child",
                project_id="project-a",
                title="Validate foundation",
                description="Validate the implementation.",
                parent_item_id="root",
                blocked_by_item_ids=("root",),
                expected_result="Validation evidence is recorded.",
            ),
            GoalProposedWorkItem(
                id="release",
                project_id="project-b",
                title="Prepare release",
                description="Prepare the second project outcome.",
            ),
        )

    def _accepted(self):
        proposal = self.proposals.create(
            self.goal.id,
            GoalDecompositionProposalCreate(
                items=self._items(),
                limits=GoalDecompositionLimits(
                    max_depth=3,
                    max_items=6,
                ),
                reason="bounded proposal",
            ),
            scope=self.scope,
            actor_id="planner",
        )
        return self.proposals.review(
            proposal.id,
            GoalDecompositionReview(
                decision=GoalDecompositionReviewDecision.ACCEPT,
                reason="approved for authoritative task creation",
            ),
            scope=self.scope,
            actor_id=self.actor.identity_id,
        )

    async def _execute_intent(self, intent_id: str):
        claimed = self.action_intents.claim(
            ActionIntentClaimRequest(
                worker_id="worker-1",
                lease_seconds=30,
            ),
            actor=self.worker,
            intent_id=intent_id,
        )
        self.assertIsNotNone(claimed)
        return await self.action_intents.execute_claimed(
            intent_id,
            "worker-1",
            actor=self.worker,
        )

    async def test_commit_queues_before_mutation_then_reconciles_full_traceability(self):
        proposal = self._accepted()

        queued = await self.commit_service.commit(
            proposal.id,
            actor=self.actor,
            reason="queue accepted Goal work",
        )

        self.assertEqual(
            queued.status,
            GoalDecompositionStatus.COMMITTING,
        )
        self.assertEqual(len(queued.commit_items), 3)
        self.assertTrue(
            all(
                item.state == GoalDecompositionCommitState.QUEUED
                and item.intent_id
                for item in queued.commit_items
            )
        )
        self.assertEqual(
            await self.sources["project-a"].discover(scope="project-a"),
            [],
        )
        self.assertEqual(
            await self.sources["project-b"].discover(scope="project-b"),
            [],
        )
        self.assertEqual(self.host.states, {})

        for record in queued.commit_items:
            completed = await self._execute_intent(record.intent_id)
            self.assertEqual(
                completed.status,
                ActionIntentStatus.SUCCEEDED,
            )
            self.assertEqual(completed.goal_id, self.goal.id)

        reconciled = self.commit_service.reconcile(
            proposal.id,
            actor=self.actor,
            reason="reconcile created authoritative work",
        )

        self.assertEqual(
            reconciled.status,
            GoalDecompositionStatus.COMMITTED,
        )
        self.assertEqual(len(reconciled.committed_work_item_refs), 3)
        self.assertTrue(
            all(
                item.state == GoalDecompositionCommitState.SUCCEEDED
                and item.work_item_ref
                for item in reconciled.commit_items
            )
        )

        refs = {
            item.proposal_item_id: item.work_item_ref
            for item in reconciled.commit_items
        }
        graph_a = self.work_graph.snapshot(
            "project-a",
            scope=self.scope,
        )
        edges = {
            (edge.relation, edge.source_ref, edge.target_ref)
            for edge in graph_a.edges
        }
        self.assertIn(
            (
                WorkGraphRelation.PARENT,
                refs["root"],
                refs["child"],
            ),
            edges,
        )
        self.assertIn(
            (
                WorkGraphRelation.BLOCKS,
                refs["root"],
                refs["child"],
            ),
            edges,
        )

        goal = self.goals.get(self.goal.id, scope=self.scope)
        bindings = {
            item.project_id: item.root_work_item_refs
            for item in goal.work_graph_bindings
        }
        self.assertEqual(bindings["project-a"], (refs["root"],))
        self.assertEqual(bindings["project-b"], (refs["release"],))
        self.assertEqual(
            [item.id for item in self.goals.goals_for_work_item(
                refs["child"],
                scope=self.scope,
            )],
            [self.goal.id],
        )

    async def test_missing_binding_fails_before_commit_state_or_external_mutation(self):
        proposal = self._accepted()

        # Disable the exact project-b binding to leave no eligible binding.
        def disable(state):
            state.bindings = [
                (
                    item.model_copy(update={"enabled": False})
                    if item.id == self.binding_b.id
                    else item
                )
                for item in state.bindings
            ]
            return state

        self.action_providers.store.update(disable)

        with self.assertRaisesRegex(
            GoalDecompositionCommitBindingError,
            "no enabled",
        ):
            await self.commit_service.commit(
                proposal.id,
                actor=self.actor,
                reason="must fail before queueing",
            )

        current = self.proposals.get(proposal.id, scope=self.scope)
        self.assertEqual(current.status, GoalDecompositionStatus.ACCEPTED)
        self.assertEqual(current.commit_items, ())
        self.assertEqual(self.action_intents.list(self.actor), [])
        self.assertEqual(
            await self.sources["project-a"].discover(scope="project-a"),
            [],
        )

    async def test_stale_goal_revision_fails_after_preflight_but_before_queueing(self):
        proposal = self._accepted()
        self.goals.revise(
            self.goal.id,
            GoalUpdate(
                title="Changed after proposal acceptance",
                reason="material Goal change",
            ),
            scope=self.scope,
            actor_id="owner",
        )

        with self.assertRaisesRegex(
            GoalDecompositionConflictError,
            "goal changed",
        ):
            await self.commit_service.commit(
                proposal.id,
                actor=self.actor,
                reason="stale commit must fail",
            )

        current = self.proposals.get(proposal.id, scope=self.scope)
        self.assertEqual(current.status, GoalDecompositionStatus.ACCEPTED)
        self.assertEqual(self.action_intents.list(self.actor), [])
        self.assertEqual(self.host.states, {})

    async def test_uncertain_non_idempotent_create_never_finalizes_or_requeues(self):
        proposal = self._accepted()
        queued = await self.commit_service.commit(
            proposal.id,
            actor=self.actor,
            reason="queue accepted Goal work",
        )
        target = next(
            item
            for item in queued.commit_items
            if item.project_id == "project-a"
        )
        self.sources["project-a"] = _FailingCreateSource("goal-commit-a")

        completed = await self._execute_intent(target.intent_id)
        self.assertEqual(
            completed.status,
            ActionIntentStatus.UNCERTAIN,
        )

        reconciled = self.commit_service.reconcile(
            proposal.id,
            actor=self.actor,
            reason="surface uncertain provider outcome",
        )
        record = next(
            item
            for item in reconciled.commit_items
            if item.proposal_item_id == target.proposal_item_id
        )
        self.assertEqual(
            record.state,
            GoalDecompositionCommitState.UNCERTAIN,
        )
        self.assertEqual(
            reconciled.status,
            GoalDecompositionStatus.COMMITTING,
        )
        self.assertEqual(reconciled.committed_work_item_refs, ())

        repeated = await self.commit_service.commit(
            proposal.id,
            actor=self.actor,
            reason="resume without unsafe replacement",
        )
        same = next(
            item
            for item in repeated.commit_items
            if item.proposal_item_id == target.proposal_item_id
        )
        self.assertEqual(same.intent_id, target.intent_id)
        self.assertEqual(
            len(self.action_intents.list(self.actor)),
            3,
        )


if __name__ == "__main__":
    unittest.main()
