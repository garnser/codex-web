from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.goals import build_goals_router
from codex_web.goals import (
    GoalBudget,
    GoalCreate,
    GoalCriterionKind,
    GoalCriterionOperator,
    GoalHealth,
    GoalPriority,
    GoalRisk,
    GoalRiskLevel,
    GoalStatus,
    GoalSuccessCriterion,
    GoalTransitionRequest,
    GoalUpdate,
    GoalWorkGraphBinding,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.services.goals import (
    GoalConflictError,
    GoalNotFoundError,
    GoalScopeError,
    GoalService,
)
from codex_web.services.projects import ProjectNotFoundError
from codex_web.storage.goals import GoalStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.work_graph import (
    WorkGraphCriticalPath,
    WorkGraphProgress,
    WorkGraphSnapshot,
    WorkGraphNode,
    WorkReadiness,
    WorkReadinessStatus,
)


class _Projects:
    allowed = {"project-a", "project-b"}

    def get(self, project_id, scope):
        if (
            project_id in self.allowed
            and scope.organization_id == "org-a"
            and scope.workspace_id == "ws-a"
        ):
            return SimpleNamespace(id=project_id)
        raise ProjectNotFoundError("Project not found")


def _node(ref, project, *, outcome=None, readiness=WorkReadinessStatus.RUNNABLE):
    return WorkGraphNode(
        ref=ref,
        title=ref,
        project_id=project,
        stage="closed" if outcome else "implementation",
        terminal_outcome=outcome,
        readiness=WorkReadiness(ref=ref, status=readiness),
    )


class _Graph:
    def __init__(self):
        self.nodes = {
            "project-a": (
                _node("root-a", "project-a", outcome="completed", readiness=WorkReadinessStatus.TERMINAL),
                _node("child-a", "project-a", readiness=WorkReadinessStatus.BLOCKED),
            ),
            "project-b": (
                _node("work-b", "project-b"),
            ),
        }

    def snapshot(self, project_id, *, scope):
        nodes = self.nodes.get(project_id, ())
        total = len(nodes)
        completed = sum(item.terminal_outcome == "completed" for item in nodes)
        failed = sum(item.terminal_outcome == "failed" for item in nodes)
        cancelled = sum(item.terminal_outcome == "cancelled" for item in nodes)
        blocked = sum(
            item.readiness.status == WorkReadinessStatus.BLOCKED for item in nodes
        )
        runnable = sum(
            item.readiness.status == WorkReadinessStatus.RUNNABLE for item in nodes
        )
        return WorkGraphSnapshot(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            project_id=project_id,
            nodes=nodes,
            edges=(),
            runnable_refs=tuple(
                item.ref
                for item in nodes
                if item.readiness.status == WorkReadinessStatus.RUNNABLE
            ),
            failure_impacts=(),
            critical_path=WorkGraphCriticalPath(),
            progress=WorkGraphProgress(
                total=total,
                completed=completed,
                failed=failed,
                cancelled=cancelled,
                active=total - completed - failed - cancelled,
                runnable=runnable,
                blocked=blocked,
                completion_fraction=(completed / total if total else 0.0),
            ),
        )

    @staticmethod
    def traverse(ref, *, scope, relation=None, direction="downstream"):
        del scope, relation, direction
        return ("child-a",) if ref == "root-a" else ()


class GoalServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.service = GoalService(GoalStore(sqlite), _Projects(), _Graph())
        self.scope = TenantScope(organization_id="org-a", workspace_id="ws-a")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _payload(self, **overrides):
        payload = {
            "title": "Ship autonomous release flow",
            "description": "Deliver a measurable bounded autonomous release flow.",
            "owner_identity_id": "owner-a",
            "priority": GoalPriority.HIGH,
            "target_date": time.time() + 86400,
            "success_criteria": (
                GoalSuccessCriterion(
                    description="At least 90 percent of release checks pass",
                    kind=GoalCriterionKind.METRIC,
                    metric_key="release_checks_pass_percent",
                    operator=GoalCriterionOperator.GTE,
                    target_value=90,
                    unit="percent",
                ),
            ),
            "budget": GoalBudget(
                max_input_tokens=30000,
                max_output_tokens=8000,
                max_model_calls=4,
                max_cost_usd=5.0,
                max_retries=3,
                max_handoffs=4,
            ),
            "work_graph_bindings": (
                GoalWorkGraphBinding(
                    project_id="project-a",
                    root_work_item_refs=("root-a",),
                ),
                GoalWorkGraphBinding(project_id="project-b"),
            ),
        }
        payload.update(overrides)
        return GoalCreate(**payload)

    def test_multi_project_goal_progress_health_and_machine_readable_budget(self) -> None:
        goal = self.service.create(
            self._payload(),
            scope=self.scope,
            actor_id="admin",
        )

        snapshot = self.service.snapshot(goal.id, scope=self.scope)

        self.assertEqual(snapshot.progress.project_count, 2)
        self.assertEqual(snapshot.progress.work_item_count, 3)
        self.assertEqual(snapshot.progress.completed, 1)
        self.assertEqual(snapshot.progress.blocked, 1)
        self.assertEqual(snapshot.progress.runnable, 1)
        self.assertAlmostEqual(snapshot.progress.completion_fraction, 1 / 3)
        self.assertEqual(snapshot.health.health, GoalHealth.ON_TRACK)
        self.assertEqual(goal.budget.max_model_calls, 4)
        criterion = goal.success_criteria[0]
        self.assertEqual(criterion.metric_key, "release_checks_pass_percent")
        self.assertEqual(criterion.operator, GoalCriterionOperator.GTE)
        self.assertEqual(criterion.target_value, 90)

    def test_blocked_and_at_risk_health_are_deterministic(self) -> None:
        blocked = self.service.create(
            self._payload(
                title="Blocked goal",
                work_graph_bindings=(
                    GoalWorkGraphBinding(
                        project_id="project-a",
                        root_work_item_refs=("child-a",),
                    ),
                ),
            ),
            scope=self.scope,
            actor_id="admin",
        )
        self.assertEqual(
            self.service.health(blocked.id, scope=self.scope).health,
            GoalHealth.BLOCKED,
        )

        at_risk = self.service.create(
            self._payload(
                title="Risk goal",
                risks=(
                    GoalRisk(
                        level=GoalRiskLevel.HIGH,
                        category="delivery",
                        description="Critical dependency may slip",
                    ),
                ),
            ),
            scope=self.scope,
            actor_id="admin",
        )
        health = self.service.health(at_risk.id, scope=self.scope)
        self.assertEqual(health.health, GoalHealth.AT_RISK)
        self.assertIn("high/critical", health.reasons[0])

    def test_work_item_reverse_trace_uses_canonical_goal_bindings(self) -> None:
        parent_goal = self.service.create(
            self._payload(
                title="Parent-rooted goal",
                work_graph_bindings=(
                    GoalWorkGraphBinding(
                        project_id="project-a",
                        root_work_item_refs=("root-a",),
                    ),
                ),
            ),
            scope=self.scope,
            actor_id="admin",
        )
        other_goal = self.service.create(
            self._payload(
                title="Other project goal",
                work_graph_bindings=(
                    GoalWorkGraphBinding(project_id="project-b"),
                ),
            ),
            scope=self.scope,
            actor_id="admin",
        )

        child_matches = self.service.goals_for_work_item(
            "child-a",
            scope=self.scope,
        )
        other_matches = self.service.goals_for_work_item(
            "work-b",
            scope=self.scope,
        )
        missing = self.service.goals_for_work_item(
            "not-bound",
            scope=self.scope,
        )

        self.assertEqual([item.id for item in child_matches], [parent_goal.id])
        self.assertEqual([item.id for item in other_matches], [other_goal.id])
        self.assertEqual(missing, ())

    def test_revisions_and_lifecycle_are_versioned_with_reasons(self) -> None:
        goal = self.service.create(
            self._payload(),
            scope=self.scope,
            actor_id="admin",
        )
        revised = self.service.revise(
            goal.id,
            GoalUpdate(
                title="Ship verified autonomous release flow",
                reason="clarify verification outcome",
            ),
            scope=self.scope,
            actor_id="admin",
        )
        active = self.service.transition(
            goal.id,
            GoalTransitionRequest(
                status=GoalStatus.ACTIVE,
                reason="approved for execution",
            ),
            scope=self.scope,
            actor_id="admin",
        )

        self.assertEqual(revised.revision, 2)
        self.assertEqual(active.revision, 3)
        revisions = self.service.revisions(goal.id, scope=self.scope)
        self.assertEqual([item.revision for item in revisions], [1, 2, 3])
        self.assertEqual(revisions[1].reason, "clarify verification outcome")
        self.assertEqual(revisions[2].snapshot.status, GoalStatus.ACTIVE)

        with self.assertRaises(GoalConflictError):
            self.service.transition(
                goal.id,
                GoalTransitionRequest(
                    status=GoalStatus.DRAFT,
                    reason="illegal rollback",
                ),
                scope=self.scope,
                actor_id="admin",
            )

    def test_invalid_project_root_and_cross_tenant_access_fail_closed(self) -> None:
        with self.assertRaises(GoalScopeError):
            self.service.create(
                self._payload(
                    work_graph_bindings=(
                        GoalWorkGraphBinding(project_id="missing-project"),
                    ),
                ),
                scope=self.scope,
                actor_id="admin",
            )

        with self.assertRaises(GoalScopeError):
            self.service.create(
                self._payload(
                    work_graph_bindings=(
                        GoalWorkGraphBinding(
                            project_id="project-a",
                            root_work_item_refs=("not-in-project",),
                        ),
                    ),
                ),
                scope=self.scope,
                actor_id="admin",
            )

        goal = self.service.create(
            self._payload(),
            scope=self.scope,
            actor_id="admin",
        )
        with self.assertRaises(GoalNotFoundError):
            self.service.get(
                goal.id,
                scope=TenantScope(
                    organization_id="org-b",
                    workspace_id="ws-b",
                ),
            )


class GoalApiAssuranceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.service = GoalService(GoalStore(sqlite), _Projects(), _Graph())
        self.scope = TenantScope(organization_id="org-a", workspace_id="ws-a")
        self.service.create(
            GoalCreate(
                title="Existing goal",
                description="Readable goal state",
                owner_identity_id="owner-a",
            ),
            scope=self.scope,
            actor_id="bootstrap",
        )
        self.trace_goal = self.service.create(
            GoalCreate(
                title="Traceable goal",
                description="Goal bound to a canonical Work Graph subgraph",
                owner_identity_id="owner-a",
                work_graph_bindings=(
                    GoalWorkGraphBinding(
                        project_id="project-a",
                        root_work_item_refs=("root-a",),
                    ),
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
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_goals_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def _create_body(self):
        return {
            "title": "API goal",
            "description": "Created through guarded Goal API",
            "owner_identity_id": "owner-a",
            "priority": "medium",
            "success_criteria": [
                {
                    "description": "Release checks pass",
                    "kind": "metric",
                    "metric_key": "pass_rate",
                    "operator": "gte",
                    "target_value": 90,
                }
            ],
            "budget": {"max_model_calls": 3, "max_cost_usd": 2.0},
        }

    def test_slow_graph_list_does_not_block_goal_event_requests(self) -> None:
        original_snapshot = self.service.work_graph.snapshot
        started = threading.Event()
        release = threading.Event()

        def slow_snapshot(project_id, *, scope):
            started.set()
            if not release.wait(timeout=5):
                raise AssertionError("slow graph test was not released")
            return original_snapshot(project_id, scope=scope)

        self.service.work_graph.snapshot = slow_snapshot
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                listing = pool.submit(self.client.get, "/api/goals")
                self.assertTrue(started.wait(timeout=2))
                events = pool.submit(self.client.get, "/api/goals/events")
                event_response = events.result(timeout=2)
                self.assertEqual(event_response.status_code, 200)
                release.set()
                list_response = listing.result(timeout=5)
                self.assertEqual(list_response.status_code, 200)
        finally:
            release.set()
            self.service.work_graph.snapshot = original_snapshot

    def test_work_item_reverse_trace_is_readable_without_step_up(self) -> None:
        response = self.client.get(
            "/api/goals/by-work-item",
            params={"ref": "child-a"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["work_item_ref"], "child-a")
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(
            response.json()["items"][0]["goal"]["id"],
            self.trace_goal.id,
        )

    def test_low_assurance_admin_can_read_but_not_mutate(self) -> None:
        self.assertEqual(self.client.get("/api/goals").status_code, 200)

        denied = self.client.post("/api/goals", json=self._create_body())

        self.assertEqual(denied.status_code, 403)
        self.assertIn("mfa", denied.json()["detail"].lower())

    def test_mfa_admin_can_create_revise_and_transition(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        created = self.client.post("/api/goals", json=self._create_body())
        self.assertEqual(created.status_code, 200)
        goal_id = created.json()["snapshot"]["goal"]["id"]

        revised = self.client.patch(
            f"/api/goals/{goal_id}",
            json={"title": "Revised API goal", "reason": "scope clarification"},
        )
        transitioned = self.client.post(
            f"/api/goals/{goal_id}/transition",
            json={"status": "active", "reason": "approved"},
        )
        history = self.client.get(f"/api/goals/{goal_id}/revisions")

        self.assertEqual(revised.status_code, 200)
        self.assertEqual(transitioned.status_code, 200)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()["count"], 3)

    def test_goals_admin_service_scope_supports_automation(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="goal-admin-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("goals:admin",),
        )

        response = self.client.post("/api/goals", json=self._create_body())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["snapshot"]["goal"]["created_by"],
            "goal-admin-service",
        )


if __name__ == "__main__":
    unittest.main()
