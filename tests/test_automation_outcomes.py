from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.automation_runs import AutomationRunStatus
from codex_web.execution_workers import AssignmentStatus
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.automation_outcomes import (
    AutomationOutcomeReconciliationService,
)
from codex_web.services.execution_workers import AssignmentNotFoundError


class _Store:
    def __init__(self, run):
        self.run = run

    def get(self, run_id, *, organization_id, workspace_id):
        assert run_id == self.run.id
        assert organization_id == self.run.organization_id
        assert workspace_id == self.run.workspace_id
        return self.run

    def list(self, *, organization_id, workspace_id, automation_id=None):
        assert organization_id == self.run.organization_id
        assert workspace_id == self.run.workspace_id
        if automation_id is not None and automation_id != self.run.automation_id:
            return []
        return [self.run]


class _Runs:
    def __init__(self, run):
        self.store = _Store(run)
        self.completed = []

    def complete(
        self,
        run_id,
        *,
        organization_id,
        workspace_id,
        succeeded,
        evidence_ids=(),
    ):
        self.completed.append((run_id, succeeded, evidence_ids))
        self.store.run = self.store.run.model_copy(
            update={
                "status": (
                    AutomationRunStatus.SUCCEEDED
                    if succeeded
                    else AutomationRunStatus.FAILED
                ),
                "evidence_ids": evidence_ids,
            }
        )
        return self.store.run


class _Workers:
    def __init__(self, assignments):
        self.assignments = assignments

    def assignment_for_execution(self, execution_id, *, actor):
        value = self.assignments.get(execution_id)
        if value is None:
            raise AssignmentNotFoundError("execution assignment not found")
        return value


class _Run(SimpleNamespace):
    def model_copy(self, *, update):
        values = dict(self.__dict__)
        values.update(update)
        return _Run(**values)


class AutomationOutcomeReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="operator",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.OWNER,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )

    @staticmethod
    def _run(*execution_ids):
        return _Run(
            id="automation-run-1",
            organization_id="local",
            workspace_id="default",
            status=AutomationRunStatus.RUNNING,
            execution_ids=tuple(execution_ids),
            evidence_ids=(),
        )

    @staticmethod
    def _assignment(status, *evidence_ids):
        return SimpleNamespace(
            status=status,
            evidence_ids=tuple(evidence_ids),
        )

    def test_reconcile_for_execution_targets_matching_running_run(self) -> None:
        run = self._run("exec-a")
        run.automation_id = "automation-a"
        runs = _Runs(run)
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({"exec-a": self._assignment(AssignmentStatus.SUCCEEDED)}),
        )

        results = service.reconcile_for_execution("exec-a", actor=self.actor)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, AutomationRunStatus.SUCCEEDED)
        self.assertEqual(runs.completed[0][0], run.id)

    def test_reconcile_for_execution_ignores_unrelated_execution(self) -> None:
        run = self._run("exec-a")
        run.automation_id = "automation-a"
        runs = _Runs(run)
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({"exec-a": self._assignment(AssignmentStatus.SUCCEEDED)}),
        )

        self.assertEqual(
            service.reconcile_for_execution("exec-other", actor=self.actor),
            (),
        )
        self.assertEqual(runs.completed, [])

    def test_all_succeeded_completes_run_and_unions_evidence(self) -> None:
        run = self._run("exec-a", "exec-b")
        runs = _Runs(run)
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers(
                {
                    "exec-a": self._assignment(
                        AssignmentStatus.SUCCEEDED,
                        "evidence-a",
                        "shared",
                    ),
                    "exec-b": self._assignment(
                        AssignmentStatus.SUCCEEDED,
                        "shared",
                        "evidence-b",
                    ),
                }
            ),
        )

        result = service.reconcile(run.id, actor=self.actor)

        self.assertEqual(result.status, AutomationRunStatus.SUCCEEDED)
        self.assertEqual(
            result.evidence_ids,
            ("evidence-a", "shared", "evidence-b"),
        )
        self.assertEqual(
            runs.completed,
            [
                (
                    "automation-run-1",
                    True,
                    ("evidence-a", "shared", "evidence-b"),
                )
            ],
        )

    def test_mixed_terminal_assignments_complete_as_failed(self) -> None:
        run = self._run("exec-a", "exec-b")
        runs = _Runs(run)
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers(
                {
                    "exec-a": self._assignment(
                        AssignmentStatus.SUCCEEDED,
                        "evidence-a",
                    ),
                    "exec-b": self._assignment(
                        AssignmentStatus.FAILED,
                        "failure-evidence",
                    ),
                }
            ),
        )

        result = service.reconcile(run.id, actor=self.actor)

        self.assertEqual(result.status, AutomationRunStatus.FAILED)
        self.assertEqual(
            result.evidence_ids,
            ("evidence-a", "failure-evidence"),
        )

    def test_active_or_not_yet_materialized_assignment_keeps_run_running(self) -> None:
        for assignments in (
            {
                "exec-a": self._assignment(AssignmentStatus.RUNNING),
                "exec-b": self._assignment(AssignmentStatus.SUCCEEDED),
            },
            {
                "exec-a": self._assignment(AssignmentStatus.SUCCEEDED),
            },
        ):
            with self.subTest(assignments=tuple(assignments)):
                run = self._run("exec-a", "exec-b")
                runs = _Runs(run)
                service = AutomationOutcomeReconciliationService(
                    runs,
                    _Workers(assignments),
                )

                result = service.reconcile(run.id, actor=self.actor)

                self.assertEqual(result.status, AutomationRunStatus.RUNNING)
                self.assertEqual(runs.completed, [])


if __name__ == "__main__":
    unittest.main()
