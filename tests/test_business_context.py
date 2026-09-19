from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from codex_web.business_context import (
    BusinessEntityCreate,
    BusinessEntityRelationshipCreate,
    BusinessEntityType,
    BusinessReferenceLinks,
    BusinessRelationshipType,
    CompanyFactCreate,
    CompanyFactLifecycle,
    CompanyFactSource,
    ExternalRecordLifecycle,
    ExternalRecordRefCreate,
    ExternalRecordRefUpdate,
    FactFreshness,
    FactQuality,
    FactSourceAuthority,
    FactValueType,
)
from codex_web.data_governance import (
    DataClassification,
    GovernanceAction,
    GovernanceActionRequest,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.business_context import (
    BusinessContextConflictError,
    BusinessContextNotFoundError,
    BusinessContextService,
)
from codex_web.services.data_governance import DataGovernanceService
from codex_web.storage.business_context import BusinessContextStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class BusinessContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.clock = _Clock(100.0)
        self.governance = DataGovernanceService(DataGovernanceStore(state))
        self.service = BusinessContextService(
            BusinessContextStore(state),
            governance=self.governance,
            clock=self.clock,
        )
        for object_type in (
            "business_entity",
            "external_record_ref",
            "company_fact",
        ):
            self.governance.register_action_handler(
                object_type,
                self.service.governance_action_handler,
            )
        self.admin = AuthenticationActor(
            identity_id="admin-a",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )
        self.other = AuthenticationActor(
            identity_id="admin-b",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-b",
            workspace_id="ws-b",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def entity(self, **overrides):
        values = {
            "entity_type": BusinessEntityType.CUSTOMER,
            "name": "Northstar Retail",
            "classification": DataClassification.INTERNAL,
            "links": BusinessReferenceLinks(
                project_ids=("project-a",),
                resource_ids=("resource-a",),
                goal_ids=("goal-a",),
                decision_ids=("decision-a",),
                metric_ids=("metric-a",),
                evidence_ids=("evidence-a",),
            ),
        }
        values.update(overrides)
        return self.service.create_entity(
            BusinessEntityCreate(**values),
            actor=self.admin,
        )

    def external(self, entity_id: str, **overrides):
        values = {
            "system": "crm",
            "provider": "salesforce",
            "provider_instance": "prod",
            "object_type": "account",
            "external_id": f"acct-{len(self.service.store.load().external_records)+1}",
            "display_name": "Northstar Retail",
            "business_entity_ids": (entity_id,),
            "classification": DataClassification.INTERNAL,
            "synced_at": self.clock.value,
        }
        values.update(overrides)
        return self.service.create_external_record(
            ExternalRecordRefCreate(**values),
            actor=self.admin,
        )

    def fact(
        self,
        entity_id: str,
        *,
        key: str = "annual_recurring_revenue",
        value=1000.0,
        external_record_ref_id: str | None = None,
        authority=FactSourceAuthority.OBSERVED,
        priority: int = 0,
        quality=FactQuality.NORMAL,
        observed_at: float | None = None,
        freshness_seconds: int | None = None,
        classification=DataClassification.INTERNAL,
    ):
        return self.service.create_fact(
            CompanyFactCreate(
                business_entity_id=entity_id,
                key=key,
                value_type=FactValueType.NUMBER,
                value=value,
                unit="usd",
                source=CompanyFactSource(
                    source="crm.account.arr",
                    provider="salesforce",
                    external_record_ref_id=external_record_ref_id,
                    authority=authority,
                    priority=priority,
                ),
                quality=quality,
                observed_at=(
                    self.clock.value if observed_at is None else observed_at
                ),
                freshness_seconds=freshness_seconds,
                classification=classification,
            ),
            actor=self.admin,
        )

    def test_conflicting_providers_are_deterministic_and_inspectable(self) -> None:
        entity = self.entity()
        secondary_ref = self.external(
            entity.id,
            system="billing",
            provider="stripe",
            external_id="customer-1",
        )
        authoritative_ref = self.external(
            entity.id,
            system="crm",
            provider="salesforce",
            external_id="account-1",
        )
        secondary = self.fact(
            entity.id,
            value=900.0,
            external_record_ref_id=secondary_ref.id,
            authority=FactSourceAuthority.SECONDARY,
            priority=50,
            quality=FactQuality.VERIFIED,
        )
        authoritative = self.fact(
            entity.id,
            value=1000.0,
            external_record_ref_id=authoritative_ref.id,
            authority=FactSourceAuthority.AUTHORITATIVE,
            priority=1,
            quality=FactQuality.NORMAL,
        )

        resolved = self.service.resolve_fact(
            entity.id,
            "annual_recurring_revenue",
            actor=self.admin,
        )
        self.assertEqual(resolved.selected.id, authoritative.id)
        self.assertTrue(resolved.conflict)
        self.assertEqual(
            set(resolved.conflict_fact_ids),
            {secondary.id, authoritative.id},
        )
        self.assertEqual(resolved.freshness, FactFreshness.FRESH)

        self.service.update_external_record(
            authoritative_ref.id,
            ExternalRecordRefUpdate(
                lifecycle=ExternalRecordLifecycle.REVOKED,
            ),
            actor=self.admin,
        )
        after_revoke = self.service.resolve_fact(
            entity.id,
            "annual_recurring_revenue",
            actor=self.admin,
        )
        self.assertEqual(after_revoke.selected.id, secondary.id)
        self.assertFalse(after_revoke.conflict)
        self.assertIn(
            authoritative.id,
            after_revoke.revoked_source_fact_ids,
        )

    def test_stale_fact_does_not_become_current_truth(self) -> None:
        entity = self.entity()
        fact = self.fact(
            entity.id,
            observed_at=100.0,
            freshness_seconds=10,
        )

        resolved = self.service.resolve_fact(
            entity.id,
            fact.key,
            actor=self.admin,
            at=111.0,
        )
        self.assertIsNone(resolved.selected)
        self.assertEqual(resolved.freshness, FactFreshness.STALE)
        self.assertEqual(resolved.stale_fact_ids, (fact.id,))

    def test_supersession_preserves_history_and_selects_replacement(self) -> None:
        entity = self.entity()
        old = self.fact(entity.id, value=1000.0)
        self.clock.value = 200.0
        replacement = self.service.supersede_fact(
            old.id,
            CompanyFactCreate(
                business_entity_id=entity.id,
                key=old.key,
                value_type=FactValueType.NUMBER,
                value=1200.0,
                unit="usd",
                source=CompanyFactSource(
                    source="crm.account.arr",
                    authority=FactSourceAuthority.AUTHORITATIVE,
                ),
                quality=FactQuality.VERIFIED,
                observed_at=self.clock.value,
            ),
            actor=self.admin,
        )

        historical = self.service.get_fact(old.id, actor=self.admin)
        self.assertEqual(
            historical.lifecycle,
            CompanyFactLifecycle.SUPERSEDED,
        )
        self.assertEqual(historical.superseded_by_fact_id, replacement.id)
        self.assertEqual(replacement.supersedes_fact_id, old.id)

        resolved = self.service.resolve_fact(
            entity.id,
            old.key,
            actor=self.admin,
        )
        self.assertEqual(resolved.selected.id, replacement.id)

    def test_deleted_source_data_invalidates_current_fact(self) -> None:
        entity = self.entity()
        external = self.external(entity.id)
        fact = self.fact(
            entity.id,
            external_record_ref_id=external.id,
            authority=FactSourceAuthority.AUTHORITATIVE,
        )
        self.assertIsNotNone(external.governance_record_id)

        request = self.governance.request_action(
            GovernanceActionRequest(
                record_id=external.governance_record_id,
                action=GovernanceAction.DELETE,
                reason="source deletion request",
            ),
            actor=self.admin,
        )
        self.governance.execute_request(request.id, actor=self.admin)

        deleted = self.service.get_external_record(
            external.id,
            actor=self.admin,
        )
        self.assertEqual(
            deleted.lifecycle,
            ExternalRecordLifecycle.DELETED,
        )
        resolved = self.service.resolve_fact(
            entity.id,
            fact.key,
            actor=self.admin,
        )
        self.assertIsNone(resolved.selected)
        self.assertEqual(
            resolved.freshness,
            FactFreshness.SOURCE_REVOKED,
        )
        self.assertEqual(
            resolved.revoked_source_fact_ids,
            (fact.id,),
        )

    def test_fact_governance_deletion_removes_value_from_resolution(self) -> None:
        entity = self.entity()
        fact = self.fact(entity.id)
        self.assertIsNotNone(fact.governance_record_id)

        request = self.governance.request_action(
            GovernanceActionRequest(
                record_id=fact.governance_record_id,
                action=GovernanceAction.DELETE,
                reason="retention expiry",
            ),
            actor=self.admin,
        )
        self.governance.execute_request(request.id, actor=self.admin)

        deleted = self.service.get_fact(fact.id, actor=self.admin)
        self.assertEqual(deleted.lifecycle, CompanyFactLifecycle.DELETED)
        self.assertIsNone(deleted.value)
        resolved = self.service.resolve_fact(
            entity.id,
            fact.key,
            actor=self.admin,
        )
        self.assertEqual(resolved.freshness, FactFreshness.MISSING)
        self.assertIsNone(resolved.selected)

    def test_governance_classification_is_inherited_by_derived_fact(self) -> None:
        entity = self.entity(
            classification=DataClassification.CONFIDENTIAL
        )
        fact = self.fact(
            entity.id,
            classification=DataClassification.INTERNAL,
        )
        self.assertEqual(
            fact.classification,
            DataClassification.CONFIDENTIAL,
        )
        governed = self.governance.get_record(
            fact.governance_record_id,
            self.admin,
        )
        self.assertEqual(
            governed.classification,
            DataClassification.CONFIDENTIAL,
        )

    def test_cross_tenant_isolation_hides_entities_external_refs_and_facts(self) -> None:
        entity = self.entity()
        external = self.external(entity.id)
        fact = self.fact(
            entity.id,
            external_record_ref_id=external.id,
        )

        self.assertEqual(
            self.service.list_entities(actor=self.other),
            (),
        )
        self.assertEqual(
            self.service.list_external_records(actor=self.other),
            (),
        )
        self.assertEqual(
            self.service.list_facts(actor=self.other),
            (),
        )
        for getter, object_id in (
            (self.service.get_entity, entity.id),
            (self.service.get_external_record, external.id),
            (self.service.get_fact, fact.id),
        ):
            with self.assertRaises(BusinessContextNotFoundError):
                getter(object_id, actor=self.other)

    def test_external_identity_is_unique_per_workspace(self) -> None:
        entity = self.entity()
        self.external(entity.id, external_id="stable-account")
        with self.assertRaises(BusinessContextConflictError):
            self.external(entity.id, external_id="stable-account")

        # The same provider identity is allowed in another tenant/workspace.
        other_entity = self.service.create_entity(
            BusinessEntityCreate(
                entity_type=BusinessEntityType.CUSTOMER,
                name="Other Tenant Customer",
            ),
            actor=self.other,
        )
        other_ref = self.service.create_external_record(
            ExternalRecordRefCreate(
                system="crm",
                provider="salesforce",
                provider_instance="prod",
                object_type="account",
                external_id="stable-account",
                business_entity_ids=(other_entity.id,),
            ),
            actor=self.other,
        )
        self.assertEqual(other_ref.organization_id, "org-b")

    def test_relationships_and_canonical_links_are_preserved(self) -> None:
        customer = self.entity()
        product = self.service.create_entity(
            BusinessEntityCreate(
                entity_type=BusinessEntityType.PRODUCT,
                name="Checkout",
                links=BusinessReferenceLinks(
                    project_ids=("checkout-project",),
                    metric_ids=("metric-checkout",),
                ),
            ),
            actor=self.admin,
        )
        relationship = self.service.create_relationship(
            BusinessEntityRelationshipCreate(
                from_entity_id=customer.id,
                to_entity_id=product.id,
                relationship_type=BusinessRelationshipType.USES,
            ),
            actor=self.admin,
        )
        rows = self.service.relationships(customer.id, actor=self.admin)
        self.assertEqual(rows, (relationship,))
        self.assertEqual(
            customer.links.goal_ids,
            ("goal-a",),
        )
        self.assertEqual(
            customer.links.decision_ids,
            ("decision-a",),
        )
        self.assertEqual(
            customer.links.evidence_ids,
            ("evidence-a",),
        )

    def test_fact_values_are_bounded_scalars_not_provider_payloads(self) -> None:
        entity = self.entity()
        with self.assertRaises(ValidationError):
            CompanyFactCreate(
                business_entity_id=entity.id,
                key="raw_payload",
                value_type=FactValueType.STRING,
                value={"entire": "provider payload"},
                source=CompanyFactSource(source="crm.raw"),
            )
        with self.assertRaises(ValidationError):
            CompanyFactCreate(
                business_entity_id=entity.id,
                key="oversized",
                value_type=FactValueType.STRING,
                value="x" * 8001,
                source=CompanyFactSource(source="crm.raw"),
            )


if __name__ == "__main__":
    unittest.main()
