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
from codex_web.services.anthropic_auth_delegation import (
    ANTHROPIC_AUTH_PURPOSE,
    CLAUDE_WORKER_HOME,
    AnthropicAuthDelegationError,
    AnthropicAuthDelegationService,
    AnthropicAuthDelegationStaleError,
    AnthropicAuthDelegationUnavailableError,
)
from codex_web.services.secrets import SecretBroker
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class AnthropicAuthDelegationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        sqlite = SQLiteStateStore(root / "state.sqlite3")
        self.backend = LocalFileSecretBackend(root / "secrets")
        self.broker = SecretBroker(SecretStateStore(sqlite), {"local": self.backend})
        self.now = [1_800_000_000.0]
        self.service = AnthropicAuthDelegationService(
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
                name="worker Anthropic API key",
                value="anthropic-secret-do-not-leak",
                provider="anthropic",
                purpose=ANTHROPIC_AUTH_PURPOSE,
                allowed_identity_ids=[self.worker.identity_id],
                expires_at=self.now[0] + 300,
            ),
            actor=self.admin,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assignment(self, **overrides) -> ExecutionAssignment:
        values = {
            "id": "assignment-claude-1",
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

        self.assertEqual(delegation.assignment_id, "assignment-claude-1")
        self.assertEqual(delegation.secret_id, self.secret.id)
        public = delegation.public()
        self.assertTrue(public["ready"])
        self.assertEqual(public["credential_store"], "ephemeral")
        self.assertTrue(public["child_environment_filtered"])
        self.assertNotIn("anthropic-secret-do-not-leak", repr(public))

    def test_use_supplies_ephemeral_key_and_mandatory_child_scrub(self) -> None:
        seen = {}

        def consumer(launch):
            seen["command"] = launch.command
            seen["environment"] = dict(launch.environment)
            return {
                "key_copy": launch.environment["ANTHROPIC_API_KEY"],
                "home": launch.environment["HOME"],
            }

        result = self.service.use(
            self.assignment(),
            worker_id="worker-a",
            fence=3,
            actor=self.worker,
            consumer=consumer,
        )

        self.assertEqual(seen["environment"]["HOME"], CLAUDE_WORKER_HOME)
        self.assertEqual(
            seen["environment"]["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"],
            "1",
        )
        self.assertEqual(
            seen["environment"]["ANTHROPIC_API_KEY"],
            "anthropic-secret-do-not-leak",
        )
        command = seen["command"]
        self.assertEqual(command[0], "claude")
        self.assertIn("--bare", command)
        self.assertEqual(
            command[command.index("--input-format") + 1],
            "stream-json",
        )
        self.assertEqual(
            command[command.index("--output-format") + 1],
            "stream-json",
        )
        self.assertEqual(result["key_copy"], "[REDACTED]")
        self.assertEqual(result["home"], CLAUDE_WORKER_HOME)

    def test_wrong_scope_or_secret_purpose_fails_closed(self) -> None:
        weak_worker = self.worker.model_copy(
            update={"service_scopes": ("execution-worker:run",)}
        )
        with self.assertRaisesRegex(
            AnthropicAuthDelegationUnavailableError,
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
                name="generic key",
                value="wrong-purpose",
                provider="anthropic",
                purpose="general",
                allowed_identity_ids=[self.worker.identity_id],
                expires_at=self.now[0] + 300,
            ),
            actor=self.admin,
        )
        with self.assertRaisesRegex(
            AnthropicAuthDelegationUnavailableError,
            "exactly one usable",
        ):
            self.service.issue(
                self.assignment(secret_refs=(wrong.id,)),
                worker_id="worker-a",
                fence=3,
                actor=self.worker,
            )

    def test_stale_fence_rotation_expiry_and_deadline_fail_closed(self) -> None:
        with self.assertRaises(AnthropicAuthDelegationStaleError):
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
            AnthropicAuthDelegationStaleError,
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
        with self.assertRaises(AnthropicAuthDelegationError):
            self.service.validate_current(
                refreshed,
                assignment,
                actor=self.worker,
            )


if __name__ == "__main__":
    unittest.main()
