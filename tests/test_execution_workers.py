from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_web.api.execution_workers import _operator_assignment
from codex_web.execution_workers import (
    AssignmentClaimRequest,
    AssignmentCompleteRequest,
    AssignmentStartRequest,
    AssignmentStatus,
    ExecutionAssignmentCreate,
    ExecutionWorkerRegister,
    NetworkPolicy,
    WorkerCapability,
    WorkerLifecycle,
    WorkerResourceLimits,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    Membership,
    MembershipRole,
    PrincipalKind,
    ServiceIdentity,
)
from codex_web.services.execution_workers import (
    ExecutionWorkerService,
    WorkerConflictError,
    WorkerLeaseError,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class ExecutionWorkerServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.identity_store = IdentityStateStore(sqlite)
        self.identity = IdentityService(self.identity_store)
        self.identity.bootstrap_local()
        self.admin = self.identity.local_trusted_actor()

        def add_worker_identity(state):
            state.services.append(
                ServiceIdentity(id="worker-service", name="Local Worker")
            )
            state.memberships.append(
                Membership(
                    identity_id="worker-service",
                    principal_kind=PrincipalKind.SERVICE,
                    organization_id="local",
                    workspace_id="default",
                    roles=[MembershipRole.MEMBER],
                )
            )
            return state

        self.identity_store.update(add_worker_identity)
        self.worker_actor = AuthenticationActor(
            identity_id="worker-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("execution-worker:run",),
        )
        self.service = ExecutionWorkerService(
            ExecutionWorkerStore(sqlite),
            identity=self.identity,
        )
        self.worker = self.service.register(
            ExecutionWorkerRegister(
                service_identity_id="worker-service",
                pool="local",
                version="1.0.0",
                capabilities=(
                    WorkerCapability.GIT,
                    WorkerCapability.COMMAND_EXECUTION,
                    WorkerCapability.ARTIFACT_UPLOAD,
                ),
                max_concurrency=1,
            ),
            actor=self.admin,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _assignment(self, **overrides):
        payload = {
            "work_item_ref": "group/app#42",
            "execution_id": "exec-1",
            "project_id": "home",
            "resource_ids": ("repo-1",),
            "base_revision": "abc123",
            "execution_contract_version": "1.0",
            "required_capabilities": (
                WorkerCapability.GIT,
                WorkerCapability.COMMAND_EXECUTION,
            ),
            "sandbox": "workspace-write",
            "approval_policy": "on-request",
            "limits": WorkerResourceLimits(
                cpu_seconds=60,
                memory_bytes=256 * 1024 * 1024,
                disk_bytes=1024 * 1024 * 1024,
                process_count=32,
                wall_seconds=120,
            ),
            "secret_refs": ("secret-ref-1",),
            "expected_artifact_types": ("patch",),
            "expected_evidence_types": ("test_result",),
        }
        payload.update(overrides)
        return self.service.create_assignment(
            ExecutionAssignmentCreate(**payload),
            actor=self.admin,
        )

    def test_registration_requires_existing_tenant_service_identity(self) -> None:
        with self.assertRaises(WorkerConflictError):
            self.service.register(
                ExecutionWorkerRegister(
                    service_identity_id="not-real",
                    version="1.0.0",
                    capabilities=(WorkerCapability.GIT,),
                ),
                actor=self.admin,
            )

    def test_assignment_contains_bounded_references_not_secret_material(self) -> None:
        assignment = self._assignment()
        serialized = json.dumps(
            self.service.store.load().model_dump(mode="json"),
            sort_keys=True,
        )
        self.assertIn("secret-ref-1", serialized)
        self.assertNotIn("actual-secret-value", serialized)
        self.assertEqual(assignment.network.enabled, False)
        self.assertEqual(assignment.limits.process_count, 32)

    def test_worker_can_claim_start_and_complete_with_fenced_lease(self) -> None:
        assignment = self._assignment()
        claimed = self.service.claim(
            self.worker.id,
            AssignmentClaimRequest(lease_seconds=30),
            actor=self.worker_actor,
        )
        self.assertEqual(claimed.id, assignment.id)
        self.assertEqual(claimed.fence, 1)
        self.assertGreater(len(claimed.lease.lease_token), 20)

        running = self.service.start(
            self.worker.id,
            assignment.id,
            AssignmentStartRequest(
                fence=claimed.fence,
                lease_token=claimed.lease.lease_token,
            ),
            actor=self.worker_actor,
        )
        self.assertEqual(running.status, AssignmentStatus.RUNNING)

        completed = self.service.complete(
            self.worker.id,
            assignment.id,
            AssignmentCompleteRequest(
                fence=claimed.fence,
                lease_token=claimed.lease.lease_token,
                succeeded=True,
                artifact_ids=("artifact-1",),
                evidence_ids=("evidence-1",),
            ),
            actor=self.worker_actor,
        )
        self.assertEqual(completed.status, AssignmentStatus.SUCCEEDED)
        self.assertIsNone(completed.lease)
        self.assertEqual(completed.artifact_ids, ("artifact-1",))

    def test_capability_mismatch_and_network_requirement_prevent_claim(self) -> None:
        assignment = self._assignment(
            execution_id="exec-network",
            required_capabilities=(
                WorkerCapability.GIT,
                WorkerCapability.NETWORK,
            ),
            network=NetworkPolicy(
                enabled=True,
                allowed_hosts=("packages.example.com",),
            ),
        )
        claimed = self.service.claim(
            self.worker.id,
            AssignmentClaimRequest(),
            actor=self.worker_actor,
        )
        self.assertIsNone(claimed)
        current = next(
            item for item in self.service.store.load().assignments if item.id == assignment.id
        )
        self.assertEqual(current.status, AssignmentStatus.PENDING)

    def test_concurrency_limit_prevents_second_claim(self) -> None:
        first = self._assignment(execution_id="exec-a")
        second = self._assignment(execution_id="exec-b")
        claimed = self.service.claim(
            self.worker.id,
            AssignmentClaimRequest(),
            actor=self.worker_actor,
        )
        self.assertEqual(claimed.id, first.id)
        self.assertIsNone(
            self.service.claim(
                self.worker.id,
                AssignmentClaimRequest(),
                actor=self.worker_actor,
            )
        )
        current_second = next(
            item for item in self.service.store.load().assignments if item.id == second.id
        )
        self.assertEqual(current_second.status, AssignmentStatus.PENDING)

    def test_expired_lease_is_fenced_from_reassigned_completion(self) -> None:
        assignment = self._assignment()
        first = self.service.claim(
            self.worker.id,
            AssignmentClaimRequest(lease_seconds=10),
            actor=self.worker_actor,
        )
        self.service.start(
            self.worker.id,
            assignment.id,
            AssignmentStartRequest(
                fence=first.fence,
                lease_token=first.lease.lease_token,
            ),
            actor=self.worker_actor,
        )
        lost = self.service.recover_expired(
            actor=self.admin,
            now=first.lease.expires_at + 1,
        )
        self.assertEqual(lost, [assignment.id])
        self.service.retry_lost(assignment.id, actor=self.admin)
        second = self.service.claim(
            self.worker.id,
            AssignmentClaimRequest(lease_seconds=30),
            actor=self.worker_actor,
        )
        self.assertEqual(second.fence, 2)

        with self.assertRaises(WorkerLeaseError):
            self.service.complete(
                self.worker.id,
                assignment.id,
                AssignmentCompleteRequest(
                    fence=first.fence,
                    lease_token=first.lease.lease_token,
                    succeeded=True,
                ),
                actor=self.worker_actor,
            )

    def test_quarantined_worker_cannot_complete_existing_work(self) -> None:
        assignment = self._assignment()
        claimed = self.service.claim(
            self.worker.id,
            AssignmentClaimRequest(),
            actor=self.worker_actor,
        )
        self.service.start(
            self.worker.id,
            assignment.id,
            AssignmentStartRequest(
                fence=claimed.fence,
                lease_token=claimed.lease.lease_token,
            ),
            actor=self.worker_actor,
        )
        self.service.set_lifecycle(
            self.worker.id,
            WorkerLifecycle.QUARANTINED,
            actor=self.admin,
            reason="suspected compromise",
        )
        with self.assertRaises(WorkerLeaseError):
            self.service.complete(
                self.worker.id,
                assignment.id,
                AssignmentCompleteRequest(
                    fence=claimed.fence,
                    lease_token=claimed.lease.lease_token,
                    succeeded=True,
                ),
                actor=self.worker_actor,
            )

    def test_stale_worker_transitions_offline_and_cannot_claim(self) -> None:
        assignment = self._assignment()
        stale_at = self.worker.last_heartbeat_at + 121
        changed = self.service.mark_stale_workers_offline(
            actor=self.admin,
            stale_after_seconds=120,
            now=stale_at,
        )
        self.assertEqual(changed, [self.worker.id])
        self.assertIsNone(
            self.service.claim(
                self.worker.id,
                AssignmentClaimRequest(),
                actor=self.worker_actor,
            )
        )
        current = next(
            item for item in self.service.store.load().assignments
            if item.id == assignment.id
        )
        self.assertEqual(current.status, AssignmentStatus.PENDING)

    def test_operator_assignment_view_redacts_lease_bearer_token(self) -> None:
        assignment = self._assignment()
        claimed = self.service.claim(
            self.worker.id,
            AssignmentClaimRequest(),
            actor=self.worker_actor,
        )
        view = _operator_assignment(claimed)
        self.assertEqual(view["lease"]["lease_token"], "[redacted]")
        self.assertNotEqual(
            view["lease"]["lease_token"],
            claimed.lease.lease_token,
        )

    def test_wrong_service_identity_cannot_act_as_worker(self) -> None:
        assignment = self._assignment()
        impostor = self.worker_actor.model_copy(
            update={"identity_id": "another-service"}
        )
        with self.assertRaises(AuthorizationError):
            self.service.claim(
                self.worker.id,
                AssignmentClaimRequest(),
                actor=impostor,
            )

    def test_cross_tenant_actor_cannot_see_or_claim_worker(self) -> None:
        foreign = self.admin.model_copy(
            update={
                "organization_id": "other",
                "workspace_id": "other",
            }
        )
        with self.assertRaises(AuthorizationError):
            self.service.list_workers(
                self.worker_actor,
            )
        self.assertEqual(self.service.list_workers(foreign), [])


if __name__ == "__main__":
    unittest.main()
