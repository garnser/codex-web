from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.automation_definitions import AutomationTargetKind
from codex_web.automation_runs import AutomationRunStatus
from codex_web.execution_workers import AssignmentStatus
from codex_web.failures import FailureReason, create_failure
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


class _Definitions:
    def __init__(
        self,
        *,
        failure_attention=True,
        target_kind=AutomationTargetKind.AGENT_PROFILE,
        budget=None,
        retry=None,
    ):
        self.failure_attention = failure_attention
        self.target_kind = target_kind
        self.budget = budget or SimpleNamespace(
            max_input_tokens=None,
            max_output_tokens=None,
            max_cost_usd=None,
            max_duration_seconds=None,
        )
        self.retry = retry or SimpleNamespace(
            max_attempts=1,
            backoff_seconds=0,
        )

    def resolve_reference(self, reference, **kwargs):
        return (
            SimpleNamespace(
                failure_attention=self.failure_attention,
                budget=self.budget,
                retry=self.retry,
                name="Automation A",
                owner_identity_id="automation-owner",
                target=SimpleNamespace(
                    kind=self.target_kind,
                    id=(
                        "agent-james"
                        if self.target_kind == AutomationTargetKind.AGENT_PROFILE
                        else "team-platform"
                    ),
                ),
            ),
            reference,
        )


class _Attention:
    def __init__(self):
        self.upserts = []
        self.resolutions = []

    async def upsert(self, payload, *, actor_id):
        self.upserts.append((payload, actor_id))
        return payload

    async def resolve_by_source(
        self,
        dedupe_key,
        *,
        organization_id,
        workspace_id,
        actor_id,
        reason,
    ):
        self.resolutions.append(
            (dedupe_key, organization_id, workspace_id, actor_id, reason)
        )
        return None


