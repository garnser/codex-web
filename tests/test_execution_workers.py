from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.execution_workers import _operator_assignment, build_execution_workers_router
from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.execution_subjects import ExecutionSubject, ExecutionSubjectKind
from codex_web.execution_workers import (
    AssignmentClaimRequest,
    AssignmentCompleteRequest,
    AssignmentStartRequest,
    AssignmentStatus,
    ExecutionAssignmentCreate,
    ExecutionWorkerRegister,
    NetworkPolicy,
    WorkerCapability,
    WorkerHeartbeatRequest,
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

    def test_thread_subject_assignment_does_not_require_fake_work_item(self) -> None:
        assignment = self._assignment(
            work_item_ref=None,
            subject=ExecutionSubject(
                kind=ExecutionSubjectKind.THREAD,
                ref="thread-abc",
            ),
            execution_id="exec-thread",
        )

        self.assertEqual(assignment.subject.kind, ExecutionSubjectKind.THREAD)
        self.assertEqual(assignment.subject.ref, "thread-abc")
        self.assertIsNone(assignment.work_item_ref)

    def test_thread_bootstrap_subject_assignment_never_sets_work_item_ref(self) -> None:
        assignment = self._assignment(
            work_item_ref=None,
            subject=ExecutionSubject(
                kind=ExecutionSubjectKind.THREAD_BOOTSTRAP,
                ref="bootstrap-abc",
            ),
            execution_id="exec-bootstrap",
        )

        self.assertEqual(
            assignment.subject.kind,
            ExecutionSubjectKind.THREAD_BOOTSTRAP,
        )
        self.assertEqual(assignment.subject.ref, "bootstrap-abc")
        self.assertIsNone(assignment.work_item_ref)

    def test_conflicting_legacy_work_item_ref_and_subject_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicts with execution subject"):
            ExecutionAssignmentCreate(
                subject=ExecutionSubject(
                    kind=ExecutionSubjectKind.WORK_ITEM,
                    ref="group/app#99",
                ),
                work_item_ref="group/app#42",
                execution_id="exec-conflict",
                project_id="home",
                resource_ids=("repo-1",),
                execution_contract_version="1.0",
                required_capabilities=(WorkerCapability.GIT,),
            )

    def test_v1_worker_state_migrates_work_item_assignment_to_subject(self) -> None:
        assignment = self._assignment(execution_id="exec-migrate")
        raw = self.service.store.store.get(self.service.store.namespace)
        raw["schema_version"] = "1.0"
        for item in raw["assignments"]:
            if item["id"] == assignment.id:
                item.pop("subject", None)
        self.service.store.store.put(self.service.store.namespace, raw)

        state = self.service.store.load()
        migrated = next(item for item in state.assignments if item.id == assignment.id)
        self.assertEqual(state.schema_version, "1.6")
        self.assertEqual(migrated.subject.kind, ExecutionSubjectKind.WORK_ITEM)
        self.assertEqual(migrated.subject.ref, "group/app#42")
        self.assertEqual(migrated.work_item_ref, "group/app#42")

    def test_assignment_workspace_subject_mismatch_fails_closed(self) -> None:
        self.service.workspaces = SimpleNamespace(
            get=lambda workspace_id, actor: SimpleNamespace(
                id=workspace_id,
                execution_id="exec-thread-mismatch",
                subject=ExecutionSubject(
                    kind=ExecutionSubjectKind.WORK_ITEM,
                    ref="group/app#42",
                ),
                project_id="home",
                resource_ids=("repo-1",),
                base_revision="abc123",
            )
        )

        with self.assertRaisesRegex(
            WorkerConflictError,
            "execution subject does not match",
        ):
            self._assignment(
                work_item_ref=None,
                subject=ExecutionSubject(
                    kind=ExecutionSubjectKind.THREAD,
                    ref="thread-mismatch",
                ),
                execution_id="exec-thread-mismatch",
                execution_workspace_id="execws-thread",
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

    def test_worker_rejects_unsupported_execution_contract_version(self) -> None:
        assignment = self._assignment(
            execution_id="exec-contract-mismatch",
            execution_contract_version="2.0",
        )
        claimed = self.service.claim(
            self.worker.id,
            AssignmentClaimRequest(lease_seconds=30),
            actor=self.worker_actor,
            assignment_id=assignment.id,
        )
        self.assertIsNone(claimed)

        compatible = self.service.register(
            ExecutionWorkerRegister(
                service_identity_id="worker-service",
                pool="compat",
                version="2.0.0",
                capabilities=(
                    WorkerCapability.GIT,
                    WorkerCapability.COMMAND_EXECUTION,
                ),
                supported_execution_contract_versions=("2.0",),
                max_concurrency=1,
            ),
            actor=self.admin,
        )
        claimed = self.service.claim(
            compatible.id,
            AssignmentClaimRequest(lease_seconds=30),
            actor=self.worker_actor,
            assignment_id=assignment.id,
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.assigned_worker_id, compatible.id)

    def test_execution_readiness_reports_missing_capability(self) -> None:
        self.service.ensure_local_worker(
            service_identity_id="worker-service",
            version="1.0.0",
            capabilities=(
                WorkerCapability.GIT,
                WorkerCapability.ARTIFACT_UPLOAD,
            ),
            supported_execution_contract_versions=("thread-turn/1.0",),
            actor=self.admin,
        )

        readiness = self.service.execution_readiness(
            required_capabilities=(
                WorkerCapability.GIT,
                WorkerCapability.COMMAND_EXECUTION,
            ),
            execution_contract_version="thread-turn/1.0",
            actor=self.admin,
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.code, "worker_capability_missing")
        self.assertIn(
            WorkerCapability.COMMAND_EXECUTION,
            readiness.required_capabilities,
        )
        self.assertNotIn(
            WorkerCapability.COMMAND_EXECUTION,
            readiness.available_capabilities,
        )
        self.assertTrue(readiness.remediation)

    def test_execution_readiness_requires_supported_contract_version(self) -> None:
        readiness = self.service.execution_readiness(
            required_capabilities=(
                WorkerCapability.GIT,
                WorkerCapability.COMMAND_EXECUTION,
            ),
            execution_contract_version="thread-turn/1.0",
            actor=self.admin,
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(
            readiness.code,
            "execution_contract_version_unsupported",
        )

        self.service.ensure_local_worker(
            service_identity_id="worker-service",
            version="1.0.0",
            capabilities=self.worker.capabilities,
            supported_execution_contract_versions=(
                "1.0",
                "thread-turn/1.0",
            ),
            actor=self.admin,
        )
        ready = self.service.execution_readiness(
            required_capabilities=(
                WorkerCapability.GIT,
                WorkerCapability.COMMAND_EXECUTION,
            ),
            execution_contract_version="thread-turn/1.0",
            actor=self.admin,
        )
        self.assertTrue(ready.ready)
        self.assertEqual(ready.code, "ready")
        self.assertEqual(len(ready.eligible_worker_ids), 1)

    def test_claim_pins_worker_into_agent_profile_execution_provenance(self) -> None:
        profile = AgentProfileExecutionBinding(
            profile_id="coder",
            profile_revision=3,
            profile_record_id="agent-profile-rev-3",
            selected_provider_id="openai",
            selected_runtime_id="codex",
            selected_provider_revision=4,
            selected_runtime_capability_revision=7,
        )
        assignment = self._assignment(
            execution_id="exec-agent-profile",
            agent_profile=profile,
        )

        claimed = self.service.claim(
            self.worker.id,
            AssignmentClaimRequest(lease_seconds=30),
            actor=self.worker_actor,
            assignment_id=assignment.id,
        )

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.assigned_worker_id, self.worker.id)
        self.assertIsNotNone(claimed.agent_profile)
        self.assertEqual(
            claimed.agent_profile.profile_id,
            "coder",
        )
        self.assertEqual(
            claimed.agent_profile.profile_revision,
            3,
        )
        self.assertEqual(
            claimed.agent_profile.selected_provider_id,
            "openai",
        )
        self.assertEqual(
            claimed.agent_profile.selected_runtime_id,
            "codex",
        )
        self.assertEqual(
            claimed.agent_profile.selected_worker_id,
            self.worker.id,
        )

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
        lost_assignment = next(
            item
            for item in self.service.store.load().assignments
            if item.id == assignment.id
        )
        self.assertIsNotNone(lost_assignment.failure)
        self.assertEqual(
            lost_assignment.failure.reason_code.value,
            "worker_lease_lost",
        )
        self.assertTrue(
            lost_assignment.failure.automatic_retry_allowed
        )
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

    def test_non_transient_worker_failure_cannot_auto_retry(self) -> None:
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
        failed = self.service.complete(
            self.worker.id,
            assignment.id,
            AssignmentCompleteRequest(
                fence=claimed.fence,
                lease_token=claimed.lease.lease_token,
                succeeded=False,
                failure_code="resource_limit",
                failure_message="memory pressure",
            ),
            actor=self.worker_actor,
        )

        self.assertEqual(failed.status, AssignmentStatus.FAILED)
        self.assertIsNotNone(failed.failure)
        self.assertEqual(
            failed.failure.reason_code.value,
            "resource_limit",
        )
        self.assertEqual(
            failed.failure.retryability.value,
            "after_remediation",
        )
        with self.assertRaises(WorkerConflictError):
            self.service.retry_lost(
                assignment.id,
                actor=self.admin,
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


class ExecutionWorkerApiAssuranceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        identity_store = IdentityStateStore(sqlite)
        self.identity = IdentityService(identity_store)
        self.identity.bootstrap_local()

        def add_worker_identity(state):
            state.services.append(
                ServiceIdentity(id="api-worker-service", name="API Worker")
            )
            state.memberships.append(
                Membership(
                    identity_id="api-worker-service",
                    principal_kind=PrincipalKind.SERVICE,
                    organization_id="local",
                    workspace_id="default",
                    roles=[MembershipRole.MEMBER],
                )
            )
            return state

        identity_store.update(add_worker_identity)
        self.bootstrap_admin = self.identity.local_trusted_actor()
        self.service = ExecutionWorkerService(
            ExecutionWorkerStore(sqlite),
            identity=self.identity,
        )
        self.worker = self.service.register(
            ExecutionWorkerRegister(
                service_identity_id="api-worker-service",
                pool="api",
                version="1.0.0",
                capabilities=(WorkerCapability.GIT,),
            ),
            actor=self.bootstrap_admin,
        )
        self.actor = AuthenticationActor(
            identity_id="human-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_execution_workers_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    def test_low_assurance_human_can_inspect_but_cannot_mutate_control_plane(self) -> None:
        listed = self.client.get("/api/execution-workers")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["items"][0]["id"], self.worker.id)

        drained = self.client.post(
            f"/api/execution-workers/{self.worker.id}/drain",
            json={"reason": "maintenance"},
        )

        self.assertEqual(drained.status_code, 403)
        self.assertIn("mfa", drained.json()["detail"].lower())

    def test_mfa_human_can_mutate_worker_lifecycle_and_create_assignment(self) -> None:
        self.actor = self.actor.model_copy(
            update={"assurance": AuthenticationAssurance.MFA}
        )
        drained = self.client.post(
            f"/api/execution-workers/{self.worker.id}/drain",
            json={"reason": "maintenance"},
        )
        self.assertEqual(drained.status_code, 200)
        self.assertEqual(drained.json()["item"]["lifecycle"], "draining")

        assignment = self.client.post(
            "/api/execution-workers/assignments",
            json={
                "work_item_ref": "group/app#api",
                "execution_id": "exec-api",
                "project_id": "home",
                "resource_ids": ["repo-api"],
                "base_revision": "abc123",
                "execution_contract_version": "1.0",
                "required_capabilities": ["git"],
                "sandbox": "read-only",
                "approval_policy": "on-request",
                "secret_refs": ["secret-ref-api"],
            },
        )
        self.assertEqual(assignment.status_code, 200)
        self.assertEqual(assignment.json()["item"]["created_by"], "human-admin")

    def test_execution_worker_admin_service_scope_can_mutate_without_human_mfa(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="worker-admin-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="local",
            workspace_id="default",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("execution-worker:admin",),
        )

        response = self.client.post(
            f"/api/execution-workers/{self.worker.id}/quarantine",
            json={"reason": "security investigation"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["lifecycle"], "quarantined")

    def test_worker_owned_heartbeat_remains_service_identity_gated_not_mfa_gated(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="api-worker-service",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("execution-worker:run",),
        )

        response = self.client.post(
            f"/api/execution-workers/{self.worker.id}/heartbeat",
            json={"version": "1.0.1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item"]["version"], "1.0.1")


if __name__ == "__main__":
    unittest.main()
