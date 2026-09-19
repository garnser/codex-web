from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.business_data_sources import build_business_data_sources_router
from codex_web.business_context import (
    FactFreshness,
    FactSourceAuthority,
    FactValueType,
)
from codex_web.business_data_sources import (
    BusinessDataField,
    BusinessDataFieldMapping,
    BusinessDataSnapshot,
    BusinessDataSourceCapability,
    BusinessDataSourceCreate,
    BusinessDataSourceStatus,
)
from codex_web.canonical_events import CanonicalEventType
from codex_web.data_governance import DataClassification
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.business_context import BusinessContextService
from codex_web.services.business_data_sources import (
    BusinessDataSourceNotFoundError,
    BusinessDataSourceRegistry,
    BusinessDataSourceService,
    BusinessDataSourceUnavailableError,
)
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.data_governance import DataGovernanceService
from codex_web.services.provider_capacity import ProviderCapacityService
from codex_web.services.reference_business_data_sources import (
    ReferenceBillingDataSource,
    ReferenceCRMDataSource,
)
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.business_context import BusinessContextStore
from codex_web.storage.business_data_sources import BusinessDataSourceStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.data_governance import DataGovernanceStore
from codex_web.storage.provider_capacity import ProviderCapacityStore
from codex_web.storage.scheduler import SchedulerStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


class BusinessDataSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.clock = MutableClock(100.0)
        self.state = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.event_store = CanonicalEventStore(self.state)
        self.bus = CanonicalEventBus(self.event_store)
        self.ingestion = CanonicalEventIngestionService(self.bus)
        self.scheduler = SchedulerService(
            SchedulerStore(self.state),
            self.ingestion,
            clock=self.clock,
            owner_id="scheduler-business-data",
        )
        self.capacity = ProviderCapacityService(
            ProviderCapacityStore(self.state),
            scheduler=self.scheduler,
            clock=self.clock,
        )
        self.governance = DataGovernanceService(
            DataGovernanceStore(self.state)
        )
        self.context = BusinessContextService(
            BusinessContextStore(self.state),
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
                self.context.governance_action_handler,
            )
        self.registry = BusinessDataSourceRegistry()
        self.crm = ReferenceCRMDataSource("crm://prod")
        self.billing = ReferenceBillingDataSource("billing://prod")
        self.registry.register(
            "reference-crm",
            lambda record, actor: self.crm
            if record.source_instance == self.crm.source_instance
            else None,
        )
        self.registry.register(
            "reference-billing",
            lambda record, actor: self.billing
            if record.source_instance == self.billing.source_instance
            else None,
        )
        self.service = BusinessDataSourceService(
            BusinessDataSourceStore(self.state),
            self.registry,
            self.context,
            self.ingestion,
            scheduler=self.scheduler,
            provider_capacity=self.capacity,
            clock=self.clock,
        )
        self.bus.subscribe(
            self.service.handle_canonical_event,
            event_types=(
                CanonicalEventType.BUSINESS_DATA,
                CanonicalEventType.SCHEDULE,
            ),
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
            identity_id="reader-b",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-b",
            workspace_id="ws-b",
            roles=(MembershipRole.MEMBER,),
            assurance=AuthenticationAssurance.PRIMARY,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def snapshot(
        *,
        external_id: str,
        entity_key: str = "customer-1",
        name: str = "Northstar",
        arr: float = 100.0,
        sequence: int = 1,
        revision: str | None = None,
        tombstone: bool = False,
    ) -> BusinessDataSnapshot:
        return BusinessDataSnapshot(
            object_type="account",
            external_id=external_id,
            entity_key=entity_key,
            entity_name=name,
            fields=(
                BusinessDataField(
                    source_field="arr",
                    value=arr,
                ),
            ),
            source_updated_at=float(sequence * 10),
            source_sequence=sequence,
            source_revision=revision or f"rev-{sequence}",
            tombstone=tombstone,
        )

    def source_payload(
        self,
        *,
        source_type: str,
        instance: str,
        provider: str,
        authority: FactSourceAuthority,
        interval: int | None = None,
        page_size: int = 100,
    ) -> BusinessDataSourceCreate:
        return BusinessDataSourceCreate(
            name=f"{provider} accounts",
            source_type=source_type,
            source_instance=instance,
            provider_id=provider,
            scope="accounts",
            object_type="account",
            entity_type="customer",
            entity_key_namespace="customer-key",
            entity_name_authority=authority,
            entity_name_priority=10,
            field_mappings=(
                BusinessDataFieldMapping(
                    source_field="arr",
                    fact_key="annual_recurring_revenue",
                    value_type=FactValueType.NUMBER,
                    unit="usd",
                    authority=authority,
                    priority=10,
                    freshness_seconds=3600,
                    classification=DataClassification.CONFIDENTIAL,
                ),
            ),
            credential_ref=f"secret://{provider}",
            classification=DataClassification.CONFIDENTIAL,
            reconciliation_interval_seconds=interval,
            page_size=page_size,
        )

    def create_crm(self, **kwargs):
        return self.service.create(
            self.source_payload(
                source_type="reference-crm",
                instance="crm://prod",
                provider="crm",
                authority=FactSourceAuthority.AUTHORITATIVE,
                **kwargs,
            ),
            actor=self.admin,
        )

    def create_billing(self, **kwargs):
        return self.service.create(
            self.source_payload(
                source_type="reference-billing",
                instance="billing://prod",
                provider="billing",
                authority=FactSourceAuthority.SECONDARY,
                **kwargs,
            ),
            actor=self.admin,
        )

    async def test_two_provider_categories_converge_on_one_entity_and_keep_conflict_visible(self) -> None:
        self.crm.snapshots = [
            self.snapshot(external_id="crm-1", arr=100.0, sequence=1)
        ]
        self.billing.snapshots = [
            self.snapshot(external_id="bill-1", arr=90.0, sequence=1)
        ]
        crm_source = self.create_crm()
        billing_source = self.create_billing()

        crm_result = await self.service.sync(crm_source.id, actor=self.admin)
        billing_result = await self.service.sync(
            billing_source.id,
            actor=self.admin,
        )

        self.assertEqual(crm_result.projected, 1)
        self.assertEqual(billing_result.projected, 1)
        entities = self.context.list_entities(actor=self.admin)
        self.assertEqual(len(entities), 1)
        entity = entities[0]
        refs = self.context.list_external_records(
            actor=self.admin,
            business_entity_id=entity.id,
            include_inactive=True,
        )
        self.assertEqual(len(refs), 2)

        resolved = self.context.resolve_fact(
            entity.id,
            "annual_recurring_revenue",
            actor=self.admin,
            at=100.0,
        )
        self.assertEqual(resolved.selected.value, 100.0)
        self.assertTrue(resolved.conflict)
        self.assertEqual(len(resolved.conflict_fact_ids), 2)

        drift = self.service.drift(billing_source.id, actor=self.admin)
        self.assertEqual(len(drift), 1)
        self.assertEqual(
            drift[0]["conflicts"][0]["fact_key"],
            "annual_recurring_revenue",
        )

    async def test_duplicate_and_out_of_order_events_do_not_corrupt_current_state(self) -> None:
        source = self.create_crm()
        newer = self.snapshot(
            external_id="crm-1",
            arr=100.0,
            sequence=2,
        )
        first = await self.service.ingest_provider_event(
            source.id,
            {
                "event_id": "crm-event-2",
                "snapshot": newer,
                "occurred_at": 20.0,
            },
            actor=self.admin,
        )
        self.assertEqual(first.outcome, "projected")

        duplicate = await self.service.ingest_provider_event(
            source.id,
            {
                "event_id": "crm-event-2",
                "snapshot": newer,
                "occurred_at": 20.0,
            },
            actor=self.admin,
        )
        self.assertEqual(duplicate.outcome, "duplicate")

        stale = await self.service.ingest_provider_event(
            source.id,
            {
                "event_id": "crm-event-1-late",
                "snapshot": self.snapshot(
                    external_id="crm-1",
                    arr=50.0,
                    sequence=1,
                ),
                "occurred_at": 10.0,
            },
            actor=self.admin,
        )
        self.assertEqual(stale.outcome, "stale")

        entity = self.context.list_entities(actor=self.admin)[0]
        current = self.context.resolve_fact(
            entity.id,
            "annual_recurring_revenue",
            actor=self.admin,
            at=20.0,
        )
        self.assertEqual(current.selected.value, 100.0)
        stored_source = self.service.get(source.id, actor=self.admin)
        self.assertIsNone(stored_source.cursor)

    async def test_tombstone_revokes_source_and_removes_fact_from_current_truth(self) -> None:
        source = self.create_crm()
        await self.service.ingest_provider_event(
            source.id,
            {
                "event_id": "upsert-1",
                "snapshot": self.snapshot(
                    external_id="crm-1",
                    sequence=1,
                ),
            },
            actor=self.admin,
        )
        receipt = await self.service.ingest_provider_event(
            source.id,
            {
                "event_id": "delete-2",
                "snapshot": self.snapshot(
                    external_id="crm-1",
                    sequence=2,
                    tombstone=True,
                ),
            },
            actor=self.admin,
        )
        self.assertEqual(receipt.outcome, "tombstone")

        entity = self.context.list_entities(actor=self.admin)[0]
        resolved = self.context.resolve_fact(
            entity.id,
            "annual_recurring_revenue",
            actor=self.admin,
            at=20.0,
        )
        self.assertEqual(resolved.freshness, FactFreshness.SOURCE_REVOKED)
        external = self.context.list_external_records(
            actor=self.admin,
            business_entity_id=entity.id,
            include_inactive=True,
        )[0]
        self.assertEqual(external.lifecycle.value, "revoked")

    async def test_outage_preserves_cursor_and_resumes_from_last_completed_page(self) -> None:
        self.crm.snapshots = [
            self.snapshot(external_id="crm-1", entity_key="customer-1"),
            self.snapshot(
                external_id="crm-2",
                entity_key="customer-2",
                sequence=2,
            ),
        ]
        source = self.create_crm(page_size=1)
        first = await self.service.sync(
            source.id,
            actor=self.admin,
            max_pages=1,
        )
        self.assertFalse(first.exhausted)
        self.assertEqual(first.cursor_after, "1")

        self.crm.fail_with = RuntimeError("provider unavailable")
        with self.assertRaises(RuntimeError):
            await self.service.sync(source.id, actor=self.admin)
        failed = self.service.get(source.id, actor=self.admin)
        self.assertEqual(failed.cursor, "1")
        self.assertEqual(failed.status, BusinessDataSourceStatus.DEGRADED)
        capacity = self.capacity.get(
            source.provider_id,
            source.id,
            actor=self.admin,
        )
        self.assertEqual(capacity.status.value, "unavailable")

        self.crm.fail_with = None
        with self.assertRaises(BusinessDataSourceUnavailableError):
            await self.service.sync(source.id, actor=self.admin)

        self.clock.value += 61.0
        resumed = await self.service.sync(source.id, actor=self.admin)
        self.assertTrue(resumed.exhausted)
        self.assertEqual(resumed.records_seen, 1)
        self.assertEqual(resumed.cursor_before, "1")
        self.assertEqual(len(self.context.list_entities(actor=self.admin)), 2)

    async def test_rate_limit_uses_shared_capacity_backoff_without_cursor_loss(self) -> None:
        class RateLimitError(RuntimeError):
            status_code = 429
            headers = {"Retry-After": "30"}

        self.crm.snapshots = [
            self.snapshot(external_id="crm-1")
        ]
        source = self.create_crm()
        self.crm.fail_with = RateLimitError("too many requests")

        with self.assertRaises(RateLimitError):
            await self.service.sync(source.id, actor=self.admin)

        stored = self.service.get(source.id, actor=self.admin)
        self.assertIsNone(stored.cursor)
        capacity = self.capacity.get(
            source.provider_id,
            source.id,
            actor=self.admin,
        )
        self.assertEqual(capacity.status.value, "throttled")
        self.assertEqual(capacity.retry_at, 130.0)

    async def test_full_resync_is_bounded_and_idempotent(self) -> None:
        self.billing.snapshots = [
            self.snapshot(external_id="bill-1"),
            self.snapshot(
                external_id="bill-2",
                entity_key="customer-2",
                sequence=2,
            ),
        ]
        source = self.create_billing(page_size=1)
        initial = await self.service.sync(source.id, actor=self.admin)
        self.assertEqual(initial.pages, 2)
        self.assertEqual(initial.projected, 2)

        resync = await self.service.sync(
            source.id,
            actor=self.admin,
            full_resync=True,
        )
        self.assertEqual(resync.pages, 2)
        self.assertEqual(resync.duplicates, 2)
        self.assertEqual(len(self.context.list_entities(actor=self.admin)), 2)

    async def test_scheduler_reconciliation_runs_without_model_polling(self) -> None:
        self.crm.snapshots = [
            self.snapshot(external_id="crm-scheduled")
        ]
        source = self.create_crm(interval=60)
        self.assertIsNotNone(source.schedule_id)

        self.clock.value = 160.0
        result = await self.scheduler.run_due()
        self.assertEqual(result.emitted, 1)

        updated = self.service.get(source.id, actor=self.admin)
        self.assertEqual(updated.projected_records, 1)
        self.assertEqual(len(self.context.list_entities(actor=self.admin)), 1)

    def test_capabilities_are_materially_different_and_contract_has_no_write_surface(self) -> None:
        self.assertTrue(
            self.crm.capabilities.supports(
                BusinessDataSourceCapability.EVENTS
            )
        )
        self.assertTrue(
            self.crm.capabilities.supports(
                BusinessDataSourceCapability.INCREMENTAL_SYNC
            )
        )
        self.assertFalse(
            self.billing.capabilities.supports(
                BusinessDataSourceCapability.EVENTS
            )
        )
        self.assertFalse(
            self.billing.capabilities.supports(
                BusinessDataSourceCapability.INCREMENTAL_SYNC
            )
        )
        for adapter in (self.crm, self.billing):
            self.assertFalse(hasattr(adapter, "write"))
            self.assertFalse(hasattr(adapter, "create"))
            self.assertFalse(hasattr(adapter, "update"))

    async def test_cross_tenant_source_state_is_invisible(self) -> None:
        source = self.create_crm()
        self.assertEqual(self.service.list(actor=self.other), ())
        with self.assertRaises(BusinessDataSourceNotFoundError):
            self.service.get(source.id, actor=self.other)
        with self.assertRaises(BusinessDataSourceNotFoundError):
            await self.service.sync(source.id, actor=self.other)


class BusinessDataSourceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        state = SQLiteStateStore(Path(self.temp.name) / "api.sqlite3")
        event_bus = CanonicalEventBus(CanonicalEventStore(state))
        ingestion = CanonicalEventIngestionService(event_bus)
        governance = DataGovernanceService(DataGovernanceStore(state))
        context = BusinessContextService(
            BusinessContextStore(state),
            governance=governance,
        )
        for object_type in (
            "business_entity",
            "external_record_ref",
            "company_fact",
        ):
            governance.register_action_handler(
                object_type,
                context.governance_action_handler,
            )
        registry = BusinessDataSourceRegistry()
        self.crm = ReferenceCRMDataSource("crm://api")
        registry.register(
            "reference-crm",
            lambda record, actor: self.crm,
        )
        self.service = BusinessDataSourceService(
            BusinessDataSourceStore(state),
            registry,
            context,
            ingestion,
        )
        self.actor = AuthenticationActor(
            identity_id="admin",
            principal_kind=PrincipalKind.HUMAN,
            organization_id="org-a",
            workspace_id="ws-a",
            roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY,
        )
        app = FastAPI()

        @app.middleware("http")
        async def inject_actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_business_data_sources_router(self.service))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp.cleanup()

    @staticmethod
    def body():
        return {
            "name": "CRM",
            "source_type": "reference-crm",
            "source_instance": "crm://api",
            "provider_id": "crm",
            "scope": "accounts",
            "object_type": "account",
            "entity_type": "customer",
            "entity_key_namespace": "customer-key",
            "field_mappings": [
                {
                    "source_field": "arr",
                    "fact_key": "arr",
                    "value_type": "number",
                    "authority": "authoritative",
                }
            ],
        }

    def test_read_is_authenticated_but_configuration_requires_mfa(self) -> None:
        self.assertEqual(
            self.client.get("/api/business-data-sources").status_code,
            200,
        )
        denied = self.client.post(
            "/api/business-data-sources",
            json=self.body(),
        )
        self.assertEqual(denied.status_code, 403)
        self.assertIn("mfa", denied.json()["detail"].lower())

    def test_service_scope_can_configure_but_read_contract_exposes_no_action_authority(self) -> None:
        self.actor = AuthenticationActor(
            identity_id="business-sync",
            principal_kind=PrincipalKind.SERVICE,
            organization_id="org-a",
            workspace_id="ws-a",
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("business-data:admin",),
        )
        created = self.client.post(
            "/api/business-data-sources",
            json=self.body(),
        )
        self.assertEqual(created.status_code, 200)
        source_id = created.json()["item"]["id"]
        fetched = self.client.get(
            f"/api/business-data-sources/{source_id}"
        )
        self.assertEqual(fetched.status_code, 200)
        item = fetched.json()["item"]
        self.assertNotIn("action_provider", item)
        self.assertNotIn("mutation_capabilities", item)


if __name__ == "__main__":
    unittest.main()
