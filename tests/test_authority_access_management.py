from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.authority import AuthorityEvaluationRequest, AuthorityLevel
from codex_web.definitions import DefinitionPublishRequest
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.authority_access_management import (
    AuthorityAccessManagementService,
)
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class AuthorityAccessManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        self.authority = install_authority_roles(self.registry)
        self.service = AuthorityAccessManagementService(
            self.authority,
            self.registry,
        )
        self.admin = AuthenticationActor(
            identity_id="local-admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        self.target = AuthenticationActor(
            identity_id="developer-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.MFA,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _decision(self):
        return self.authority.evaluate(
            AuthorityEvaluationRequest(
                capability="repository.write",
                level=AuthorityLevel.EXECUTE,
            ),
            actor=self.target,
        )

    def test_authority_expansion_stays_pending_until_independent_approval(self):
        self.assertEqual(self._decision().outcome.value, "deny")

        staged = self.service.add_direct_binding(
            actor=self.admin,
            identity_id=self.target.identity_id,
            role_id="local-admin",
            reason="grant developer temporary operational access",
        )

        self.assertEqual(staged["status"], "pending_approval")
        self.assertTrue(staged["assessment"].requires_independent_approval)
        self.assertIn("new identity Role binding", " ".join(staged["assessment"].reasons))
        self.assertEqual(self._decision().outcome.value, "deny")

        record = staged["record"]
        self.registry.approve_publication(
            record.record_id,
            actor="independent-approver",
            reference="ACCESS-APPROVAL-1",
            reason="reviewed requested access expansion",
        )
        self.registry.publish(
            record.record_id,
            DefinitionPublishRequest(
                actor=self.admin.identity_id,
                reason="publish independently approved access",
                expected_active_revision=None,
            ),
        )

        self.assertEqual(self._decision().outcome.value, "allow")

    def test_direct_binding_removal_publishes_authority_reduction(self):
        staged = self.service.add_direct_binding(
            actor=self.admin,
            identity_id=self.target.identity_id,
            role_id="local-admin",
            reason="stage access",
        )
        record = staged["record"]
        self.registry.approve_publication(
            record.record_id,
            actor="independent-approver",
            reference="ACCESS-APPROVAL-2",
            reason="approve fixture access",
        )
        self.registry.publish(
            record.record_id,
            DefinitionPublishRequest(
                actor=self.admin.identity_id,
                reason="publish fixture access",
                expected_active_revision=None,
            ),
        )
        self.assertEqual(self._decision().outcome.value, "allow")

        removed = self.service.remove_direct_binding(
            actor=self.admin,
            binding_id=staged["binding"].id,
            reason="remove developer operational access",
        )

        self.assertEqual(removed["status"], "published")
        self.assertFalse(removed["assessment"].requires_independent_approval)
        self.assertEqual(removed["record"].published_by, self.admin.identity_id)
        self.assertEqual(self._decision().outcome.value, "deny")

    def test_duplicate_direct_binding_is_idempotent(self):
        staged = self.service.add_direct_binding(
            actor=self.admin,
            identity_id=self.target.identity_id,
            role_id="local-admin",
            reason="stage access",
        )
        record = staged["record"]
        self.registry.approve_publication(
            record.record_id,
            actor="independent-approver",
            reference="ACCESS-APPROVAL-3",
            reason="approve fixture access",
        )
        self.registry.publish(
            record.record_id,
            DefinitionPublishRequest(
                actor=self.admin.identity_id,
                reason="publish fixture access",
                expected_active_revision=None,
            ),
        )

        duplicate = self.service.add_direct_binding(
            actor=self.admin,
            identity_id=self.target.identity_id,
            role_id="local-admin",
            reason="repeat same assignment",
        )

        self.assertEqual(duplicate["status"], "already_effective")
        self.assertEqual(duplicate["binding"].id, staged["binding"].id)


if __name__ == "__main__":
    unittest.main()
