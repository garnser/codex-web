from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.reconciliation_gates import (
    ReconcilerDeclaration,
    ReconcilerStartupClass,
    ReconciliationGateState,
)
from codex_web.services.reconciliation_gates import (
    ReconciliationGateConflict,
    ReconciliationGateService,
)
from codex_web.storage.sqlite_state import SQLiteStateStore


def _actor() -> AuthenticationActor:
    return AuthenticationActor(
        identity_id="admin-a",
        principal_kind=PrincipalKind.HUMAN,
        organization_id="org-a",
        workspace_id="ws-a",
        roles=(MembershipRole.ADMIN,),
        assurance=AuthenticationAssurance.MFA,
    )


class _Identity:
    pass


class ReconciliationGateServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SQLiteStateStore(
            Path(self.temp.name) / "state.sqlite3"
        )
        self.ready = True
        self.blocker = None

        def readiness(_project_id, _actor):
            return {
                "execution_ready": self.ready,
                "checks": (
                    []
                    if self.blocker is None
                    else [
                        {
                            "id": "execution:worker",
                            "status": "blocked",
                            "code": self.blocker,
                            "message": "worker unavailable",
                        }
                    ]
                ),
            }

        self.service = ReconciliationGateService(
            self.store,
            readiness=readiness,
            identity=_Identity(),
            clock=lambda: 1_800_000_000.0,
        )
        self.service.register(
            ReconcilerDeclaration(
                service_id="slack-backfill",
                startup_class=(
                    ReconcilerStartupClass.OPERATOR_APPROVAL_REQUIRED
                ),
                readiness_required=True,
                initial_approval_required=True,
                maintenance_incompatible=True,
                readiness_check="execution:worker",
            )
        )
        self.actor = _actor()

    def test_ready_project_waits_for_operator_approval_then_becomes_eligible(self):
        waiting = self.service.decision(
            "slack-backfill",
            "project-a",
            actor=self.actor,
        )
        self.assertEqual(
            waiting.state,
            ReconciliationGateState.WAITING,
        )
        self.assertFalse(waiting.eligible)
        self.assertEqual(
            waiting.reason_code,
            "initial_operator_approval_required",
        )

        eligible = self.service.approve(
            "slack-backfill",
            "project-a",
            actor=self.actor,
            correlation_id="operator-123",
        )
        self.assertTrue(eligible.eligible)
        self.assertTrue(eligible.approved)

        restored = ReconciliationGateService(
            self.store,
            readiness=self.service.readiness,
            identity=_Identity(),
            clock=lambda: 1_800_000_000.0,
        )
        restored.register(self.service.declarations()[0])
        self.assertTrue(
            restored.decision(
                "slack-backfill",
                "project-a",
                actor=self.actor,
            ).eligible
        )

    def test_readiness_regression_blocks_previously_approved_reconciler(self):
        self.service.approve(
            "slack-backfill",
            "project-a",
            actor=self.actor,
        )
        self.ready = False
        self.blocker = "worker_capability_missing"

        decision = self.service.decision(
            "slack-backfill",
            "project-a",
            actor=self.actor,
        )

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.state, ReconciliationGateState.BLOCKED)
        self.assertEqual(
            decision.reason_code,
            "worker_capability_missing",
        )
        self.assertEqual(decision.readiness_check, "execution:worker")

    def test_pause_and_resume_are_durable(self):
        self.service.approve(
            "slack-backfill",
            "project-a",
            actor=self.actor,
        )
        paused = self.service.pause(
            "slack-backfill",
            "project-a",
            actor=self.actor,
            reason="operator maintenance",
        )
        self.assertEqual(paused.state, ReconciliationGateState.PAUSED)

        resumed = self.service.resume(
            "slack-backfill",
            "project-a",
            actor=self.actor,
        )
        self.assertTrue(resumed.eligible)

    def test_maintenance_and_active_reconciliation_exclude_each_other(self):
        self.service.approve(
            "slack-backfill",
            "project-a",
            actor=self.actor,
        )
        self.service.record_start(
            "slack-backfill",
            "project-a",
            actor=self.actor,
        )
        with self.assertRaises(ReconciliationGateConflict):
            self.service.acquire_maintenance(
                "project-a",
                actor=self.actor,
                owner="compaction-1",
                reason="legacy compaction",
            )

        self.service.record_completion(
            "slack-backfill",
            "project-a",
            actor=self.actor,
            cursor="cursor-7",
        )
        lease = self.service.acquire_maintenance(
            "project-a",
            actor=self.actor,
            owner="compaction-1",
            reason="legacy compaction",
        )
        self.assertEqual(lease.owner, "compaction-1")

        decision = self.service.decision(
            "slack-backfill",
            "project-a",
            actor=self.actor,
        )
        self.assertFalse(decision.eligible)
        self.assertTrue(decision.maintenance_blocked)

        self.service.release_maintenance(
            "project-a",
            actor=self.actor,
            owner="compaction-1",
        )
        self.assertTrue(
            self.service.decision(
                "slack-backfill",
                "project-a",
                actor=self.actor,
            ).eligible
        )

    def test_status_exposes_gate_state_audit_and_cursor_without_secrets(self):
        self.service.approve(
            "slack-backfill",
            "project-a",
            actor=self.actor,
            correlation_id="approval-7",
        )
        self.service.record_start(
            "slack-backfill",
            "project-a",
            actor=self.actor,
        )
        self.service.record_completion(
            "slack-backfill",
            "project-a",
            actor=self.actor,
            cursor="cursor-9",
        )

        status = self.service.status(
            "project-a",
            actor=self.actor,
        )

        self.assertEqual(status["items"][0]["cursor"], "cursor-9")
        self.assertEqual(status["audit"][0]["action"], "approve")
        self.assertNotIn("token", str(status).casefold())


