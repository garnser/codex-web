from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.execution_workers import (
    AssignmentLease,
    AssignmentStatus,
    ExecutionAssignment,
    NetworkPolicy,
    WorkerCapability,
    WorkerResourceLimits,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate, SecretRotate
from codex_web.services.mammouth_auth_delegation import (
    MAMMOUTH_AUTH_PURPOSE,
    MAMMOUTH_WORKER_HOME,
    MammouthAuthDelegationError,
    MammouthAuthDelegationService,
    MammouthAuthDelegationStaleError,
    MammouthAuthDelegationUnavailableError,
)
from codex_web.services.secrets import SecretBroker
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class MammouthAuthDelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.backend = LocalFileSecretBackend(root / "secrets")
        self.broker = SecretBroker(SecretStateStore(sqlite), {"local": self.backend})
        self.now = [1_800_000_000.0]
        self.service = MammouthAuthDelegationService(
            self.broker,
            executable="/usr/local/bin/mammouth",
            max_delegation_seconds=900,
            clock=lambda: self.now[0],
        )
        self.admin = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        self.worker = AuthenticationActor(
            identity_id="execution-worker-a",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("execution-worker:run", "secret:use"),
        )
        self.secret_value = "mammouth-secret-do-not-leak"
        self.secret = self.broker.create(
            SecretCreate(
                name="worker Mammouth API key",
                value=self.secret_value,
                provider="mammouth-ai",
                purpose=MAMMOUTH_AUTH_PURPOSE,
                allowed_identity_ids=[self.worker.identity_id],
                expires_at=self.now[0] + 300,
            ),
            actor=self.admin,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assignment(self, **overrides) -> ExecutionAssignment:
        values = {
            "id": "assignment-mammouth-1",
            "organization_id": "org-a",
            "workspace_id": "ws-a",
            "work_item_ref": "group/app#844",
            "execution_id": "exec-844",
            "project_id": "project-a",
            "resource_ids": ("repo-a",),
            "base_revision": "abc123",
            "execution_contract_version": "1.0",
            "required_capabilities": (
                WorkerCapability.GIT,
                WorkerCapability.COMMAND_EXECUTION,
            ),
            "sandbox": "workspace-write",
            "approval_policy": "on-request",
            "network": NetworkPolicy(),
            "limits": WorkerResourceLimits(),
            "secret_refs": (self.secret.id,),
            "deadline_at": self.now[0] + 600,
            "execution_workspace_id": "execws-a",
            "status": AssignmentStatus.RUNNING,
            "fence": 3,
            "lease": AssignmentLease(
                worker_id="worker-a",
                fence=3,
                lease_token="lease-token-that-is-long-enough",
                acquired_at=self.now[0] - 10,
                expires_at=self.now[0] + 120,
            ),
            "assigned_worker_id": "worker-a",
            "created_by": "admin",
        }
        values.update(overrides)
        return ExecutionAssignment(**values)

    def test_issue_returns_metadata_only_fenced_delegation(self) -> None:
        delegation = self.service.issue(
            self.assignment(),
            worker_id="worker-a",
            fence=3,
            actor=self.worker,
        )

        self.assertEqual(delegation.assignment_id, "assignment-mammouth-1")
        self.assertEqual(delegation.secret_id, self.secret.id)
        public = delegation.public()
        self.assertTrue(public["ready"])
        self.assertEqual(public["credential_store"], "ephemeral")
        self.assertTrue(public["environment_allowlisted"])
        self.assertNotIn(self.secret_value, repr(public))

    def test_use_exposes_only_explicit_worker_environment_inside_callback(self) -> None:
        seen = {}

        def consumer(launch):
            seen["command"] = launch.command
            seen["environment"] = dict(launch.environment)
            return {
                "key_copy": launch.environment["MAMMOUTH_API_KEY"],
                "home": launch.environment["HOME"],
            }

        result = self.service.use(
            self.assignment(),
            worker_id="worker-a",
            fence=3,
            actor=self.worker,
            consumer=consumer,
        )

        self.assertEqual(seen["command"], ("/usr/local/bin/mammouth",))
        self.assertEqual(
            seen["environment"],
            {
                "HOME": MAMMOUTH_WORKER_HOME,
                "MAMMOUTH_API_KEY": self.secret_value,
            },
        )
        self.assertEqual(result["key_copy"], "[REDACTED]")
        self.assertEqual(result["home"], MAMMOUTH_WORKER_HOME)

    def test_wrong_authority_purpose_or_provider_fails_closed(self) -> None:
        weak_worker = self.worker.model_copy(
            update={"service_scopes": ("execution-worker:run",)}
        )
        with self.assertRaisesRegex(
            MammouthAuthDelegationUnavailableError,
            "secret:use",
        ):
            self.service.issue(
                self.assignment(),
                worker_id="worker-a",
                fence=3,
                actor=weak_worker,
            )

        wrong = self.broker.create(
            SecretCreate(
                name="wrong provider key",
                value="wrong-purpose",
                provider="openai",
                purpose=MAMMOUTH_AUTH_PURPOSE,
                allowed_identity_ids=[self.worker.identity_id],
                expires_at=self.now[0] + 300,
            ),
            actor=self.admin,
        )
        with self.assertRaisesRegex(
            MammouthAuthDelegationUnavailableError,
            "exactly one usable",
        ):
            self.service.issue(
                self.assignment(secret_refs=(wrong.id,)),
                worker_id="worker-a",
                fence=3,
                actor=self.worker,
            )

    def test_stale_fence_rotation_expiry_and_deadline_fail_closed(self) -> None:
        with self.assertRaises(MammouthAuthDelegationStaleError):
            self.service.issue(
                self.assignment(),
                worker_id="worker-a",
                fence=2,
                actor=self.worker,
            )

        assignment = self.assignment()
        delegation = self.service.issue(
            assignment,
            worker_id="worker-a",
            fence=3,
            actor=self.worker,
        )
        self.broker.rotate(
            self.secret.id,
            SecretRotate(value="rotated-key"),
            actor=self.admin,
        )
        with self.assertRaisesRegex(
            MammouthAuthDelegationStaleError,
            "rotated or changed",
        ):
            self.service.validate_current(
                delegation,
                assignment,
                actor=self.worker,
            )

        refreshed = self.service.issue(
            assignment,
            worker_id="worker-a",
            fence=3,
            actor=self.worker,
        )
        self.now[0] = refreshed.expires_at + 1
        with self.assertRaises(MammouthAuthDelegationError):
            self.service.validate_current(
                refreshed,
                assignment,
                actor=self.worker,
            )


if __name__ == "__main__":
    unittest.main()
