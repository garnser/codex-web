from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from codex_web.action_intents import (
    ActionDecisionOutcome,
    ActionDecisionSnapshot,
    ActionIntentClaimRequest,
    ActionIntentCreate,
    ActionIntentStatus,
)
from codex_web.action_providers import ActionProviderBindingCreate, ActionRequest
from codex_web.authority import (
    AUTHORITY_ROLE_CATALOG_ID,
    AUTHORITY_ROLE_CATALOG_KIND,
    AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
    AuthorityDelegation,
    AuthorityGrant,
    AuthorityLevel,
    AuthorityRoleBinding,
    AuthorityRoleCatalogDefinition,
    AuthorityRoleDefinition,
)
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    Membership,
    MembershipRole,
    PrincipalKind,
    TenantScope,
)
from codex_web.resources import ResourceCreate, ResourceType
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import ActionExecutionService, ActionProviderRegistry
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.identity import IdentityService
from codex_web.services.reference_action_provider import ReferenceActionProvider
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class ActionIntentCanonicalAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(self.sqlite))
        self.identity.bootstrap_local()
        self.admin = self.identity.local_trusted_actor()
        self.definition_approver = AuthenticationActor(
            identity_id="action-authority-approver",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="local",
            workspace_id="default",
            roles=(MembershipRole.APPROVER,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED,
        )
        self.scope = TenantScope(
            organization_id="local",
            workspace_id="default",
        )

        human = self.identity.create_human_identity(
            display_name="Developer A",
            identity_id="developer-a",
        )
        self.identity.add_membership(
            Membership(
                identity_id=human.id,
                principal_kind=PrincipalKind.HUMAN,
                organization_id="local",
                workspace_id="default",
                roles=[MembershipRole.MEMBER],
            )
        )
        self.developer = self.identity.actor_for_identity(
            human.id,
            scope=self.scope,
        )

        self.resources = ResourceCatalogService(
            ResourceCatalogStore(self.sqlite)
        )
        self.resource = self.resources.create(
            ResourceCreate(
                resource_type=ResourceType.OTHER,
                name="Authority target",
            ),
            actor=self.admin,
        )
        self.definition_registry = DefinitionRegistryService(
            DefinitionRegistryStore(self.sqlite)
        )
        self.authority = install_authority_roles(
            self.definition_registry,
            self.resources,
        )

        self.registry = ActionProviderRegistry(
            ActionProviderStateStore(self.sqlite)
        )
        self.provider = ReferenceActionProvider()
        self.registry.register(self.provider)
        self.execution = ActionExecutionService(
            self.registry,
            self.resources,
        )
        self.binding = self.registry.bind(
            ActionProviderBindingCreate(
                provider_type=self.provider.provider_type,
                provider_instance=self.provider.provider_instance,
                resource_ids=(self.resource.id,),
            ),
            actor=self.admin,
            resources=self.resources,
        )
        self.service = ActionIntentService(
            ActionIntentStore(self.sqlite),
            self.execution,
            authority=self.authority,
            identity=self.identity,
        )
        self.worker = AuthenticationActor(
            identity_id="action-worker",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="local",
            workspace_id="default",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("action-intent:worker",),
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _request(self) -> ActionRequest:
        return ActionRequest(
            action_id="reference.set",
            organization_id="local",
            workspace_id="default",
            project_id="project-a",
            resource_ids=(self.resource.id,),
            parameters={"key": "authority-test", "value": "changed"},
        )

    def _active_record(self):
        return self.definition_registry.resolve(
            definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,
        )

    def _publish(self, catalog: AuthorityRoleCatalogDefinition):
        active = self._active_record()
        draft = self.definition_registry.create_draft(
            DefinitionDraftCreate(
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                payload=catalog.model_dump(mode="json"),
                actor="test",
                reason="authority enforcement test",
            )
        )
        preflight = self.definition_registry.publication_preflight(
            draft.record_id
        )
        approval_id = None
        if preflight.requires_approval:
            approval = self.definition_registry.record_publication_approval(
                draft.record_id,
                actor=self.definition_approver,
                reason="approve authority enforcement test definition",
            )
            approval_id = approval.id
        return self.definition_registry.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor="test-publisher",
                reason="activate authority enforcement test",
                expected_active_revision=active.revision,
                publication_approval_id=approval_id,
            ),
        )

    @staticmethod
    def _developer_catalog(
        *,
        include_binding: bool = True,
        delegation: AuthorityDelegation | None = None,
    ) -> AuthorityRoleCatalogDefinition:
        bindings = (
            (
                AuthorityRoleBinding(
                    id="developer-binding",
                    role_id="developer",
                    subject_kind="identity",
                    subject_id="developer-a",
                    organization_id="local",
                    workspace_id="default",
                    project_ids=("project-a",),
                ),
            )
            if include_binding
            else ()
        )
        return AuthorityRoleCatalogDefinition(
            roles=(
                AuthorityRoleDefinition(
                    id="developer",
                    name="Developer",
                    description="Scoped external-action authority.",
                    grants=(
                        AuthorityGrant(
                            id="developer.reference-set",
                            capability="action.reference.set",
                            level=AuthorityLevel.EXECUTE,
                            project_ids=("project-a",),
                            resource_ids=(),
                        ),
                    ),
                ),
            ),
            bindings=bindings,
            delegations=((delegation,) if delegation is not None else ()),
        )

    def _create(self, actor, **overrides):
        payload = {
            "binding_id": self.binding.id,
            "request": self._request(),
        }
        payload.update(overrides)
        return self.service.create(
            ActionIntentCreate(**payload),
            actor=actor,
        )

    async def _execute(self, intent_id: str):
        claimed = self.service.claim(
            ActionIntentClaimRequest(
                worker_id="worker-1",
                lease_seconds=30,
            ),
            actor=self.worker,
            intent_id=intent_id,
        )
        self.assertIsNotNone(claimed)
        return await self.service.execute_claimed(
            intent_id,
            "worker-1",
            actor=self.worker,
        )

    async def test_spoofed_caller_allow_cannot_authorize_unbound_actor(self):
        self._publish(
            self._developer_catalog(include_binding=False)
        )

        intent = self._create(
            self.developer,
            authority_decision=ActionDecisionSnapshot(
                outcome=ActionDecisionOutcome.ALLOW,
                source="approval:spoofed",
                reason="caller says yes",
            ),
        )

        self.assertEqual(intent.status, ActionIntentStatus.CANCELLED)
        self.assertEqual(
            intent.authority_decision.outcome,
            ActionDecisionOutcome.DENY,
        )
        self.assertEqual(
            intent.authority_decision.source,
            "canonical:role-authority",
        )
        self.assertNotEqual(
            intent.authority_decision.source,
            "approval:spoofed",
        )
        self.assertEqual(self.provider.values, {})

    async def test_bound_actor_queues_with_exact_definition_and_grant_provenance(self):
        record = self._publish(self._developer_catalog())

        intent = self._create(self.developer)

        self.assertEqual(intent.status, ActionIntentStatus.PENDING)
        self.assertEqual(
            intent.authority_decision.capabilities,
            ("action.reference.set",),
        )
        self.assertEqual(
            intent.authority_decision.role_ids,
            ("developer",),
        )
        self.assertEqual(
            intent.authority_decision.grant_ids,
            ("developer.reference-set",),
        )
        self.assertEqual(
            [item.record_id for item in intent.authority_decision.definition_refs],
            [record.record_id],
        )

    async def test_definition_quarantine_after_queue_cancels_before_provider_execution(self):
        self._publish(self._developer_catalog())
        intent = self._create(self.developer)
        self.assertEqual(intent.status, ActionIntentStatus.PENDING)

        active = self._active_record()
        self.definition_registry.quarantine(
            active.record_id,
            actor="security",
            reason="authority withdrawn before execution",
        )

        completed = await self._execute(intent.id)

        self.assertEqual(completed.status, ActionIntentStatus.CANCELLED)
        self.assertIsNotNone(completed.authority_recheck)
        self.assertEqual(
            completed.authority_recheck.outcome,
            ActionDecisionOutcome.DENY,
        )
        self.assertEqual(self.provider.values, {})

    async def test_requester_membership_revocation_after_queue_cancels_execution(self):
        self._publish(self._developer_catalog())
        intent = self._create(self.developer)
        self.assertEqual(intent.status, ActionIntentStatus.PENDING)

        now = time.time()

        def revoke(state):
            state.memberships = [
                (
                    item.model_copy(update={"revoked_at": now})
                    if (
                        item.identity_id == self.developer.identity_id
                        and item.organization_id == "local"
                        and item.workspace_id == "default"
                        and item.revoked_at is None
                    )
                    else item
                )
                for item in state.memberships
            ]
            return state

        self.identity.store.update(revoke)

        completed = await self._execute(intent.id)

        self.assertEqual(completed.status, ActionIntentStatus.CANCELLED)
        self.assertIn(
            "authority recheck unavailable",
            completed.last_error,
        )
        self.assertEqual(self.provider.values, {})

    async def test_delegated_authority_expiry_in_new_definition_revision_cancels_execution(self):
        delegation = AuthorityDelegation(
            id="temporary-delegation",
            role_id="developer",
            delegate_identity_id=self.developer.identity_id,
            delegated_by_identity_id=self.admin.identity_id,
            organization_id="local",
            workspace_id="default",
            project_ids=("project-a",),
            expires_at=time.time() + 3600,
            reason="temporary release duty",
        )
        self._publish(
            self._developer_catalog(
                include_binding=False,
                delegation=delegation,
            )
        )
        intent = self._create(self.developer)
        self.assertEqual(intent.status, ActionIntentStatus.PENDING)
        self.assertEqual(
            intent.authority_decision.delegation_ids,
            ("temporary-delegation",),
        )

        expired = delegation.model_copy(
            update={"expires_at": max(1.0, time.time() - 1)}
        )
        self._publish(
            self._developer_catalog(
                include_binding=False,
                delegation=expired,
            )
        )

        completed = await self._execute(intent.id)

        self.assertEqual(completed.status, ActionIntentStatus.CANCELLED)
        self.assertIsNotNone(completed.authority_recheck)
        self.assertEqual(
            completed.authority_recheck.outcome,
            ActionDecisionOutcome.DENY,
        )
        self.assertEqual(self.provider.values, {})

    async def test_local_admin_compatibility_role_still_executes_and_rechecks(self):
        intent = self._create(self.admin)
        self.assertEqual(intent.status, ActionIntentStatus.PENDING)
        self.assertEqual(
            intent.authority_decision.role_ids,
            ("local-admin",),
        )

        completed = await self._execute(intent.id)

        self.assertEqual(completed.status, ActionIntentStatus.SUCCEEDED)
        self.assertIsNotNone(completed.authority_recheck)
        self.assertEqual(
            completed.authority_recheck.outcome,
            ActionDecisionOutcome.ALLOW,
        )
        self.assertEqual(
            completed.authority_decision.decision_id,
            intent.authority_decision.decision_id,
        )
        self.assertEqual(
            self.provider.values["authority-test"],
            "changed",
        )


if __name__ == "__main__":
    unittest.main()
