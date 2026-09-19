from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from codex_web.action_intents import ActionIntentClaimRequest, ActionIntentStatus
from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.decisions import (
    Decision,
    DecisionBudget,
    DecisionDeliberationLimits,
    DecisionFinalDecision,
    DecisionImportance,
    DecisionOption,
    DecisionParticipant,
    DecisionStatus,
    DecisionWorkCommitRequest,
    DecisionWorkItemRequest,
    DecisionWorkState,
)
from codex_web.goals import GoalBudget, GoalCreate
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    PrincipalKind,
    TenantScope,
)
from codex_web.models import Project, TaskSourceConfiguration, WorkItemState
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import (
    ActionExecutionService,
    ActionProviderRegistry,
)
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.decision_work import DecisionWorkService
from codex_web.services.decisions import DecisionService, DecisionStateError
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.goals import GoalService
from codex_web.services.identity import IdentityService
from codex_web.services.metrics import MetricService
from codex_web.services.reference_task_source import ReferenceTaskSource
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.task_source_action_provider import (
    TASK_SOURCE_ACTION_PROVIDER_INSTANCE,
    TASK_SOURCE_ACTION_PROVIDER_TYPE,
    TaskSourceActionProvider,
)
from codex_web.services.task_source_runtime import TaskSourceRegistry
from codex_web.services.work_graph import WorkGraphService
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.services.work_items import WorkItemService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.decisions import DecisionStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.goals import GoalStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.metrics import MetricStore
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
    DEFAULT_VALIDATION_OWNER = "validator"
    DEFAULT_RELEASE_OWNER = "release"
    NON_IMPLEMENTATION_OWNERS = {"validator", "release"}

    def __init__(self, root: Path, projects: tuple[Project, ...]) -> None:
        self.projects = list(projects)
        self.states: dict[str, WorkItemState] = {}
        self.DATA_DIR = root
        self.WORK_ITEM_EVENTS_FILE = root / "work-item-events.jsonl"

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

    @staticmethod
    def _leading_owner_cue_in_action(value):
        del value
        return None


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
            current_owner=(snapshot.owners[0] if snapshot.owners else None),
            current_stage="implementation_active",
            labels=list(snapshot.labels),
            last_meaningful_update_at=now,
            updated_at=now,
            created_at=now,
        )
        self.host.states[ref] = state
        return state


class DecisionWorkTraceabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(sqlite))
        identity.bootstrap_local()
        self.identity = identity
        self.actor = identity.local_trusted_actor()
        self.worker = AuthenticationActor(
            identity_id="decision-work-worker",
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

        configuration = TaskSourceConfiguration(
            source_type="reference",
            source_instance="decision-work",
            scope="project-a",
        )
        self.project = Project(
            id="project-a",
            organization_id="local",
            workspace_id="default",
            name="Project A",
            path="/tmp/project-a",
            authoritative_task_source=configuration,
        )
        self.project_service = _Projects((self.project,))
        self.host = _Host(root, (self.project,))
        self.source = ReferenceTaskSource("decision-work")
        registry = TaskSourceRegistry()
        registry.register_project(
            "reference",
            lambda configuration, project_id, scope: self.source,
        )
        self.work_items = object.__new__(WorkItemService)
        self.work_items.host = self.host
        self.work_items.task_source_registry = registry
        self.work_items.task_source_projector = _Projector(self.host)
        self.work_items.state_machine = WorkItemStateMachine(self.host)

        self.resources = ResourceCatalogService(ResourceCatalogStore(sqlite))
        definitions = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        authority = install_authority_roles(definitions, self.resources)
        self.action_providers = ActionProviderRegistry(ActionProviderStateStore(sqlite))
        self.action_providers.register(TaskSourceActionProvider(self.work_items))
        self.action_execution = ActionExecutionService(
            self.action_providers,
            self.resources,
        )
        self.action_intents = ActionIntentService(
            ActionIntentStore(sqlite),
            self.action_execution,
            authority=authority,
            identity=identity,
        )
        self.binding = self.action_providers.bind(
            ActionProviderBindingCreate(
                provider_type=TASK_SOURCE_ACTION_PROVIDER_TYPE,
                provider_instance=TASK_SOURCE_ACTION_PROVIDER_INSTANCE,
                project_id="project-a",
            ),
            actor=self.actor,
            resources=self.resources,
        )

        self.work_graph = WorkGraphService(
            WorkGraphStore(sqlite),
            self.host._load_work_item_states,
        )
        self.goals = GoalService(
            GoalStore(sqlite),
            self.project_service,
            self.work_graph,
        )
        self.goal = self.goals.create(
            GoalCreate(
                title="Improve governed delivery",
                description="Prove Goal to Decision to Work result traceability.",
                owner_identity_id=self.actor.identity_id,
                budget=GoalBudget(
                    max_input_tokens=10000,
                    max_output_tokens=2000,
                    max_model_calls=1,
                    max_cost_usd=1.0,
                ),
            ),
            scope=self.scope,
            actor_id=self.actor.identity_id,
        )

        self.decision_store = DecisionStore(sqlite)
        self.decisions = DecisionService(
            self.decision_store,
            None,
            MetricService(MetricStore(sqlite)),
            ArtifactEvidenceService(ArtifactEvidenceStore(sqlite)),
            goals=self.goals,
        )
        self.decision = Decision(
            organization_id="local",
            workspace_id="default",
            project_id="project-a",
            goal_id=self.goal.id,
            title="Choose governed rollout",
            question="Should the approved rollout create canonical work?",
            initiator_identity_id=self.actor.identity_id,
            participants=(
                DecisionParticipant(
                    id="risk",
                    role="risk",
                    perspective="Surface delivery risk.",
                ),
            ),
            options=(
                DecisionOption(
                    id="ship",
                    title="Ship",
                    description="Create approved canonical work.",
                ),
                DecisionOption(
                    id="defer",
                    title="Defer",
                    description="Do not create work yet.",
                ),
            ),
            importance=DecisionImportance.HIGH,
            budget=DecisionBudget(
                max_input_tokens=10000,
                max_output_tokens=2000,
                max_model_calls=2,
                max_cost_usd=1.0,
            ),
            limits=DecisionDeliberationLimits(
                max_participants=2,
                max_rounds=1,
            ),
            status=DecisionStatus.APPROVED,
            final_decision=DecisionFinalDecision(
                option_id="ship",
                rationale="Approved canonical consequence.",
                confidence=0.9,
                approval_request_id="approval-test",
            ),
        )
        self.decision_store.create(self.decision)
        self.service = DecisionWorkService(
            self.decisions,
            self.goals,
            self.work_items,
            self.work_graph,
            self.action_intents,
            self.action_execution,
            self.action_providers,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _payload():
        return DecisionWorkCommitRequest(
            items=(
                DecisionWorkItemRequest(
                    id="root",
                    project_id="project-a",
                    title="Implement governed rollout",
                    description="Implement the approved Decision consequence.",
                    expected_result="Implementation reaches validation.",
                    labels=("decision::generated",),
                ),
                DecisionWorkItemRequest(
                    id="validate",
                    project_id="project-a",
                    title="Validate governed rollout",
                    description="Validate the approved implementation.",
                    expected_result="Validation evidence exists.",
                    parent_item_id="root",
                    blocked_by_item_ids=("root",),
                ),
            ),
            reason="materialize approved Decision consequences",
        )

    async def _execute(self, intent_id: str):
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

    async def test_approved_decision_queues_durable_intents_before_external_work_and_reconciles_provenance(self):
        queued = await self.service.commit(
            self.decision.id,
            self._payload(),
            actor=self.actor,
        )

        self.assertEqual(len(queued.work_links), 2)
        self.assertTrue(
            all(
                item.state == DecisionWorkState.QUEUED
                and item.action_intent_id
                and item.work_item_ref is None
                for item in queued.work_links
            )
        )
        self.assertEqual(await self.source.discover(scope="project-a"), [])
        self.assertEqual(self.host.states, {})

        for link in queued.work_links:
            intent = self.action_intents.get(link.action_intent_id, self.actor)
            self.assertEqual(intent.decision_id, self.decision.id)
            self.assertEqual(intent.goal_id, self.goal.id)
            completed = await self._execute(link.action_intent_id)
            self.assertEqual(completed.status, ActionIntentStatus.SUCCEEDED)

        reconciled = await self.service.reconcile(
            self.decision.id,
            actor=self.actor,
            reason="reconcile canonical Decision work",
        )
        self.assertTrue(
            all(
                item.state == DecisionWorkState.SUCCEEDED
                and item.work_item_ref
                for item in reconciled.work_links
            )
        )

        refs = {item.item_id: item.work_item_ref for item in reconciled.work_links}
        root_state = self.host.states[refs["root"]]
        child_state = self.host.states[refs["validate"]]
        for state in (root_state, child_state):
            self.assertEqual(state.goal_id, self.goal.id)
            self.assertEqual(state.decision_id, self.decision.id)
            self.assertIsNotNone(state.originating_action_intent_id)

        graph = self.work_graph.snapshot("project-a", scope=self.scope)
        edges = {
            (edge.relation, edge.source_ref, edge.target_ref)
            for edge in graph.edges
        }
        self.assertIn(
            (WorkGraphRelation.PARENT, refs["root"], refs["validate"]),
            edges,
        )
        self.assertIn(
            (WorkGraphRelation.BLOCKS, refs["root"], refs["validate"]),
            edges,
        )

        goal = self.goals.get(self.goal.id, scope=self.scope)
        self.assertEqual(
            goal.work_graph_bindings[0].root_work_item_refs,
            (refs["root"],),
        )
        self.assertEqual(
            [item.id for item in self.goals.goals_for_work_item(
                refs["validate"],
                scope=self.scope,
            )],
            [self.goal.id],
        )

        trace = self.service.trace(self.decision.id, actor=self.actor)
        self.assertEqual(trace["goal"]["goal"]["id"], self.goal.id)
        self.assertEqual(
            {item["decision_id"] for item in trace["work_items"]},
            {self.decision.id},
        )
        self.assertEqual(len(trace["action_intents"]), 2)
        self.assertTrue(
            all(item["receipts"] for item in trace["action_intents"])
        )

    async def test_repeated_commit_reuses_same_action_intents_and_new_independent_work_can_append(self):
        first = await self.service.commit(
            self.decision.id,
            self._payload(),
            actor=self.actor,
        )
        first_ids = {
            item.item_id: item.action_intent_id
            for item in first.work_links
        }

        repeated = await self.service.commit(
            self.decision.id,
            self._payload(),
            actor=self.actor,
        )
        self.assertEqual(
            {
                item.item_id: item.action_intent_id
                for item in repeated.work_links
            },
            first_ids,
        )
        self.assertEqual(len(self.action_intents.list(self.actor)), 2)

        appended = await self.service.commit(
            self.decision.id,
            DecisionWorkCommitRequest(
                items=(
                    DecisionWorkItemRequest(
                        id="observe",
                        project_id="project-a",
                        title="Observe rollout outcome",
                        description="Track the approved rollout outcome.",
                    ),
                ),
                reason="append an independent approved consequence",
            ),
            actor=self.actor,
        )
        self.assertEqual(
            {item.item_id for item in appended.work_links},
            {"root", "validate", "observe"},
        )
        self.assertEqual(len(self.action_intents.list(self.actor)), 3)

    async def test_unapproved_decision_cannot_generate_work(self):
        draft = self.decision.model_copy(
            update={
                "id": "decision-draft",
                "status": DecisionStatus.DRAFT,
                "final_decision": None,
            }
        )
        self.decision_store.create(draft)
        with self.assertRaisesRegex(
            DecisionStateError,
            "only be generated after canonical approval",
        ):
            await self.service.commit(
                draft.id,
                self._payload(),
                actor=self.actor,
            )
        self.assertEqual(len(self.action_intents.list(self.actor)), 0)
        self.assertEqual(await self.source.discover(scope="project-a"), [])


if __name__ == "__main__":
    unittest.main()
