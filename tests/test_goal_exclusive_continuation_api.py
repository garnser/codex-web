from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.goals import build_goals_router
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _GoalsStub:
    def __init__(self) -> None:
        self.goal = SimpleNamespace(
            id="goal-a",
            revision=7,
            work_graph_bindings=(
                SimpleNamespace(
                    project_id="project-a",
                    root_work_item_refs=("root-a", "root-b"),
                ),
            ),
        )

    def get(self, goal_id, *, scope):
        if goal_id != self.goal.id:
            from codex_web.services.goals import GoalNotFoundError
            raise GoalNotFoundError("goal not found")
        return self.goal


class GoalExclusiveContinuationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        store = AutonomyStateStore(
            SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        )
        self.autonomy = AutonomyController(store)
        self.goals = _GoalsStub()
        self.actor = AuthenticationActor(
            identity_id="member",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_goals_router(self.goals, self.autonomy))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def _admin(self) -> None:
        self.actor = self.actor.model_copy(
            update={
                "roles": (MembershipRole.ADMIN,),
                "assurance": AuthenticationAssurance.MFA,
            }
        )

    def test_exclusive_scope_requires_mfa_admin(self) -> None:
        response = self.client.put(
            "/api/goals/goal-a/exclusive-continuation",
            json={
                "project_id": "project-a",
                "reason": "bounded release continuation",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(
            self.autonomy.store.load().control.exclusive_goal_scope
        )

    def test_exclusive_scope_uses_canonical_goal_work_graph_roots(self) -> None:
        self._admin()

        response = self.client.put(
            "/api/goals/goal-a/exclusive-continuation",
            json={
                "project_id": "project-a",
                "reason": "bounded release continuation",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["goal_revision"], 7)
        scope = self.autonomy.store.load().control.exclusive_goal_scope
        self.assertIsNotNone(scope)
        self.assertEqual(scope.goal_id, "goal-a")
        self.assertEqual(scope.project_id, "project-a")
        self.assertEqual(scope.root_work_item_refs, ("root-a", "root-b"))
        self.assertEqual(scope.reason, "bounded release continuation")

    def test_project_outside_goal_scope_is_rejected(self) -> None:
        self._admin()

        response = self.client.put(
            "/api/goals/goal-a/exclusive-continuation",
            json={
                "project_id": "project-b",
                "reason": "must remain within canonical Goal scope",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn("outside", response.json()["detail"])
        self.assertIsNone(
            self.autonomy.store.load().control.exclusive_goal_scope
        )

    def test_clear_requires_matching_goal_and_removes_scope(self) -> None:
        self._admin()
        self.client.put(
            "/api/goals/goal-a/exclusive-continuation",
            json={
                "project_id": "project-a",
                "reason": "bounded release continuation",
            },
        )

        missing = self.client.delete(
            "/api/goals/goal-b/exclusive-continuation"
        )
        self.assertEqual(missing.status_code, 404)

        response = self.client.delete(
            "/api/goals/goal-a/exclusive-continuation"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(
            self.autonomy.store.load().control.exclusive_goal_scope
        )


if __name__ == "__main__":
    unittest.main()
