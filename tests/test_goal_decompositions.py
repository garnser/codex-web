from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.goal_decompositions import build_goal_decompositions_router
from codex_web.goal_decomposition import (
    GoalDecompositionLimits,
    GoalDecompositionProposalCreate,
    GoalDecompositionProposalRevise,
    GoalDecompositionReview,
    GoalDecompositionReviewDecision,
    GoalDecompositionStatus,
    GoalProposedWorkItem,
)
from codex_web.goals import GoalBudget, GoalCreate, GoalUpdate
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.services.goal_decompositions import (
    GoalDecompositionConflictError,
    GoalDecompositionScopeError,
    GoalDecompositionService,
)
from codex_web.services.goals import GoalService
from codex_web.services.projects import ProjectNotFoundError
from codex_web.storage.goal_decompositions import GoalDecompositionStore
from codex_web.storage.goals import GoalStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Projects:
    def get(self, project_id, scope):
        if (
            project_id in {"project-a", "project-b"}
            and scope.organization_id == "org-a"
            and scope.workspace_id == "ws-a"
        ):
            return SimpleNamespace(id=project_id)
        raise ProjectNotFoundError("Project not found")


class _Graph:
    pass


class GoalDecompositionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.goals = GoalService(GoalStore(sqlite), _Projects(), _Graph())
        self.service = GoalDecompositionService(
            GoalDecompositionStore(sqlite),
            self.goals,
            _Projects(),
        )
        self.scope = TenantScope(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.goal = self.goals.create(
            GoalCreate(
                title="Ship bounded Goal decomposition",
                description="Propose work without silently creating it.",
                owner_identity_id="owner-a",
                budget=GoalBudget(
                    max_input_tokens=12000,
                    max_output_tokens=3000,
                    max_model_calls=2,
                    max_cost_usd=1.5,
                    max_retries=1,
                    max_handoffs=2,
                ),
            ),
            scope=self.scope,
            actor_id="bootstrap",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _items():
        return (
            GoalProposedWorkItem(
                id="root",
                project_id="project-a",
                title="Implement foundation",
                description="Create the bounded deterministic foundation.",
                expected_result="Foundation tests pass.",
            ),
            GoalProposedWorkItem(
                id="child",
                project_id="project-a",
                title="Validate foundation",
                description="Validate the proposed implementation.",
                parent_item_id="root",
                blocked_by_item_ids=("root",),
                expected_result="Validation evidence is available.",
            ),
            GoalProposedWorkItem(
                id="release",
                project_id="project-b",
                title="Release outcome",
                description="Prepare the verified result for release.",
            ),
        )

    def _create(self, *, items=None, limits=None):
        return self.service.create(
            self.goal.id,
            GoalDecompositionProposalCreate(
                items=items or self._items(),
                limits=limits or GoalDecompositionLimits(
                    max_depth=3,
                    max_items=5,
                ),
                reason="initial bounded proposal",
            ),
            scope=self.scope,
            actor_id="planner",
        )

    def test_proposal_is_bounded_versioned_and_copies_goal_reasoning_budget(self) -> None:
        proposal = self._create()

        self.assertEqual(proposal.status, GoalDecompositionStatus.PROPOSED)
        self.assertEqual(proposal.goal_revision, self.goal.revision)
        self.assertEqual(proposal.reasoning_budget, self.goal.budget)
        self.assertEqual(len(proposal.items), 3)

        revisions = self.service.revisions(
            proposal.id,
            scope=self.scope,
        )
        events = self.service.events(
            self.goal.id,
            scope=self.scope,
        )
        self.assertEqual([item.revision for item in revisions], [1])
        self.assertEqual(events[0].event_type, "goal_decomposition.proposed")

    def test_item_count_depth_and_graph_cycles_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            GoalDecompositionConflictError,
            "max_items",
        ):
            self._create(
                limits=GoalDecompositionLimits(max_depth=3, max_items=2)
            )

        deep = (
            GoalProposedWorkItem(
                id="a",
                project_id="project-a",
                title="A",
                description="A",
                parent_item_id="b",
            ),
            GoalProposedWorkItem(
                id="b",
                project_id="project-a",
                title="B",
                description="B",
                parent_item_id="a",
            ),
        )
        with self.assertRaisesRegex(
            GoalDecompositionConflictError,
            "parent relationships contain a cycle",
        ):
            self._create(items=deep)

        blocking = (
            GoalProposedWorkItem(
                id="a",
                project_id="project-a",
                title="A",
                description="A",
                blocked_by_item_ids=("b",),
            ),
            GoalProposedWorkItem(
                id="b",
                project_id="project-a",
                title="B",
                description="B",
                blocked_by_item_ids=("a",),
            ),
        )
        with self.assertRaisesRegex(
            GoalDecompositionConflictError,
            "blocking relationships contain a cycle",
        ):
            self._create(items=blocking)

        over_depth = (
            GoalProposedWorkItem(
                id="a",
                project_id="project-a",
                title="A",
                description="A",
            ),
            GoalProposedWorkItem(
                id="b",
                project_id="project-a",
                title="B",
                description="B",
                parent_item_id="a",
            ),
        )
        with self.assertRaisesRegex(
            GoalDecompositionConflictError,
            "exceeds max_depth",
        ):
            self._create(
                items=over_depth,
                limits=GoalDecompositionLimits(max_depth=1, max_items=5),
            )

    def test_cross_project_parent_and_blocking_edges_fail_closed(self) -> None:
        cross_parent = (
            GoalProposedWorkItem(
                id="parent",
                project_id="project-a",
                title="Parent",
                description="Parent work.",
            ),
            GoalProposedWorkItem(
                id="child",
                project_id="project-b",
                title="Child",
                description="Child work.",
                parent_item_id="parent",
            ),
        )
        with self.assertRaisesRegex(
            GoalDecompositionConflictError,
            "parent relationships must stay within one project",
        ):
            self._create(items=cross_parent)

        cross_blocker = (
            GoalProposedWorkItem(
                id="blocker",
                project_id="project-a",
                title="Blocker",
                description="Blocking work.",
            ),
            GoalProposedWorkItem(
                id="blocked",
                project_id="project-b",
                title="Blocked",
                description="Blocked work.",
                blocked_by_item_ids=("blocker",),
            ),
        )
        with self.assertRaisesRegex(
            GoalDecompositionConflictError,
            "blocking relationships must stay within one project",
        ):
            self._create(items=cross_blocker)

    def test_unknown_project_and_missing_references_fail_closed(self) -> None:
        with self.assertRaises(GoalDecompositionScopeError):
            self._create(
                items=(
                    GoalProposedWorkItem(
                        id="missing",
                        project_id="project-x",
                        title="Missing project",
                        description="Must fail.",
                    ),
                )
            )

        with self.assertRaisesRegex(
            GoalDecompositionConflictError,
            "parent item not found",
        ):
            self._create(
                items=(
                    GoalProposedWorkItem(
                        id="child",
                        project_id="project-a",
                        title="Child",
                        description="Must fail.",
                        parent_item_id="missing",
                    ),
                )
            )

    def test_accept_reject_and_revise_are_revisioned_and_stale_goal_is_not_accepted(self) -> None:
        proposal = self._create()
        revised_goal = self.goals.revise(
            self.goal.id,
            GoalUpdate(
                title="Ship revised bounded Goal decomposition",
                reason="outcome changed",
            ),
            scope=self.scope,
            actor_id="owner",
        )
        self.assertGreater(revised_goal.revision, proposal.goal_revision)

        with self.assertRaisesRegex(
            GoalDecompositionConflictError,
            "goal changed",
        ):
            self.service.review(
                proposal.id,
                GoalDecompositionReview(
                    decision=GoalDecompositionReviewDecision.ACCEPT,
                    reason="approve stale proposal",
                ),
                scope=self.scope,
                actor_id="reviewer",
            )

        revised = self.service.revise(
            proposal.id,
            GoalDecompositionProposalRevise(
                items=self._items(),
                reason="refresh against revised goal",
            ),
            scope=self.scope,
            actor_id="planner",
        )
        self.assertEqual(revised.goal_revision, revised_goal.revision)
        self.assertEqual(revised.status, GoalDecompositionStatus.PROPOSED)

        accepted = self.service.review(
            proposal.id,
            GoalDecompositionReview(
                decision=GoalDecompositionReviewDecision.ACCEPT,
                reason="approved bounded plan",
            ),
            scope=self.scope,
            actor_id="reviewer",
        )
        self.assertEqual(accepted.status, GoalDecompositionStatus.ACCEPTED)
        self.assertEqual(accepted.reviewed_by, "reviewer")
        self.assertEqual(
            [item.revision for item in self.service.revisions(
                proposal.id,
                scope=self.scope,
            )],
            [1, 2, 3],
        )

        with self.assertRaisesRegex(
            GoalDecompositionConflictError,
            "cannot revise",
        ):
            self.service.revise(
                proposal.id,
                GoalDecompositionProposalRevise(
                    items=self._items(),
                    reason="accepted plans need a new proposal",
                ),
                scope=self.scope,
                actor_id="planner",
            )

    def test_rejected_proposal_can_be_revised_but_not_silently_accepted(self) -> None:
        proposal = self._create()
        rejected = self.service.review(
            proposal.id,
            GoalDecompositionReview(
                decision=GoalDecompositionReviewDecision.REJECT,
                reason="split validation work more clearly",
            ),
            scope=self.scope,
            actor_id="reviewer",
        )
        self.assertEqual(rejected.status, GoalDecompositionStatus.REJECTED)

        revised = self.service.revise(
            proposal.id,
            GoalDecompositionProposalRevise(
                items=self._items(),
                reason="address review feedback",
            ),
            scope=self.scope,
            actor_id="planner",
        )
        self.assertEqual(revised.status, GoalDecompositionStatus.PROPOSED)
        self.assertIsNone(revised.reviewed_by)


class GoalDecompositionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.scope = TenantScope(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        goals = GoalService(GoalStore(sqlite), _Projects(), _Graph())
        self.goal = goals.create(
            GoalCreate(
                title="API decomposition goal",
                description="Review proposed work before creation.",
                owner_identity_id="owner-a",
                budget=GoalBudget(max_model_calls=1, max_cost_usd=1.0),
            ),
            scope=self.scope,
            actor_id="bootstrap",
        )
        self.service = GoalDecompositionService(
            GoalDecompositionStore(sqlite),
            goals,
            _Projects(),
        )
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_goal_decompositions_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def _body(self):
        return {
            "items": [
                {
                    "id": "api-root",
                    "project_id": "project-a",
                    "title": "Implement API proposal",
                    "description": "No Work Item is created by this request.",
                }
            ],
            "limits": {"max_depth": 2, "max_items": 4},
            "reason": "bounded preview",
        }

    def test_low_assurance_can_read_but_not_create_or_review(self) -> None:
        self.assertEqual(
            self.client.get(
                f"/api/goals/{self.goal.id}/decompositions"
            ).status_code,
            200,
        )
        denied = self.client.post(
            f"/api/goals/{self.goal.id}/decompositions",
            json=self._body(),
        )
        self.assertEqual(denied.status_code, 403)
        self.assertIn("mfa", denied.json()["detail"].lower())

    def test_mfa_admin_can_create_review_and_inspect_revision_history(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        created = self.client.post(
            f"/api/goals/{self.goal.id}/decompositions",
            json=self._body(),
        )
        self.assertEqual(created.status_code, 200)
        proposal_id = created.json()["proposal"]["id"]

        accepted = self.client.post(
            f"/api/goals/{self.goal.id}/decompositions/{proposal_id}/review",
            json={
                "decision": "accept",
                "reason": "approved for later commit",
            },
        )
        history = self.client.get(
            f"/api/goals/{self.goal.id}/decompositions/{proposal_id}/revisions"
        )
        events = self.client.get(
            f"/api/goals/{self.goal.id}/decompositions/events"
        )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(
            accepted.json()["proposal"]["status"],
            "accepted",
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["count"], 2)
        self.assertEqual(events.status_code, 200)
        self.assertEqual(events.json()["count"], 2)

    def test_goals_admin_service_scope_can_manage_proposals(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="goal-planner-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("goals:admin",),
        )
        response = self.client.post(
            f"/api/goals/{self.goal.id}/decompositions",
            json=self._body(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["proposal"]["created_by"],
            "goal-planner-service",
        )


if __name__ == "__main__":
    unittest.main()
