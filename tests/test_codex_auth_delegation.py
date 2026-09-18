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
from codex_web.services.codex_auth_delegation import (
    CODEX_AUTH_PURPOSE,
    CODEX_WORKER_HOME,
    CodexAuthDelegationService,
    CodexAuthDelegationStaleError,
    CodexAuthDelegationUnavailableError,
)
from codex_web.services.secrets import SecretBroker
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class CodexAuthDelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.backend = LocalFileSecretBackend(root / "secrets")
        self.broker = SecretBroker(
            SecretStateStore(sqlite),
            {"local": self.backend},
        )
        self.now = [1_800_000_000.0]
        self.service = CodexAuthDelegationService(
            self.broker,
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
        self.secret = self.broker.create(
            SecretCreate(
                name="worker Codex access token",
                value="codex-access-token-do-not-leak",
                provider="codex",
                purpose=CODEX_AUTH_PURPOSE,
                allowed_identity_ids=[self.worker.identity_id],
                expires_at=self.now[0] + 300,
            ),
            actor=self.admin,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assignment(self, **overrides) -> ExecutionAssignment:
        values = {
            "id": "assignment-codex-1",
            "organization_id": "org-a",
            "workspace_id": "ws-a",
            "work_item_ref": "group/app#42",
            "execution_id": "exec-42",
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

    def test_issue_returns_metadata_only_assignment_bound_delegation(self) -> None:
        delegation = self.service.issue(
            self.assignment(),
            worker_id="worker-a",
            fence=3,
            actor=self.worker,
        )

        self.assertEqual(delegation.assignment_id, "assignment-codex-1")
        self.assertEqual(delegation.worker_id, "worker-a")
        self.assertEqual(delegation.fence, 3)
        self.assertEqual(delegation.secret_id, self.secret.id)
        self.assertEqual(delegation.expires_at, self.now[0] + 300)
        public = delegation.public()
        self.assertTrue(public["ready"])
        self.assertEqual(public["credential_store"], "ephemeral")
        self.assertTrue(public["child_environment_filtered"])
        self.assertNotIn("token", " ".join(public.keys()).casefold())
        self.assertNotIn("codex-access-token-do-not-leak", repr(public))

    def test_use_supplies_ephemeral_codex_environment_and_scrubs_escape(self) -> None:
        seen = {}

        def consumer(launch):
            seen["command"] = launch.command
            seen["environment"] = dict(launch.environment)
            return {
                "token_copy": launch.environment["CODEX_ACCESS_TOKEN"],
                "home": launch.environment["CODEX_HOME"],
            }

        result = self.service.use(
            self.assignment(),
            worker_id="worker-a",
            fence=3,
            actor=self.worker,
            consumer=consumer,
        )

        self.assertEqual(seen["environment"]["CODEX_HOME"], CODEX_WORKER_HOME)
        self.assertEqual(
            seen["environment"]["CODEX_ACCESS_TOKEN"],
            "codex-access-token-do-not-leak",
        )
        command = seen["command"]
        self.assertEqual(command[0], "codex")
        self.assertEqual(command[-1], "app-server")
        joined = " ".join(command)
        self.assertIn('cli_auth_credentials_store="ephemeral"', joined)
        self.assertIn('shell_environment_policy.inherit="none"', joined)
        self.assertIn("shell_environment_policy.ignore_default_excludes=false", joined)
        self.assertIn(
            'shell_environment_policy.filters.CODEX_ACCESS_TOKEN="exclude"',
            joined,
        )
        self.assertNotIn("auth.json", joined)
        self.assertEqual(result["token_copy"], "[REDACTED]")
        self.assertEqual(result["home"], CODEX_WORKER_HOME)

    def test_human_or_worker_without_secret_use_cannot_issue_delegation(self) -> None:
        with self.assertRaisesRegex(
            CodexAuthDelegationUnavailableError,
            "execution-worker:run",
        ):
            self.service.issue(
                self.assignment(),
                worker_id="worker-a",
                fence=3,
                actor=self.admin,
            )

        weak_worker = self.worker.model_copy(
            update={"service_scopes": ("execution-worker:run",)}
        )
        with self.assertRaisesRegex(
            CodexAuthDelegationUnavailableError,
            "secret:use",
        ):
            self.service.issue(
                self.assignment(),
                worker_id="worker-a",
                fence=3,
                actor=weak_worker,
            )

    def test_stale_fence_reassignment_and_expired_lease_fail_closed(self) -> None:
        for assignment, fence in (
            (self.assignment(), 2),
            (
                self.assignment(
                    assigned_worker_id="worker-b",
                    lease=self.assignment().lease.model_copy(
                        update={"worker_id": "worker-b"}
                    ),
                ),
                3,
            ),
            (
                self.assignment(
                    lease=self.assignment().lease.model_copy(
                        update={"expires_at": self.now[0] - 1}
                    ),
                ),
                3,
            ),
        ):
            with self.subTest(assignment=assignment.assigned_worker_id, fence=fence):
                with self.assertRaises(CodexAuthDelegationStaleError):
                    self.service.issue(
                        assignment,
                        worker_id="worker-a",
                        fence=fence,
                        actor=self.worker,
                    )

    def test_cross_tenant_secret_reference_is_not_delegated(self) -> None:
        other_admin = self.admin.model_copy(
            update={
                "identity_id": "admin-b",
                "organization_id": "org-b",
                "workspace_id": "ws-b",
            }
        )
        other_secret = self.broker.create(
            SecretCreate(
                name="other tenant token",
                value="other-tenant-token",
                provider="codex",
                purpose=CODEX_AUTH_PURPOSE,
                allowed_identity_ids=[self.worker.identity_id],
                expires_at=self.now[0] + 300,
            ),
            actor=other_admin,
        )

        with self.assertRaisesRegex(
            CodexAuthDelegationUnavailableError,
            "unavailable to the worker",
        ):
            self.service.issue(
                self.assignment(secret_refs=(other_secret.id,)),
                worker_id="worker-a",
                fence=3,
                actor=self.worker,
            )

    def test_non_purpose_specific_or_long_lived_secret_is_rejected(self) -> None:
        wrong = self.broker.create(
            SecretCreate(
                name="generic provider secret",
                value="generic-secret",
                provider="codex",
                purpose="general",
                allowed_identity_ids=[self.worker.identity_id],
                expires_at=self.now[0] + 300,
            ),
            actor=self.admin,
        )
        with self.assertRaisesRegex(
            CodexAuthDelegationUnavailableError,
            "exactly one usable",
        ):
            self.service.issue(
                self.assignment(secret_refs=(wrong.id,)),
                worker_id="worker-a",
                fence=3,
                actor=self.worker,
            )

        long_lived = self.broker.create(
            SecretCreate(
                name="long lived Codex token",
                value="long-lived-secret",
                provider="openai",
                purpose=CODEX_AUTH_PURPOSE,
                allowed_identity_ids=[self.worker.identity_id],
                expires_at=self.now[0] + 1_200,
            ),
            actor=self.admin,
        )
        with self.assertRaisesRegex(
            CodexAuthDelegationUnavailableError,
            "lifetime exceeds",
        ):
            self.service.issue(
                self.assignment(
                    secret_refs=(long_lived.id,),
                    deadline_at=self.now[0] + 2_000,
                ),
                worker_id="worker-a",
                fence=3,
                actor=self.worker,
            )

    def test_rotation_or_expiry_invalidates_existing_delegation(self) -> None:
        assignment = self.assignment()
        delegation = self.service.issue(
            assignment,
            worker_id="worker-a",
            fence=3,
            actor=self.worker,
        )
        self.broker.rotate(
            self.secret.id,
            SecretRotate(value="rotated-token"),
            actor=self.admin,
        )

        with self.assertRaisesRegex(
            CodexAuthDelegationStaleError,
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
        with self.assertRaises(CodexAuthDelegationError):
            self.service.validate_current(
                refreshed,
                assignment,
                actor=self.worker,
            )

    def test_status_never_exposes_credential_material(self) -> None:
        status = self.service.status(
            self.assignment(),
            worker_id="worker-a",
            fence=3,
            actor=self.worker,
        )
        self.assertTrue(status["ready"])
        self.assertNotIn("codex-access-token-do-not-leak", repr(status))

        denied = self.service.status(
            self.assignment(),
            worker_id="worker-a",
            fence=2,
            actor=self.worker,
        )
        self.assertFalse(denied["ready"])
        self.assertIn("stale", str(denied["reason"]).lower())


if __name__ == "__main__":
    unittest.main()
