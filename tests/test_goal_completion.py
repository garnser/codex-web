from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.goals import build_goals_router
from codex_web.goals import (
    GoalCompletionEvaluationRequest,
    GoalCreate,
    GoalCriterionKind,
    GoalCriterionObservation,
    GoalCriterionOperator,
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
from codex_web.services.goals import GoalConflictError, GoalNotFoundError, GoalService
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
    def get(self, project_id, scope):
        if (
            project_id == "project-a"
            and scope.organization_id == "org-a"
            and scope.workspace_id == "ws-a"
        ):
            return SimpleNamespace(id=project_id)
        raise ProjectNotFoundError("Project not found")


def _node(ref, *, outcome=None):
    return WorkGraphNode(
        ref=ref,
        title=ref,
        project_id="project-a",
        stage="closed" if outcome else "implementation_active",
        terminal_outcome=outcome,
        readiness=WorkReadiness(
            ref=ref,
            status=(
                WorkReadinessStatus.TERMINAL
                if outcome is not None
                else WorkReadinessStatus.RUNNABLE
            ),
        ),
    )


class _Graph:
    def __init__(self):
        self.nodes = (
            _node("work-a", outcome="completed"),
            _node("work-b"),
        )

    def snapshot(self, project_id, *, scope):
        del project_id
        total = len(self.nodes)
        completed = sum(item.terminal_outcome == "completed" for item in self.nodes)
        failed = sum(item.terminal_outcome == "failed" for item in self.nodes)
        cancelled = sum(item.terminal_outcome == "cancelled" for item in self.nodes)
        active = total - completed - failed - cancelled
        return WorkGraphSnapshot(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            project_id="project-a",
            nodes=self.nodes,
            edges=(),
            runnable_refs=tuple(
                item.ref
                for item in self.nodes
                if item.readiness.status == WorkReadinessStatus.RUNNABLE
            ),
            failure_impacts=(),
            critical_path=WorkGraphCriticalPath(),
            progress=WorkGraphProgress(
                total=total,
                completed=completed,
                failed=failed,
                cancelled=cancelled,
                active=active,
                runnable=active,
                blocked=0,
                completion_fraction=(completed / total if total else 0.0),
            ),
        )

    @staticmethod
    def traverse(ref, *, scope, relation=None, direction="downstream"):
        del ref, scope, relation, direction
        return ()


class GoalCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.graph = _Graph()
        self.service = GoalService(GoalStore(sqlite), _Projects(), self.graph)
        self.scope = TenantScope(organization_id="org-a", workspace_id="ws-a")
        self.metric = GoalSuccessCriterion(
            id="metric-pass-rate",
            description="Pass rate is at least 90",
            kind=GoalCriterionKind.METRIC,
            metric_key="pass_rate",
            operator=GoalCriterionOperator.GTE,
            target_value=90,
        )
        self.manual = GoalSuccessCriterion(
            id="manual-review",
            description="Release owner verified the result",
            kind=GoalCriterionKind.MANUAL,
        )
        self.goal = self.service.create(
            GoalCreate(
                title="Verified outcome",
                description="Completion requires work and criteria verification.",
                owner_identity_id="owner-a",
                success_criteria=(self.metric, self.manual),
                work_graph_bindings=(GoalWorkGraphBinding(project_id="project-a"),),
            ),
            scope=self.scope,
            actor_id="bootstrap",
        )
        self.goal = self.service.transition(
            self.goal.id,
            GoalTransitionRequest(status=GoalStatus.ACTIVE, reason="start work"),
            scope=self.scope,
            actor_id="admin",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _evaluation(self, *, metric=95, manual=True, reason="verify"):
        return self.service.evaluate_completion(
            self.goal.id,
            GoalCompletionEvaluationRequest(
                observations=(
                    GoalCriterionObservation(
                        criterion_id=self.metric.id,
                        source="ci:release",
                        reference="run-42",
                        observed_value=metric,
                    ),
                    GoalCriterionObservation(
                        criterion_id=self.manual.id,
                        source="approval:release-owner",
                        reference="approval-7",
                        verified=manual,
                    ),
                ),
                reason=reason,
            ),
            scope=self.scope,
            actor_id="admin",
        )

    def test_incomplete_work_and_failed_metric_block_completion(self):
        evaluation = self._evaluation(metric=85)

        self.assertFalse(evaluation.eligible)
        self.assertTrue(
            any("work-b" in blocker for blocker in evaluation.blockers)
        )
        self.assertTrue(
            any(self.metric.id in blocker for blocker in evaluation.blockers)
        )
        with self.assertRaisesRegex(GoalConflictError, "unresolved blockers"):
            self.service.transition(
                self.goal.id,
                GoalTransitionRequest(
                    status=GoalStatus.COMPLETED,
                    reason="must not complete",
                    completion_evaluation_id=evaluation.id,
                ),
                scope=self.scope,
                actor_id="admin",
            )

    def test_failed_or_cancelled_bound_work_fails_closed(self):
        self.graph.nodes = (
            _node("work-a", outcome="completed"),
            _node("work-b", outcome="failed"),
        )
        failed = self._evaluation()
        self.assertFalse(failed.eligible)
        self.assertTrue(any("failed" in blocker for blocker in failed.blockers))

        self.graph.nodes = (
            _node("work-a", outcome="completed"),
            _node("work-b", outcome="cancelled"),
        )
        cancelled = self._evaluation(reason="recheck cancelled")
        self.assertFalse(cancelled.eligible)
        self.assertTrue(any("cancelled" in blocker for blocker in cancelled.blockers))

    def test_missing_manual_verification_blocks_even_when_work_is_done(self):
        self.graph.nodes = (
            _node("work-a", outcome="completed"),
            _node("work-b", outcome="completed"),
        )
        evaluation = self.service.evaluate_completion(
            self.goal.id,
            GoalCompletionEvaluationRequest(
                observations=(
                    GoalCriterionObservation(
                        criterion_id=self.metric.id,
                        source="ci:release",
                        observed_value=95,
                    ),
                ),
                reason="manual verification missing",
            ),
            scope=self.scope,
            actor_id="admin",
        )
        self.assertFalse(evaluation.eligible)
        manual = next(
            item for item in evaluation.criteria if item.criterion_id == self.manual.id
        )
        self.assertFalse(manual.passed)
        self.assertIn("no verification", manual.findings[0])

    def test_verified_current_evaluation_allows_completion_and_is_attributed(self):
        self.graph.nodes = (
            _node("work-a", outcome="completed"),
            _node("work-b", outcome="completed"),
        )
        evaluation = self._evaluation()
        self.assertTrue(evaluation.eligible)

        completed = self.service.transition(
            self.goal.id,
            GoalTransitionRequest(
                status=GoalStatus.COMPLETED,
                reason="verified outcome achieved",
                completion_evaluation_id=evaluation.id,
            ),
            scope=self.scope,
            actor_id="admin",
        )

        self.assertEqual(completed.status, GoalStatus.COMPLETED)
        self.assertEqual(completed.completion_evaluation_id, evaluation.id)
        self.assertIsNotNone(completed.completed_at)
        revision = self.service.revisions(self.goal.id, scope=self.scope)[-1]
        self.assertEqual(
            revision.snapshot.completion_evaluation_id,
            evaluation.id,
        )

    def test_goal_revision_and_newer_evaluation_make_old_pass_unusable(self):
        self.graph.nodes = (
            _node("work-a", outcome="completed"),
            _node("work-b", outcome="completed"),
        )
        passing = self._evaluation()
        newer = self._evaluation(manual=False, reason="later verification failed")
        self.assertFalse(newer.eligible)
        with self.assertRaisesRegex(GoalConflictError, "latest current evaluation"):
            self.service.transition(
                self.goal.id,
                GoalTransitionRequest(
                    status=GoalStatus.COMPLETED,
                    reason="old pass must not win",
                    completion_evaluation_id=passing.id,
                ),
                scope=self.scope,
                actor_id="admin",
            )

        self.service.revise(
            self.goal.id,
            GoalUpdate(title="Revised outcome", reason="scope changed"),
            scope=self.scope,
            actor_id="admin",
        )
        with self.assertRaisesRegex(GoalConflictError, "stale"):
            self.service.transition(
                self.goal.id,
                GoalTransitionRequest(
                    status=GoalStatus.COMPLETED,
                    reason="stale evaluation",
                    completion_evaluation_id=newer.id,
                ),
                scope=self.scope,
                actor_id="admin",
            )

    def test_metric_eq_gte_lte_are_deterministic(self):
        goal = self.service.create(
            GoalCreate(
                title="Metrics only",
                description="No work is required when explicit metrics prove outcome.",
                owner_identity_id="owner-a",
                success_criteria=(
                    GoalSuccessCriterion(
                        id="eq",
                        description="exact",
                        kind=GoalCriterionKind.METRIC,
                        metric_key="eq",
                        operator=GoalCriterionOperator.EQ,
                        target_value=10,
                    ),
                    GoalSuccessCriterion(
                        id="gte",
                        description="minimum",
                        kind=GoalCriterionKind.METRIC,
                        metric_key="gte",
                        operator=GoalCriterionOperator.GTE,
                        target_value=10,
                    ),
                    GoalSuccessCriterion(
                        id="lte",
                        description="maximum",
                        kind=GoalCriterionKind.METRIC,
                        metric_key="lte",
                        operator=GoalCriterionOperator.LTE,
                        target_value=10,
                    ),
                ),
            ),
            scope=self.scope,
            actor_id="admin",
        )
        evaluation = self.service.evaluate_completion(
            goal.id,
            GoalCompletionEvaluationRequest(
                observations=(
                    GoalCriterionObservation(
                        criterion_id="eq", source="metrics", observed_value=10
                    ),
                    GoalCriterionObservation(
                        criterion_id="gte", source="metrics", observed_value=11
                    ),
                    GoalCriterionObservation(
                        criterion_id="lte", source="metrics", observed_value=9
                    ),
                ),
                reason="metric check",
            ),
            scope=self.scope,
            actor_id="admin",
        )
        self.assertTrue(evaluation.eligible)
        self.assertTrue(all(item.passed for item in evaluation.criteria))

    def test_cross_tenant_cannot_read_completion_evaluations(self):
        with self.assertRaises(GoalNotFoundError):
            self.service.completion_evaluations(
                self.goal.id,
                scope=TenantScope(
                    organization_id="org-b",
                    workspace_id="ws-b",
                ),
            )


class GoalCompletionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.graph = _Graph()
        self.service = GoalService(GoalStore(sqlite), _Projects(), self.graph)
        self.scope = TenantScope(organization_id="org-a", workspace_id="ws-a")
        self.goal = self.service.create(
            GoalCreate(
                title="API completion",
                description="API completion evaluation",
                owner_identity_id="owner-a",
                success_criteria=(
                    GoalSuccessCriterion(
                        id="manual",
                        description="Operator verified",
                        kind=GoalCriterionKind.MANUAL,
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

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_read_is_low_assurance_but_evaluation_requires_step_up(self):
        read = self.client.get(
            f"/api/goals/{self.goal.id}/completion-evaluation"
        )
        denied = self.client.post(
            f"/api/goals/{self.goal.id}/completion-evaluations",
            json={
                "observations": [
                    {
                        "criterion_id": "manual",
                        "source": "approval:test",
                        "verified": True,
                    }
                ],
                "reason": "verify",
            },
        )
        self.assertEqual(read.status_code, 200)
        self.assertIsNone(read.json()["item"])
        self.assertEqual(denied.status_code, 403)

        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        accepted = self.client.post(
            f"/api/goals/{self.goal.id}/completion-evaluations",
            json={
                "observations": [
                    {
                        "criterion_id": "manual",
                        "source": "approval:test",
                        "verified": True,
                    }
                ],
                "reason": "verify",
            },
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()["item"]["eligible"])


if __name__ == "__main__":
    unittest.main()
