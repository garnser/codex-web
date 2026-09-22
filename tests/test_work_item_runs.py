from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.agent_runtime_usage import AgentRuntimeUsage
from codex_web.artifact_evidence import (
    Artifact,
    ArtifactEvidenceState,
    ArtifactType,
    Evidence,
    EvidenceResult,
    EvidenceType,
    Verification,
    VerificationResult,
)
from codex_web.execution_workers import (
    AssignmentLease,
    AssignmentStatus,
    ExecutionAssignment,
    NetworkPolicy,
    WorkerCapability,
    WorkerEvent,
    WorkerResourceLimits,
)
from codex_web.resources import (
    RepositoryExecutionScope,
    RepositoryTargetSource,
    RepositoryWriteMode,
)
from codex_web.services.work_item_runs import WorkItemRunProjectionService
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class WorkItemRunProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.store = ExecutionWorkerStore(sqlite)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def assignment(
        execution_id: str,
        *,
        status: AssignmentStatus,
        created_at: float,
        fence: int = 0,
        lease: AssignmentLease | None = None,
        artifact_ids: tuple[str, ...] = (),
        evidence_ids: tuple[str, ...] = (),
        resource_ids: tuple[str, ...] = ("repo-1",),
        repository_scope: RepositoryExecutionScope | None = None,
    ) -> ExecutionAssignment:
        terminal = status in {
            AssignmentStatus.SUCCEEDED,
            AssignmentStatus.FAILED,
            AssignmentStatus.CANCELLED,
        }
        return ExecutionAssignment(
            id=f"assignment-{execution_id}",
            organization_id="local",
            workspace_id="default",
            work_item_ref="group/app#42",
            execution_id=execution_id,
            project_id="home",
            resource_ids=resource_ids,
            base_revision="abc123",
            execution_contract_version="1.0",
            required_capabilities=(WorkerCapability.GIT,),
            sandbox="workspace-write",
            approval_policy="on-request",
            network=NetworkPolicy(),
            limits=WorkerResourceLimits(),
            secret_refs=("secret-ref-must-not-leak",),
            repository_scope=repository_scope,
            status=status,
            fence=fence,
            lease=lease,
            assigned_worker_id=lease.worker_id if lease else None,
            created_by="tester",
            created_at=created_at,
            updated_at=created_at + 1,
            started_at=created_at + 0.25 if status != AssignmentStatus.PENDING else None,
            completed_at=created_at + 0.75 if terminal else None,
            artifact_ids=artifact_ids,
            evidence_ids=evidence_ids,
        )

    def seed(self, assignments, events=()) -> None:
        def update(state):
            state.assignments = list(assignments)
            state.events = list(events)
            return state

        self.store.update(update)

    def test_active_runs_are_pinned_and_history_is_cursor_paginated(self) -> None:
        lease = AssignmentLease(
            worker_id="worker-1",
            fence=1,
            lease_token="super-secret-bearer-token-1234567890",
            acquired_at=40.0,
            expires_at=80.0,
        )
        active = self.assignment(
            "exec-active",
            status=AssignmentStatus.RUNNING,
            created_at=40.0,
            fence=1,
            lease=lease,
        )
        history = [
            self.assignment(
                "exec-3",
                status=AssignmentStatus.SUCCEEDED,
                created_at=30.0,
            ),
            self.assignment(
                "exec-2",
                status=AssignmentStatus.FAILED,
                created_at=20.0,
            ),
            self.assignment(
                "exec-1",
                status=AssignmentStatus.SUCCEEDED,
                created_at=10.0,
            ),
        ]
        self.seed([active, *history])
        service = WorkItemRunProjectionService(self.store)

        first = service.list_runs(
            "group/app#42",
            organization_id="local",
            workspace_id="default",
            limit=1,
        )
        self.assertEqual([item["executionId"] for item in first["active"]], ["exec-active"])
        self.assertEqual([item["executionId"] for item in first["items"]], ["exec-3"])
        self.assertTrue(first["hasMore"])
        self.assertIsNotNone(first["nextCursor"])

        serialized = json.dumps(first)
        self.assertNotIn("lease_token", serialized)
        self.assertNotIn("super-secret-bearer-token", serialized)
        self.assertNotIn("secret-ref-must-not-leak", serialized)

        second = service.list_runs(
            "group/app#42",
            organization_id="local",
            workspace_id="default",
            limit=1,
            cursor=first["nextCursor"],
        )
        self.assertEqual([item["executionId"] for item in second["items"]], ["exec-2"])

    def test_coordinated_repository_scope_is_projected_on_run_summary(self) -> None:
        scope = RepositoryExecutionScope(
            organization_id="local",
            workspace_id="default",
            project_id="home",
            writable_repository_ids=("repo-app", "repo-api"),
            read_only_repository_ids=("repo-docs",),
            write_mode=RepositoryWriteMode.COORDINATED,
            source=RepositoryTargetSource.EXPLICIT,
            source_ref="turn:multi-repo",
        )
        item = self.assignment(
            "exec-coordinated",
            status=AssignmentStatus.RUNNING,
            created_at=35.0,
            resource_ids=("repo-app", "repo-api", "repo-docs"),
            repository_scope=scope,
        )
        self.seed([item])
        service = WorkItemRunProjectionService(self.store)

        payload = service.list_runs(
            "group/app#42",
            organization_id="local",
            workspace_id="default",
        )
        projected = payload["active"][0]["repositoryScope"]

        self.assertEqual(projected["writeMode"], "coordinated")
        self.assertEqual(
            projected["writableRepositoryIds"],
            ["repo-app", "repo-api"],
        )
        self.assertEqual(projected["readOnlyRepositoryIds"], ["repo-docs"])
        self.assertEqual(projected["source"], "explicit")
        self.assertEqual(projected["sourceRef"], "turn:multi-repo")

    def test_retry_attempts_are_projected_from_immutable_worker_events(self) -> None:
        item = self.assignment(
            "exec-retry",
            status=AssignmentStatus.RUNNING,
            created_at=10.0,
            fence=2,
            lease=AssignmentLease(
                worker_id="worker-b",
                fence=2,
                lease_token="another-secret-token-1234567890",
                acquired_at=20.0,
                expires_at=50.0,
            ),
        )
        events = [
            WorkerEvent(
                organization_id="local",
                workspace_id="default",
                event_type="assignment_claimed",
                worker_id="worker-a",
                assignment_id=item.id,
                details={"fence": 1, "expires_at": 15.0},
                occurred_at=11.0,
            ),
            WorkerEvent(
                organization_id="local",
                workspace_id="default",
                event_type="assignment_started",
                worker_id="worker-a",
                assignment_id=item.id,
                details={"fence": 1},
                occurred_at=12.0,
            ),
            WorkerEvent(
                organization_id="local",
                workspace_id="default",
                event_type="assignment_lost",
                worker_id="worker-a",
                assignment_id=item.id,
                details={"fence": 1},
                occurred_at=14.0,
            ),
            WorkerEvent(
                organization_id="local",
                workspace_id="default",
                event_type="assignment_retried",
                worker_id="worker-a",
                assignment_id=item.id,
                details={"fence": 1},
                occurred_at=16.0,
            ),
            WorkerEvent(
                organization_id="local",
                workspace_id="default",
                event_type="assignment_claimed",
                worker_id="worker-b",
                assignment_id=item.id,
                details={"fence": 2, "expires_at": 50.0},
                occurred_at=20.0,
            ),
        ]
        self.seed([item], events)
        service = WorkItemRunProjectionService(self.store)

        payload = service.list_runs(
            "group/app#42",
            organization_id="local",
            workspace_id="default",
        )
        lineage = payload["active"][0]["retryLineage"]

        self.assertEqual(lineage["attemptCount"], 2)
        self.assertEqual(lineage["retryCount"], 1)
        self.assertEqual(lineage["attempts"][0]["outcome"], "lost")
        self.assertEqual(lineage["attempts"][1]["workerId"], "worker-b")

    def test_detail_joins_usage_artifacts_evidence_and_verification_by_execution(self) -> None:
        repository_scope = RepositoryExecutionScope(
            organization_id="local",
            workspace_id="default",
            project_id="home",
            writable_repository_ids=("repo-1", "repo-2"),
            write_mode=RepositoryWriteMode.COORDINATED,
            source=RepositoryTargetSource.EXPLICIT,
        )
        item = self.assignment(
            "exec-detail",
            status=AssignmentStatus.SUCCEEDED,
            created_at=30.0,
            artifact_ids=("artifact-1",),
            evidence_ids=("evidence-1",),
            resource_ids=("repo-1", "repo-2"),
            repository_scope=repository_scope,
        )
        self.seed([item])
        usage = AgentRuntimeUsage(
            organization_id="local",
            workspace_id="default",
            project_id="home",
            work_item_ref="group/app#42",
            execution_id="exec-detail",
            assignment_id=item.id,
            provider_id="openai",
            runtime_id="codex",
            runtime_type="agent",
            observed_model_ids=("gpt-test",),
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            cost_usd=0.01,
            tool_call_count=3,
            file_edit_count=2,
        )
        artifact = Artifact(
            id="artifact-1",
            organization_id="local",
            workspace_id="default",
            project_id="home",
            work_item_ref="group/app#42",
            execution_id="exec-detail",
            resource_ids=("repo-2",),
            artifact_type=ArtifactType.PATCH,
            name="change.patch",
            producer_identity_id="worker",
            produced_at=31.0,
        )
        evidence = Evidence(
            id="evidence-1",
            organization_id="local",
            workspace_id="default",
            project_id="home",
            work_item_ref="group/app#42",
            execution_id="exec-detail",
            evidence_type=EvidenceType.TEST_RESULT,
            artifact_ids=("artifact-1",),
            producer_identity_id="worker",
            result=EvidenceResult.PASS,
            summary="tests passed",
            observed_at=32.0,
        )
        verification = Verification(
            id="verification-1",
            organization_id="local",
            workspace_id="default",
            work_item_ref="group/app#42",
            execution_id="exec-detail",
            artifact_ids=("artifact-1",),
            evidence_ids=("evidence-1",),
            verifier_identity_id="reviewer",
            independent=True,
            method="ci",
            result=VerificationResult.VERIFIED,
            verified_at=33.0,
        )
        service = WorkItemRunProjectionService(
            self.store,
            runtime_usage=SimpleNamespace(list=lambda: [usage]),
            artifact_evidence=SimpleNamespace(
                load=lambda: ArtifactEvidenceState(
                    artifacts=[artifact],
                    evidence=[evidence],
                    verifications=[verification],
                )
            ),
            action_intents=SimpleNamespace(
                load=lambda: SimpleNamespace(
                    intents=[
                        SimpleNamespace(
                            id="action-1",
                            organization_id="local",
                            workspace_id="default",
                            execution_id="exec-detail",
                            status="succeeded",
                            provider_type="github",
                            provider_instance="primary",
                            action_id="pull_request.create",
                            resource_ids=("repo-1",),
                            attempt=1,
                            correlation_id="corr-1",
                            causation_id=None,
                            created_at=33.5,
                            completed_at=34.0,
                            failure=None,
                        )
                    ],
                    receipts=[],
                    verifications=[],
                )
            ),
            work_item_execution=SimpleNamespace(
                run_context=lambda ref, execution_id: {
                    "id": "checkpoint-2",
                    "executionId": execution_id,
                    "deliveryProven": True,
                    "continuation": {
                        "mode": "delta",
                        "reason": "verified_checkpoint_delta",
                        "checkpointId": "checkpoint-1",
                        "fallbackToFullContextReason": None,
                        "metrics": {
                            "baselineContextBytes": 4096,
                            "deltaContextBytes": 512,
                            "estimatedTokensReused": 896,
                        },
                    },
                }
            ),
        )

        payload = service.get_run(
            "group/app#42",
            "exec-detail",
            organization_id="local",
            workspace_id="default",
        )["run"]

        self.assertEqual(payload["usage"]["totalTokens"], 150)
        self.assertEqual(payload["usage"]["fileEdits"], 2)
        self.assertEqual(payload["artifacts"][0]["id"], "artifact-1")
        self.assertEqual(payload["evidence"][0]["result"], "pass")
        self.assertEqual(payload["verifications"][0]["result"], "verified")
        self.assertEqual(payload["artifacts"][0]["resourceIds"], ["repo-2"])
        activity = {entry["kind"]: entry for entry in payload["repositoryActivity"]}
        self.assertEqual(activity["artifact"]["repositoryIds"], ["repo-2"])
        self.assertEqual(activity["evidence"]["repositoryIds"], ["repo-2"])
        self.assertEqual(activity["verification"]["repositoryIds"], ["repo-2"])
        self.assertEqual(activity["action"]["repositoryIds"], ["repo-1"])
        self.assertEqual(payload["actions"][0]["resourceIds"], ["repo-1"])
        self.assertEqual(
            [entry["occurredAt"] for entry in payload["repositoryActivity"]],
            [31.0, 32.0, 33.0, 34.0],
        )
        self.assertEqual(payload["contextCheckpoint"]["id"], "checkpoint-2")
        self.assertEqual(
            payload["contextCheckpoint"]["continuation"]["mode"],
            "delta",
        )
        self.assertEqual(
            payload["contextCheckpoint"]["continuation"]["metrics"][
                "estimatedTokensReused"
            ],
            896,
        )


class WorkItemRunUiContractTests(unittest.TestCase):
    def test_static_ui_uses_bounded_run_api_and_live_event_not_full_history_polling(self) -> None:
        root = Path(__file__).resolve().parents[1]
        ui = (root / "static/work_items_ui.js").read_text(encoding="utf-8")
        run_ui = (root / "static/work_item_runs_ui.js").read_text(encoding="utf-8")
        app = (root / "static/app.js").read_text(encoding="utf-8")

        self.assertIn("RUN_PAGE_SIZE = 20", ui)
        self.assertIn("/runs?", run_ui)
        self.assertIn("work-runs-load-more", run_ui)
        self.assertIn("repositoryScope", run_ui)
        self.assertIn("Repository execution scope", run_ui)
        self.assertIn("writable_repositories", run_ui)
        self.assertIn("repositoryActivity", run_ui)
        self.assertIn("Combined repository activity", run_ui)
        self.assertIn("work-run-repository-activity", run_ui)
        self.assertIn("codex:work-item-run-updated", ui)
        self.assertIn("work_item.run.updated", app)
        self.assertNotIn("setInterval(() => refreshRuns", ui + run_ui)


if __name__ == "__main__":
    unittest.main()