if __name__ == "__main__":
    unittest.main()


class ReconciliationReadinessActorTests(unittest.TestCase):
    def test_runtime_probe_satisfies_real_worker_admin_check_without_elevating_caller(self):
        from types import SimpleNamespace
        from codex_web.services.reconciliation_gates import project_reconciliation_readiness
        from codex_web.services.execution_workers import ExecutionWorkerService
        from codex_web.services.identity import AuthorizationError
        actor = _actor().model_copy(update={'identity_id': 'system-bot-runtime',
            'principal_kind': PrincipalKind.SERVICE, 'service_scopes': ('secret:use',)})
        scopes = []
        def bootstrap(**kwargs):
            scopes.append(kwargs['scope'])
            return actor.model_copy(update={'identity_id': kwargs['identity_id'],
                'service_scopes': kwargs['service_scopes']})
        def evaluate(project_id, *, actor, record):
            ExecutionWorkerService._require_admin(actor)
            self.assertFalse(record)
            self.assertEqual(project_id, 'project-a')
            return SimpleNamespace(model_dump=lambda **kwargs: {'execution_ready': True})
        result = project_reconciliation_readiness('project-a', actor,
            projects=SimpleNamespace(get=lambda project_id, scope: self.assertEqual(scope, actor.tenant)),
            identity=SimpleNamespace(bootstrap_service_actor=bootstrap),
            readiness=SimpleNamespace(evaluate=evaluate))
        self.assertTrue(result['execution_ready'])
        self.assertEqual(scopes, [actor.tenant])
        with self.assertRaises(AuthorizationError):
            ExecutionWorkerService._require_admin(actor)

    def test_foreign_project_refused_before_internal_actor_is_created(self):
        from types import SimpleNamespace
        from codex_web.services.reconciliation_gates import project_reconciliation_readiness
        def denied(*args):
            raise LookupError('Project not found')
        def unexpected(**kwargs):
            self.fail('unscoped internal principal created')
        with self.assertRaises(LookupError):
            project_reconciliation_readiness('foreign', _actor(),
                projects=SimpleNamespace(get=denied),
                identity=SimpleNamespace(bootstrap_service_actor=unexpected), readiness=None)
