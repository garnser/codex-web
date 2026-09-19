from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.goal_decompositions import build_goal_decompositions_router
from codex_web.goal_decomposition import (
    GoalDecompositionGenerationRequest,
    GoalDecompositionLimits,
    GoalDecompositionStatus,
)
from codex_web.goals import GoalBudget, GoalCreate, GoalUpdate
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.model_gateway import ModelGoalUsage
from codex_web.services.goal_decomposition_generation import (
    GoalDecompositionGenerationError,
    GoalDecompositionGenerationService,
)
from codex_web.services.goal_decompositions import (
    GoalDecompositionConflictError,
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
            return SimpleNamespace(
                id=project_id,
                name=f"Project {project_id[-1].upper()}",
            )
        raise ProjectNotFoundError("Project not found")


class _Graph:
    def snapshot(self, project_id, *, scope):
        del scope
        nodes = tuple(
            SimpleNamespace(
                ref=f"{project_id}-work-{index:02d}",
                project_id=project_id,
                title=f"Existing {index}",
                stage="implementation_active",
                terminal_outcome=None,
                readiness=SimpleNamespace(status="runnable"),
            )
            for index in range(40)
        )
        return SimpleNamespace(nodes=nodes)


class _Gateway:
    def __init__(self) -> None:
        self.usage = ModelGoalUsage(
            goal_id="placeholder",
            calls=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
        )
        self.requests = []
        self.response_text = json.dumps(
            {
                "items": [
                    {
                        "id": "implement",
                        "project_id": "project-a",
                        "title": "Implement bounded slice",
                        "description": "Implement only the minimum required work.",
                        "owner_identity_id": None,
                        "labels": [],
                        "parent_item_id": None,
                        "blocked_by_item_ids": [],
                        "expected_result": "Focused validation passes.",
                    }
                ]
            }
        )
        self.on_invoke = None

    def goal_usage(self, goal_id, *, actor):
        del actor
        return self.usage.model_copy(update={"goal_id": goal_id})

    async def invoke(self, request, *, actor):
        del actor
        self.requests.append(request)
        if self.on_invoke is not None:
            self.on_invoke()
        return SimpleNamespace(
            text=self.response_text,
            invocation=SimpleNamespace(id="model-invocation-plan-1"),
        )


class GoalDecompositionGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.scope = TenantScope(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        self.projects = _Projects()
        self.goals = GoalService(
            GoalStore(sqlite),
            self.projects,
            _Graph(),
        )
        self.proposals = GoalDecompositionService(
            GoalDecompositionStore(sqlite),
            self.goals,
            self.projects,
        )
        self.gateway = _Gateway()
        self.service = GoalDecompositionGenerationService(
            self.proposals,
            self.goals,
            self.projects,
            _Graph(),
            self.gateway,
        )
        self.goal = self.goals.create(
            GoalCreate(
                title="Deliver bounded planning",
                description="Generate reviewable work without side effects.",
                owner_identity_id="owner-a",
                budget=GoalBudget(
                    max_input_tokens=12000,
                    max_output_tokens=3000,
                    max_model_calls=2,
                    max_cost_usd=1.5,
                ),
            ),
            scope=self.scope,
            actor_id="bootstrap",
        )
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _request(self, **overrides):
        values = {
            "project_ids": ("project-a",),
            "limits": GoalDecompositionLimits(
                max_depth=3,
                max_items=5,
            ),
            "reason": "generate bounded preview",
        }
        values.update(overrides)
        return GoalDecompositionGenerationRequest(**values)

    async def test_generation_uses_one_goal_attributed_call_and_persists_reviewable_proposal(self) -> None:
        self.gateway.usage = ModelGoalUsage(
            goal_id=self.goal.id,
            calls=1,
            input_tokens=100,
            output_tokens=100,
            cost_usd=0.25,
        )

        proposal = await self.service.generate(
            self.goal.id,
            self._request(),
            actor=self.actor,
        )

        self.assertEqual(proposal.status, GoalDecompositionStatus.PROPOSED)
        self.assertEqual(
            proposal.model_invocation_id,
            "model-invocation-plan-1",
        )
        request = self.gateway.requests[-1]
        self.assertEqual(request.goal_id, self.goal.id)
        self.assertEqual(request.purpose, "goal-decomposition")
        self.assertFalse(request.allow_fallback)
        self.assertEqual(request.max_input_tokens, 11900)
        self.assertEqual(request.max_output_tokens, 2900)
        self.assertAlmostEqual(request.max_cost_usd, 1.25)
        self.assertIn("project-a-work-29", request.messages[0].content)
        self.assertNotIn("project-a-work-30", request.messages[0].content)

    async def test_missing_or_exhausted_goal_budget_fails_before_model_call(self) -> None:
        unbudgeted = self.goals.create(
            GoalCreate(
                title="Unbudgeted",
                description="Must not invoke a model.",
                owner_identity_id="owner-a",
            ),
            scope=self.scope,
            actor_id="bootstrap",
        )
        with self.assertRaisesRegex(
            GoalDecompositionGenerationError,
            "reasoning budget must define",
        ):
            await self.service.generate(
                unbudgeted.id,
                self._request(),
                actor=self.actor,
            )
        self.assertEqual(self.gateway.requests, [])

        self.gateway.usage = ModelGoalUsage(
            goal_id=self.goal.id,
            calls=2,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
        )
        with self.assertRaisesRegex(
            GoalDecompositionGenerationError,
            "model-call budget is exhausted",
        ):
            await self.service.generate(
                self.goal.id,
                self._request(),
                actor=self.actor,
            )
        self.assertEqual(self.gateway.requests, [])

    async def test_output_must_be_exact_json_and_stay_within_selected_projects(self) -> None:
        self.gateway.response_text = "not-json"
        with self.assertRaisesRegex(
            GoalDecompositionGenerationError,
            "exact JSON",
        ):
            await self.service.generate(
                self.goal.id,
                self._request(),
                actor=self.actor,
            )
        self.assertEqual(
            self.proposals.list(self.goal.id, scope=self.scope),
            (),
        )

        self.gateway.response_text = json.dumps(
            {
                "items": [
                    {
                        "id": "escape",
                        "project_id": "project-b",
                        "title": "Escape selected project",
                        "description": "Must be rejected.",
                        "owner_identity_id": None,
                        "labels": [],
                        "parent_item_id": None,
                        "blocked_by_item_ids": [],
                        "expected_result": None,
                    }
                ]
            }
        )
        with self.assertRaisesRegex(
            GoalDecompositionGenerationError,
            "outside allowed set",
        ):
            await self.service.generate(
                self.goal.id,
                self._request(),
                actor=self.actor,
            )

    async def test_model_cannot_invent_owner_identity(self) -> None:
        payload = json.loads(self.gateway.response_text)
        payload["items"][0]["owner_identity_id"] = "invented-admin"
        self.gateway.response_text = json.dumps(payload)

        with self.assertRaisesRegex(
            GoalDecompositionGenerationError,
            "may not assign owner identities",
        ):
            await self.service.generate(
                self.goal.id,
                self._request(),
                actor=self.actor,
            )

    async def test_goal_revision_change_during_model_call_prevents_persistence(self) -> None:
        def revise_goal():
            self.goals.revise(
                self.goal.id,
                GoalUpdate(
                    title="Changed while planning",
                    reason="concurrent outcome revision",
                ),
                scope=self.scope,
                actor_id="owner",
            )

        self.gateway.on_invoke = revise_goal

        with self.assertRaisesRegex(
            GoalDecompositionConflictError,
            "changed while decomposition proposal was being generated",
        ):
            await self.service.generate(
                self.goal.id,
                self._request(),
                actor=self.actor,
            )
        self.assertEqual(
            self.proposals.list(self.goal.id, scope=self.scope),
            (),
        )

    async def test_generation_requires_explicit_or_goal_bound_projects(self) -> None:
        with self.assertRaisesRegex(
            GoalDecompositionGenerationError,
            "requires explicit project_ids",
        ):
            await self.service.generate(
                self.goal.id,
                self._request(project_ids=()),
                actor=self.actor,
            )


class GoalDecompositionGenerationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.scope = TenantScope(
            organization_id="org-a",
            workspace_id="ws-a",
        )
        projects = _Projects()
        goals = GoalService(GoalStore(sqlite), projects, _Graph())
        self.goal = goals.create(
            GoalCreate(
                title="API planning goal",
                description="Model-assisted proposal remains reviewable.",
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
        proposals = GoalDecompositionService(
            GoalDecompositionStore(sqlite),
            goals,
            projects,
        )
        self.gateway = _Gateway()
        generation = GoalDecompositionGenerationService(
            proposals,
            goals,
            projects,
            _Graph(),
            self.gateway,
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

        app.include_router(
            build_goal_decompositions_router(
                proposals,
                generation,
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def test_generate_requires_step_up_and_mfa_generation_stops_at_proposal(self) -> None:
        body = {
            "project_ids": ["project-a"],
            "limits": {"max_depth": 2, "max_items": 4},
            "reason": "bounded API generation",
        }
        denied = self.client.post(
            f"/api/goals/{self.goal.id}/decompositions/generate",
            json=body,
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.gateway.requests, [])

        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        created = self.client.post(
            f"/api/goals/{self.goal.id}/decompositions/generate",
            json=body,
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["proposal"]["status"], "proposed")
        self.assertEqual(len(self.gateway.requests), 1)


if __name__ == "__main__":
    unittest.main()
