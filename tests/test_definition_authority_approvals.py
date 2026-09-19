from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.authority import (
    AUTHORITY_ROLE_CATALOG_ID,
    AUTHORITY_ROLE_CATALOG_KIND,
    AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
    AuthorityApprovalRequirement,
    AuthorityAutonomyRisk,
    AuthorityDelegation,
    AuthorityEnvironment,
    AuthorityGrant,
    AuthorityLevel,
    AuthorityRoleBinding,
    AuthorityRoleCatalogDefinition,
    AuthorityRoleDefinition,
    validate_authority_role_catalog,
)
from codex_web.definitions import (
    DefinitionDraftCreate,
    DefinitionPublishRequest,
    DefinitionRollbackRequest,
)
from codex_web.resources import ResourceRisk, ResourceSensitivity, ResourceType
from codex_web.services.authority_publication import assess_authority_catalog_publication
from codex_web.services.definitions import (
    DefinitionApprovalRequiredError,
    DefinitionKindSchema,
    DefinitionRegistryService,
)
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class DefinitionAuthorityApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.service = DefinitionRegistryService(DefinitionRegistryStore(sqlite))
        self.service.register_schema(
            DefinitionKindSchema(
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                validate=validate_authority_role_catalog,
                assess_publish=assess_authority_catalog_publication,
            )
        )
        self.base = self._catalog()
        created = self.service.bootstrap(
            [
                DefinitionDraftCreate(
                    definition_id=AUTHORITY_ROLE_CATALOG_ID,
                    kind=AUTHORITY_ROLE_CATALOG_KIND,
                    definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                    payload=self.base.model_dump(mode="json"),
                    actor="bootstrap",
                    reason="test baseline",
                )
            ]
        )
        self.assertEqual(len(created), 1)
        self.active = created[0]

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _catalog() -> AuthorityRoleCatalogDefinition:
        return AuthorityRoleCatalogDefinition(
            roles=(
                AuthorityRoleDefinition(
                    id="reader",
                    name="Reader",
                    description="Inherited read authority.",
                    grants=(
                        AuthorityGrant(
                            id="reader.read",
                            capability="repository.read",
                            level=AuthorityLevel.READ,
                            project_ids=("project-a",),
                        ),
                    ),
                ),
                AuthorityRoleDefinition(
                    id="developer",
                    name="Developer",
                    description="Scoped delivery authority.",
                    grants=(
                        AuthorityGrant(
                            id="developer.deploy",
                            capability="deploy.release",
                            level=AuthorityLevel.PREPARE,
                            project_ids=("project-a",),
                            resource_ids=("resource-a",),
                            resource_types=(ResourceType.REPOSITORY,),
                            resource_risks=(ResourceRisk.MEDIUM,),
                            resource_sensitivities=(ResourceSensitivity.INTERNAL,),
                            environments=(AuthorityEnvironment.STAGING,),
                            max_amount_usd=100.0,
                            max_input_tokens=1000,
                            max_output_tokens=500,
                            max_model_calls=2,
                            max_autonomous_risk=AuthorityAutonomyRisk.MEDIUM,
                            approvals=AuthorityApprovalRequirement(
                                count=2,
                                role_ids=("release-approver", "security-approver"),
                            ),
                        ),
                    ),
                ),
            ),
            bindings=(
                AuthorityRoleBinding(
                    id="developer-binding",
                    role_id="developer",
                    subject_kind="identity",
                    subject_id="developer-a",
                    organization_id="local",
                    workspace_id="default",
                    project_ids=("project-a",),
                ),
            ),
            delegations=(
                AuthorityDelegation(
                    id="temporary-delegation",
                    role_id="developer",
                    delegate_identity_id="delegate-a",
                    delegated_by_identity_id="manager-a",
                    organization_id="local",
                    workspace_id="default",
                    project_ids=("project-a",),
                    expires_at=1000.0,
                    reason="temporary duty",
                ),
            ),
        )

    def _draft(self, catalog: AuthorityRoleCatalogDefinition):
        return self.service.create_draft(
            DefinitionDraftCreate(
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                payload=catalog.model_dump(mode="json"),
                actor="publisher",
                reason="test change",
            )
        )

    def _developer_grant(self, catalog):
        role = next(item for item in catalog.roles if item.id == "developer")
        return next(item for item in role.grants if item.id == "developer.deploy")

    def _replace_grant(self, catalog, **updates):
        roles = []
        for role in catalog.roles:
            if role.id != "developer":
                roles.append(role)
                continue
            grants = tuple(
                grant.model_copy(update=updates)
                if grant.id == "developer.deploy"
                else grant
                for grant in role.grants
            )
            roles.append(role.model_copy(update={"grants": grants}))
        return catalog.model_copy(update={"roles": tuple(roles)})

    def _publish_with_independent_approval(self, draft):
        self.service.approve_publication(
            draft.record_id,
            actor="approver",
            reference="CAB-42",
            reason="reviewed expansion",
        )
        return self.service.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor="publisher",
                reason="approved change",
                expected_active_revision=self.active.revision,
            ),
        )

    def test_sensitive_boundary_categories_require_independent_approval(self):
        grant = self._developer_grant(self.base)
        cases = [
            (
                "authority level",
                self._replace_grant(self.base, level=AuthorityLevel.EXECUTE),
                "authority level increased",
            ),
            (
                "project scope",
                self._replace_grant(
                    self.base,
                    project_ids=("project-a", "project-b"),
                ),
                "project scope broadened",
            ),
            (
                "resource id scope",
                self._replace_grant(
                    self.base,
                    resource_ids=("resource-a", "resource-b"),
                ),
                "resource ID scope broadened",
            ),
            (
                "resource type scope",
                self._replace_grant(
                    self.base,
                    resource_types=(
                        ResourceType.REPOSITORY,
                        ResourceType.OTHER,
                    ),
                ),
                "resource type scope broadened",
            ),
            (
                "resource risk scope",
                self._replace_grant(
                    self.base,
                    resource_risks=(ResourceRisk.MEDIUM, ResourceRisk.HIGH),
                ),
                "resource risk scope broadened",
            ),
            (
                "resource sensitivity scope",
                self._replace_grant(
                    self.base,
                    resource_sensitivities=(
                        ResourceSensitivity.INTERNAL,
                        ResourceSensitivity.CONFIDENTIAL,
                    ),
                ),
                "resource sensitivity scope broadened",
            ),
            (
                "production environment",
                self._replace_grant(
                    self.base,
                    environments=(
                        AuthorityEnvironment.STAGING,
                        AuthorityEnvironment.PRODUCTION,
                    ),
                ),
                "production environment authority added",
            ),
            (
                "monetary ceiling",
                self._replace_grant(self.base, max_amount_usd=101.0),
                "monetary ceiling increased",
            ),
            (
                "input-token ceiling",
                self._replace_grant(self.base, max_input_tokens=1001),
                "input-token ceiling increased",
            ),
            (
                "output-token ceiling",
                self._replace_grant(self.base, max_output_tokens=501),
                "output-token ceiling increased",
            ),
            (
                "model-call ceiling",
                self._replace_grant(self.base, max_model_calls=3),
                "model-call ceiling increased",
            ),
            (
                "autonomy risk",
                self._replace_grant(
                    self.base,
                    max_autonomous_risk=AuthorityAutonomyRisk.HIGH,
                ),
                "autonomous-risk ceiling increased",
            ),
            (
                "approval count",
                self._replace_grant(
                    self.base,
                    approvals=grant.approvals.model_copy(update={"count": 1}),
                ),
                "required approval count decreased",
            ),
            (
                "approver roles",
                self._replace_grant(
                    self.base,
                    approvals=grant.approvals.model_copy(
                        update={
                            "role_ids": (
                                "release-approver",
                                "security-approver",
                                "team-lead",
                            )
                        }
                    ),
                ),
                "approver Role scope broadened",
            ),
        ]

        for label, candidate, expected in cases:
            with self.subTest(label=label):
                draft = self._draft(candidate)
                assessment = self.service.publication_assessment(draft.record_id)
                self.assertTrue(assessment.requires_independent_approval)
                self.assertTrue(
                    any(expected in reason for reason in assessment.reasons),
                    assessment.reasons,
                )
                with self.assertRaises(DefinitionApprovalRequiredError):
                    self.service.publish(
                        draft.record_id,
                        DefinitionPublishRequest(
                            actor="publisher",
                            expected_active_revision=self.active.revision,
                        ),
                    )

    def test_new_inheritance_binding_delegation_and_extended_expiry_are_sensitive(self):
        reader = next(item for item in self.base.roles if item.id == "reader")
        developer = next(item for item in self.base.roles if item.id == "developer")
        variants = [
            (
                self.base.model_copy(
                    update={
                        "roles": (
                            reader,
                            developer.model_copy(update={"inherits": ("reader",)}),
                        )
                    }
                ),
                "inherited authority added",
            ),
            (
                self.base.model_copy(
                    update={
                        "bindings": (
                            *self.base.bindings,
                            AuthorityRoleBinding(
                                id="new-binding",
                                role_id="developer",
                                subject_kind="team",
                                subject_id="team-platform",
                                organization_id="local",
                                workspace_id="default",
                            ),
                        )
                    }
                ),
                "new team Role binding",
            ),
            (
                self.base.model_copy(
                    update={
                        "delegations": (
                            *self.base.delegations,
                            AuthorityDelegation(
                                id="new-delegation",
                                role_id="developer",
                                delegate_identity_id="delegate-b",
                                delegated_by_identity_id="manager-a",
                                organization_id="local",
                                workspace_id="default",
                                project_ids=("project-a",),
                                expires_at=1200.0,
                                reason="coverage",
                            ),
                        )
                    }
                ),
                "new delegated authority",
            ),
            (
                self.base.model_copy(
                    update={
                        "delegations": (
                            self.base.delegations[0].model_copy(
                                update={"expires_at": 2000.0}
                            ),
                        )
                    }
                ),
                "expiry extended",
            ),
        ]
        for candidate, expected in variants:
            draft = self._draft(candidate)
            assessment = self.service.publication_assessment(draft.record_id)
            self.assertTrue(assessment.requires_independent_approval)
            self.assertTrue(
                any(expected in reason for reason in assessment.reasons),
                assessment.reasons,
            )

    def test_restrictive_change_publishes_without_extra_approval(self):
        candidate = self._replace_grant(
            self.base,
            level=AuthorityLevel.READ,
            max_amount_usd=50.0,
            max_input_tokens=500,
            max_output_tokens=250,
            max_model_calls=1,
            max_autonomous_risk=AuthorityAutonomyRisk.LOW,
            project_ids=("project-a",),
            resource_ids=("resource-a",),
            environments=(AuthorityEnvironment.STAGING,),
            approvals=AuthorityApprovalRequirement(
                count=3,
                role_ids=("release-approver",),
            ),
        )
        draft = self._draft(candidate)
        assessment = self.service.publication_assessment(draft.record_id)
        self.assertFalse(assessment.requires_independent_approval)

        published = self.service.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor="publisher",
                expected_active_revision=self.active.revision,
            ),
        )
        self.assertEqual(published.lifecycle.value, "published")
        self.assertEqual(published.publication_approvals, ())

    def test_independent_approval_is_durable_and_self_approval_does_not_satisfy_gate(self):
        candidate = self._replace_grant(
            self.base,
            level=AuthorityLevel.EXECUTE,
        )
        draft = self._draft(candidate)
        self.service.approve_publication(
            draft.record_id,
            actor="publisher",
            reference="SELF-1",
            reason="self review",
        )
        with self.assertRaises(DefinitionApprovalRequiredError):
            self.service.publish(
                draft.record_id,
                DefinitionPublishRequest(
                    actor="publisher",
                    expected_active_revision=self.active.revision,
                ),
            )

        approved = self.service.approve_publication(
            draft.record_id,
            actor="approver",
            reference="CAB-99",
            reason="independent review",
        )
        self.assertEqual(len(approved.publication_approvals), 2)
        published = self.service.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor="publisher",
                reason="ship approved expansion",
                expected_active_revision=self.active.revision,
                approval_metadata={"ticket": "CHANGE-99"},
            ),
        )
        self.assertEqual(
            published.publication_approvals[-1].approved_by,
            "approver",
        )
        self.assertEqual(
            published.publication_approvals[-1].reference,
            "CAB-99",
        )
        self.assertEqual(published.approval_metadata["ticket"], "CHANGE-99")

    def test_approval_becomes_stale_when_active_revision_changes(self):
        candidate = self._replace_grant(
            self.base,
            level=AuthorityLevel.EXECUTE,
        )
        draft = self._draft(candidate)
        self.service.approve_publication(
            draft.record_id,
            actor="approver",
            reference="CAB-stale",
            reason="approved against r1",
        )

        restrictive = self._replace_grant(
            self.base,
            max_amount_usd=50.0,
        )
        restrictive_draft = self._draft(restrictive)
        current = self.service.publish(
            restrictive_draft.record_id,
            DefinitionPublishRequest(
                actor="other-publisher",
                expected_active_revision=self.active.revision,
            ),
        )

        with self.assertRaises(DefinitionApprovalRequiredError):
            self.service.publish(
                draft.record_id,
                DefinitionPublishRequest(
                    actor="publisher",
                    expected_active_revision=current.revision,
                ),
            )

    def test_sensitive_rollback_creates_draft_and_requires_approval(self):
        expanded = self._replace_grant(
            self.base,
            level=AuthorityLevel.EXECUTE,
        )
        expanded_draft = self._draft(expanded)
        expanded_record = self._publish_with_independent_approval(expanded_draft)
        self.active = expanded_record

        restrictive = self._replace_grant(
            expanded,
            level=AuthorityLevel.READ,
        )
        restrictive_draft = self._draft(restrictive)
        restrictive_record = self.service.publish(
            restrictive_draft.record_id,
            DefinitionPublishRequest(
                actor="publisher",
                expected_active_revision=self.active.revision,
            ),
        )
        self.active = restrictive_record

        with self.assertRaises(DefinitionApprovalRequiredError) as ctx:
            self.service.rollback(
                DefinitionRollbackRequest(
                    definition_id=AUTHORITY_ROLE_CATALOG_ID,
                    kind=AUTHORITY_ROLE_CATALOG_KIND,
                    target_revision=expanded_record.revision,
                    actor="publisher",
                    expected_active_revision=restrictive_record.revision,
                    reason="restore execution authority",
                )
            )
        rollback_draft_id = ctx.exception.record_id
        self.assertIsNotNone(rollback_draft_id)
        rollback_draft = self.service.get_record(rollback_draft_id)
        self.assertEqual(
            rollback_draft.rollback_of_record_id,
            expanded_record.record_id,
        )
        self.assertEqual(rollback_draft.lifecycle.value, "draft")

        self.service.approve_publication(
            rollback_draft_id,
            actor="approver",
            reference="CAB-rollback",
            reason="approved rollback expansion",
        )
        restored = self.service.publish(
            rollback_draft_id,
            DefinitionPublishRequest(
                actor="publisher",
                expected_active_revision=restrictive_record.revision,
            ),
        )
        self.assertEqual(restored.lifecycle.value, "published")

    def test_reactivating_disabled_or_deprecated_role_is_sensitive(self):
        disabled_roles = tuple(
            role.model_copy(update={"lifecycle": "disabled"})
            if role.id == "developer"
            else role
            for role in self.base.roles
        )
        disabled = self.base.model_copy(update={"roles": disabled_roles})
        disabled_draft = self._draft(disabled)
        disabled_record = self.service.publish(
            disabled_draft.record_id,
            DefinitionPublishRequest(
                actor="publisher",
                expected_active_revision=self.active.revision,
            ),
        )
        self.active = disabled_record

        reactivated_draft = self._draft(self.base)
        reactivated = self.service.publication_assessment(
            reactivated_draft.record_id
        )
        self.assertTrue(reactivated.requires_independent_approval)
        self.assertTrue(
            any("reactivated" in reason for reason in reactivated.reasons),
            reactivated.reasons,
        )

        deprecated_roles = tuple(
            role.model_copy(update={"lifecycle": "deprecated"})
            if role.id == "developer"
            else role
            for role in self.base.roles
        )
        deprecated = self.base.model_copy(update={"roles": deprecated_roles})
        deprecated_draft = self._draft(deprecated)
        self.service.approve_publication(
            deprecated_draft.record_id,
            actor="approver",
            reference="CAB-deprecated",
            reason="move from disabled to deprecated",
        )
        deprecated_record = self.service.publish(
            deprecated_draft.record_id,
            DefinitionPublishRequest(
                actor="publisher",
                expected_active_revision=self.active.revision,
            ),
        )
        self.active = deprecated_record

        active_draft = self._draft(self.base)
        active_assessment = self.service.publication_assessment(
            active_draft.record_id
        )
        self.assertTrue(active_assessment.requires_independent_approval)
        self.assertTrue(
            any(
                "returned to active" in reason
                for reason in active_assessment.reasons
            ),
            active_assessment.reasons,
        )

    def test_generic_definition_kind_remains_unchanged(self):
        self.service.register_schema(
            DefinitionKindSchema(
                kind="generic-test",
                schema_version="1.0",
                validate=lambda payload: dict(payload),
            )
        )
        draft = self.service.create_draft(
            DefinitionDraftCreate(
                definition_id="generic.test",
                kind="generic-test",
                definition_schema_version="1.0",
                payload={"enabled": True},
                actor="publisher",
            )
        )
        assessment = self.service.publication_assessment(draft.record_id)
        self.assertFalse(assessment.requires_independent_approval)
        published = self.service.publish(
            draft.record_id,
            DefinitionPublishRequest(actor="publisher"),
        )
        self.assertEqual(published.lifecycle.value, "published")

    def test_import_drops_source_approval_attestations(self):
        candidate = self._replace_grant(
            self.base,
            level=AuthorityLevel.EXECUTE,
        )
        draft = self._draft(candidate)
        self.service.approve_publication(
            draft.record_id,
            actor="approver",
            reference="CAB-export",
            reason="approval must not transfer",
        )
        exported = self.service.export()

        imported = self.service.import_records(
            exported,
            actor="importer",
        )
        imported_matching = [
            item
            for item in imported
            if item.payload == draft.payload
        ]
        self.assertTrue(imported_matching)
        self.assertTrue(
            all(item.publication_approvals == () for item in imported_matching)
        )


if __name__ == "__main__":
    unittest.main()