class _Runs:
    def __init__(
        self,
        run,
        *,
        failure_attention=True,
        budget=None,
        retry=None,
    ):
        self.store = _Store(run)
        self.completed = []
        self.definitions = _Definitions(
            failure_attention=failure_attention,
            budget=budget,
            retry=retry,
        )

    def complete(
        self,
        run_id,
        *,
        organization_id,
        workspace_id,
        succeeded,
        evidence_ids=(),
        result_code=None,
        result_reason=None,
    ):
        self.completed.append(
            (run_id, succeeded, evidence_ids, result_code, result_reason)
        )
        self.store.run = self.store.run.model_copy(
            update={
                "status": (
                    AutomationRunStatus.SUCCEEDED
                    if succeeded
                    else AutomationRunStatus.FAILED
                ),
                "evidence_ids": evidence_ids,
                "result_code": result_code,
                "result_reason": result_reason,
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


class _Scheduler:
    def __init__(self):
        self.records = []
        self.created = []

    def list(self):
        return list(self.records)

    def create(self, payload, *, actor_id):
        record = SimpleNamespace(
            id=f"schedule-{len(self.records) + 1}",
            trigger_type=payload.trigger_type,
            tenant_id=payload.tenant_id,
            workspace_id=payload.workspace_id,
            payload=dict(payload.payload),
            due_at=payload.due_at,
            actor_id=actor_id,
        )
        self.records.append(record)
        self.created.append((payload, actor_id))
        return record


class _UsageStore:
    def __init__(self, records=()):
        self.records = list(records)

    def list(self):
        return list(self.records)


class _Run(SimpleNamespace):
    def model_copy(self, *, update):
        values = dict(self.__dict__)
        values.update(update)
        return _Run(**values)


class AutomationOutcomeReconciliationTests(unittest.IsolatedAsyncioTestCase):
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
            project_id="home",
            automation_id="automation-a",
            definition_ref=SimpleNamespace(record_id="definition-1"),
            status=AutomationRunStatus.RUNNING,
            execution_ids=tuple(execution_ids),
            evidence_ids=(),
            started_at=100.0,
            result_code=None,
            result_reason=None,
            attempt=1,
            work_item_ref="group/app#42",
        )

    @staticmethod
    def _assignment(
        status,
        *evidence_ids,
        completed_at=120.0,
        failure=None,
    ):
        return SimpleNamespace(
            status=status,
            evidence_ids=tuple(evidence_ids),
            completed_at=completed_at,
            failure=failure,
            failure_code=None,
            failure_message=None,
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
                    "automation_succeeded",
                    "Canonical execution completed within configured Automation budgets.",
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

    def test_input_token_budget_exhaustion_fails_successful_execution(self) -> None:
        run = self._run("exec-a")
        budget = SimpleNamespace(
            max_input_tokens=100,
            max_output_tokens=None,
            max_cost_usd=None,
            max_duration_seconds=None,
        )
        runs = _Runs(run, budget=budget)
        usage = _UsageStore([
            SimpleNamespace(
                organization_id="local",
                workspace_id="default",
                execution_id="exec-a",
                input_tokens=125,
                output_tokens=20,
                cost_usd=0.3,
                evidence_ids=("usage-evidence",),
            )
        ])
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({"exec-a": self._assignment(AssignmentStatus.SUCCEEDED)}),
            runtime_usage=usage,
        )

        result = service.reconcile(run.id, actor=self.actor)

        self.assertEqual(result.status, AutomationRunStatus.FAILED)
        self.assertEqual(
            result.result_code,
            "automation_input_tokens_budget_exhausted",
        )
        self.assertIn("125", result.result_reason)
        self.assertIn("100", result.result_reason)
        self.assertEqual(result.evidence_ids, ("usage-evidence",))

    def test_configured_cost_budget_fails_closed_when_metric_unavailable(self) -> None:
        run = self._run("exec-a")
        budget = SimpleNamespace(
            max_input_tokens=None,
            max_output_tokens=None,
            max_cost_usd=1.0,
            max_duration_seconds=None,
        )
        runs = _Runs(run, budget=budget)
        usage = _UsageStore([
            SimpleNamespace(
                organization_id="local",
                workspace_id="default",
                execution_id="exec-a",
                input_tokens=20,
                output_tokens=5,
                cost_usd=None,
                evidence_ids=(),
            )
        ])
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({"exec-a": self._assignment(AssignmentStatus.SUCCEEDED)}),
            runtime_usage=usage,
        )

        result = service.reconcile(run.id, actor=self.actor)

        self.assertEqual(result.status, AutomationRunStatus.FAILED)
        self.assertEqual(result.result_code, "automation_budget_unverifiable")
        self.assertIn("cost_usd", result.result_reason)

    def test_missing_terminal_usage_keeps_run_running_until_telemetry_arrives(self) -> None:
        run = self._run("exec-a", "exec-b")
        budget = SimpleNamespace(
            max_input_tokens=1000,
            max_output_tokens=None,
            max_cost_usd=None,
            max_duration_seconds=None,
        )
        runs = _Runs(run, budget=budget)
        usage = _UsageStore([
            SimpleNamespace(
                organization_id="local",
                workspace_id="default",
                execution_id="exec-a",
                input_tokens=20,
                output_tokens=5,
                cost_usd=0.1,
                evidence_ids=(),
            )
        ])
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({
                "exec-a": self._assignment(AssignmentStatus.SUCCEEDED),
                "exec-b": self._assignment(AssignmentStatus.SUCCEEDED),
            }),
            runtime_usage=usage,
        )

        pending = service.reconcile(run.id, actor=self.actor)
        self.assertEqual(pending.status, AutomationRunStatus.RUNNING)
        self.assertEqual(runs.completed, [])

        usage.records.append(
            SimpleNamespace(
                organization_id="local",
                workspace_id="default",
                execution_id="exec-b",
                input_tokens=30,
                output_tokens=6,
                cost_usd=0.2,
                evidence_ids=(),
            )
        )
        completed = service.reconcile(run.id, actor=self.actor)
        self.assertEqual(completed.status, AutomationRunStatus.SUCCEEDED)

    def test_duration_budget_uses_canonical_run_and_assignment_times(self) -> None:
        run = self._run("exec-a")
        budget = SimpleNamespace(
            max_input_tokens=None,
            max_output_tokens=None,
            max_cost_usd=None,
            max_duration_seconds=10,
        )
        runs = _Runs(run, budget=budget)
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({
                "exec-a": self._assignment(
                    AssignmentStatus.SUCCEEDED,
                    completed_at=115.0,
                )
            }),
        )

        result = service.reconcile(run.id, actor=self.actor)

        self.assertEqual(result.status, AutomationRunStatus.FAILED)
        self.assertEqual(
            result.result_code,
            "automation_duration_budget_exhausted",
        )
        self.assertIn("15", result.result_reason)

    def test_transient_execution_failure_schedules_durable_retry(self) -> None:
        run = self._run("exec-a")
        retry = SimpleNamespace(max_attempts=3, backoff_seconds=30)
        runs = _Runs(run, retry=retry)
        scheduler = _Scheduler()
        transient = create_failure(
            FailureReason.PROCESS_FAILURE,
            source_subsystem="execution_worker",
        )
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({
                "exec-a": self._assignment(
                    AssignmentStatus.FAILED,
                    "failure-evidence",
                    failure=transient,
                )
            }),
            scheduler=scheduler,
            clock=lambda: 100.0,
        )

        result = service.reconcile(run.id, actor=self.actor)

        self.assertEqual(result.status, AutomationRunStatus.FAILED)
        self.assertEqual(len(scheduler.created), 1)
        payload, actor_id = scheduler.created[0]
        self.assertEqual(payload.trigger_type, "automation.retry")
        self.assertEqual(payload.due_at, 130.0)
        self.assertEqual(payload.payload["retry_of_run_id"], run.id)
        self.assertEqual(payload.payload["retry_attempt"], 2)
        self.assertEqual(payload.payload["work_item_ref"], "group/app#42")
        self.assertEqual(actor_id, self.actor.identity_id)
        self.assertIn("Retry attempt 2", result.result_reason)

    def test_mixed_success_and_failure_never_replays_automation(self) -> None:
        run = self._run("exec-a", "exec-b")
        retry = SimpleNamespace(max_attempts=3, backoff_seconds=5)
        runs = _Runs(run, retry=retry)
        scheduler = _Scheduler()
        transient = create_failure(
            FailureReason.PROCESS_FAILURE,
            source_subsystem="execution_worker",
        )
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({
                "exec-a": self._assignment(AssignmentStatus.SUCCEEDED),
                "exec-b": self._assignment(
                    AssignmentStatus.FAILED,
                    failure=transient,
                ),
            }),
            scheduler=scheduler,
            clock=lambda: 100.0,
        )

        result = service.reconcile(run.id, actor=self.actor)

        self.assertEqual(result.status, AutomationRunStatus.FAILED)
        self.assertEqual(scheduler.created, [])

    def test_non_transient_failure_is_not_automatically_retried(self) -> None:
        run = self._run("exec-a")
        retry = SimpleNamespace(max_attempts=3, backoff_seconds=5)
        runs = _Runs(run, retry=retry)
        scheduler = _Scheduler()
        blocked = create_failure(
            FailureReason.CONFIGURATION_MISSING_OR_INVALID,
            source_subsystem="execution_worker",
        )
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({
                "exec-a": self._assignment(
                    AssignmentStatus.FAILED,
                    failure=blocked,
                )
            }),
            scheduler=scheduler,
            clock=lambda: 100.0,
        )

        service.reconcile(run.id, actor=self.actor)

        self.assertEqual(scheduler.created, [])

    def test_retry_limit_prevents_another_schedule(self) -> None:
        run = self._run("exec-a")
        run.attempt = 3
        retry = SimpleNamespace(max_attempts=3, backoff_seconds=5)
        runs = _Runs(run, retry=retry)
        scheduler = _Scheduler()
        transient = create_failure(
            FailureReason.PROCESS_FAILURE,
            source_subsystem="execution_worker",
        )
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({
                "exec-a": self._assignment(
                    AssignmentStatus.FAILED,
                    failure=transient,
                )
            }),
            scheduler=scheduler,
            clock=lambda: 100.0,
        )

        service.reconcile(run.id, actor=self.actor)

        self.assertEqual(scheduler.created, [])

    async def test_failed_run_creates_deduped_attention_with_provenance(self) -> None:
        run = self._run("exec-a")
        runs = _Runs(run)
        attention = _Attention()
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({
                "exec-a": self._assignment(
                    AssignmentStatus.FAILED,
                    "failure-evidence",
                )
            }),
            attention=attention,
        )

        result = service.reconcile(run.id, actor=self.actor)
        await service.sync_attention(result, actor=self.actor)

        self.assertEqual(result.status, AutomationRunStatus.FAILED)
        self.assertEqual(len(attention.upserts), 1)
        payload, actor_id = attention.upserts[0]
        self.assertEqual(payload.type, "automation.failed")
        self.assertEqual(payload.project_id, "home")
        self.assertEqual(payload.source.object_id, run.id)
        self.assertEqual(
            payload.dedupe_key,
            "automation-run:automation-run-1:failure",
        )
        self.assertEqual(payload.owner_identity_id, "automation-owner")
        self.assertEqual(payload.requesting_agent_profile_id, "agent-james")
        self.assertEqual(payload.evidence_ids, ("failure-evidence",))
        self.assertEqual(payload.diagnostic_refs, ("execution:exec-a",))
        self.assertEqual(actor_id, self.actor.identity_id)

    async def test_failure_attention_can_be_disabled_by_definition(self) -> None:
        run = self._run("exec-a")
        runs = _Runs(run, failure_attention=False)
        attention = _Attention()
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({"exec-a": self._assignment(AssignmentStatus.FAILED)}),
            attention=attention,
        )

        result = service.reconcile(run.id, actor=self.actor)
        await service.sync_attention(result, actor=self.actor)

        self.assertEqual(result.status, AutomationRunStatus.FAILED)
        self.assertEqual(attention.upserts, [])

    async def test_success_resolves_prior_failure_attention_idempotently(self) -> None:
        run = self._run("exec-a")
        runs = _Runs(run)
        attention = _Attention()
        service = AutomationOutcomeReconciliationService(
            runs,
            _Workers({"exec-a": self._assignment(AssignmentStatus.SUCCEEDED)}),
            attention=attention,
        )

        result = service.reconcile(run.id, actor=self.actor)
        await service.sync_attention(result, actor=self.actor)

        self.assertEqual(result.status, AutomationRunStatus.SUCCEEDED)
        self.assertEqual(attention.upserts, [])
        self.assertEqual(
            attention.resolutions[0][0],
            "automation-run:automation-run-1:failure",
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
