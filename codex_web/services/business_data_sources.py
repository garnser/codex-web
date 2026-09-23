from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable

from codex_web.business_context import (
    FACT_AUTHORITY_RANK,
    BusinessEntityCreate,
    BusinessEntityLifecycle,
    BusinessEntityUpdate,
    BusinessFieldProvenance,
    CompanyFactCreate,
    CompanyFactLifecycle,
    CompanyFactSource,
    ExternalRecordLifecycle,
    ExternalRecordRefCreate,
    ExternalRecordRefUpdate,
)
from codex_web.business_data_sources import (
    BUSINESS_DATA_SOURCE_CONTRACT,
    BusinessDataEventKind,
    BusinessDataField,
    BusinessDataPage,
    BusinessDataProjectionReceipt,
    BusinessDataSnapshot,
    BusinessDataSource,
    BusinessDataSourceCapabilities,
    BusinessDataSourceCapability,
    BusinessDataSourceCreate,
    BusinessDataSourceRecord,
    BusinessDataSourceStatus,
    BusinessDataSourceState,
    BusinessDataSyncResult,
    BusinessEntityBinding,
)
from codex_web.canonical_events import CanonicalEventType
from codex_web.compatibility import CanonicalEventEnvelope, ContractCompatibilityError
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    PrincipalKind,
    TenantScope,
)
from codex_web.provider_capacity import (
    ProviderCapacityReport,
    ProviderCapacityStatus,
)
from codex_web.scheduler import (
    MisfirePolicy,
    RecurrenceKind,
    ScheduleCreate,
    ScheduleRecurrence,
)
from codex_web.services.business_context import (
    BusinessContextConflictError,
    BusinessContextNotFoundError,
    BusinessContextService,
)
from codex_web.services.canonical_events import CanonicalEventIngestionService
from codex_web.services.provider_capacity import ProviderCapacityService
from codex_web.services.scheduler import SchedulerService
from codex_web.storage.business_data_sources import BusinessDataSourceStore


BusinessDataSourceFactory = Callable[
    [BusinessDataSourceRecord, AuthenticationActor],
    BusinessDataSource | None,
]


class BusinessDataSourceError(RuntimeError):
    pass


class BusinessDataSourceNotFoundError(BusinessDataSourceError):
    pass


class BusinessDataSourceConflictError(BusinessDataSourceError):
    pass


class BusinessDataSourceUnavailableError(BusinessDataSourceError):
    pass


class BusinessDataSourceValidationError(BusinessDataSourceError):
    pass


class BusinessDataSourceRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, BusinessDataSourceFactory] = {}
        self._tenant_factories: dict[
            tuple[str, str, str],
            BusinessDataSourceFactory,
        ] = {}

    @staticmethod
    def _key(source_type: str) -> str:
        value = str(source_type or "").strip().casefold()
        if not value:
            raise ValueError("business data source type must not be empty")
        return value

    def register(
        self,
        source_type: str,
        factory: BusinessDataSourceFactory,
        *,
        scope: TenantScope | None = None,
    ) -> None:
        key = self._key(source_type)
        if scope is None:
            self._factories[key] = factory
            return
        self._tenant_factories[
            (scope.organization_id, scope.workspace_id, key)
        ] = factory

    def resolve(
        self,
        record: BusinessDataSourceRecord,
        *,
        actor: AuthenticationActor,
        required: bool = True,
    ) -> BusinessDataSource | None:
        key = self._key(record.source_type)
        factory = self._tenant_factories.get(
            (record.organization_id, record.workspace_id, key)
        )
        if factory is None:
            factory = self._factories.get(key)
        if factory is None:
            if required:
                raise BusinessDataSourceUnavailableError(
                    f"no BusinessDataSource adapter registered for {record.source_type!r}"
                )
            return None
        adapter = factory(record, actor)
        if adapter is None:
            if required:
                raise BusinessDataSourceUnavailableError(
                    f"BusinessDataSource adapter {record.source_type!r} is unavailable"
                )
            return None
        if adapter.source_type.casefold() != record.source_type.casefold():
            raise BusinessDataSourceValidationError(
                "resolved adapter source_type does not match configured source"
            )
        if adapter.source_instance.rstrip("/") != record.source_instance.rstrip("/"):
            raise BusinessDataSourceValidationError(
                "resolved adapter source_instance does not match configured source"
            )
        version = str(
            getattr(adapter, "contract_version", BUSINESS_DATA_SOURCE_CONTRACT.current)
            or BUSINESS_DATA_SOURCE_CONTRACT.current
        ).strip()
        try:
            BUSINESS_DATA_SOURCE_CONTRACT.require(version)
        except (ContractCompatibilityError, ValueError) as exc:
            raise BusinessDataSourceValidationError(
                f"incompatible BusinessDataSource contract version {version!r}"
            ) from exc
        return adapter


class BusinessDataSourceService:
    SCHEDULE_TRIGGER = "business_data.reconcile"
    MAX_PAGES_PER_RUN = 25

    def __init__(
        self,
        store: BusinessDataSourceStore,
        registry: BusinessDataSourceRegistry,
        business_context: BusinessContextService,
        canonical_events: CanonicalEventIngestionService,
        *,
        scheduler: SchedulerService | None = None,
        provider_capacity: ProviderCapacityService | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.registry = registry
        self.business_context = business_context
        self.canonical_events = canonical_events
        self.scheduler = scheduler
        self.provider_capacity = provider_capacity
        self.clock = clock

    @staticmethod
    def _same_scope(
        item: BusinessDataSourceRecord,
        actor: AuthenticationActor,
    ) -> bool:
        return (
            item.organization_id == actor.organization_id
            and item.workspace_id == actor.workspace_id
        )

    @staticmethod
    def _system_actor(record: BusinessDataSourceRecord) -> AuthenticationActor:
        return AuthenticationActor(
            identity_id=f"business-data-sync:{record.id}",
            principal_kind=PrincipalKind.SERVICE,
            organization_id=record.organization_id,
            workspace_id=record.workspace_id,
            assurance=AuthenticationAssurance.SERVICE_TOKEN,
            service_scopes=("business-data:admin",),
        )

    def _source(
        self,
        state: BusinessDataSourceState,
        source_id: str,
        *,
        actor: AuthenticationActor | None = None,
    ) -> BusinessDataSourceRecord:
        item = state.sources.get(source_id)
        if item is None or (
            actor is not None and not self._same_scope(item, actor)
        ):
            raise BusinessDataSourceNotFoundError("business data source not found")
        return item

    def get(
        self,
        source_id: str,
        *,
        actor: AuthenticationActor,
    ) -> BusinessDataSourceRecord:
        return self._source(self.store.load(), source_id, actor=actor)

    def list(
        self,
        *,
        actor: AuthenticationActor,
    ) -> tuple[BusinessDataSourceRecord, ...]:
        rows = [
            item
            for item in self.store.load().sources.values()
            if self._same_scope(item, actor)
        ]
        rows.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return tuple(rows)

    def _update_source(
        self,
        source_id: str,
        changes: dict[str, object],
    ) -> BusinessDataSourceRecord:
        saved: list[BusinessDataSourceRecord] = []

        def apply(state: BusinessDataSourceState) -> BusinessDataSourceState:
            current = self._source(state, source_id)
            replacement = current.model_copy(
                update={**changes, "updated_at": float(self.clock())}
            )
            state.sources[source_id] = replacement
            saved.append(replacement)
            return state

        self.store.update(apply)
        return saved[0]

    def create(
        self,
        payload: BusinessDataSourceCreate,
        *,
        actor: AuthenticationActor,
    ) -> BusinessDataSourceRecord:
        now = float(self.clock())
        draft = BusinessDataSourceRecord(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            name=payload.name,
            source_type=payload.source_type,
            source_instance=payload.source_instance,
            provider_id=payload.provider_id,
            extension_installation_id=payload.extension_installation_id,
            scope=payload.scope,
            object_type=payload.object_type,
            entity_type=payload.entity_type,
            entity_key_namespace=payload.entity_key_namespace,
            entity_name_authority=payload.entity_name_authority,
            entity_name_priority=payload.entity_name_priority,
            field_mappings=payload.field_mappings,
            credential_ref=payload.credential_ref,
            classification=payload.classification,
            reconciliation_interval_seconds=payload.reconciliation_interval_seconds,
            page_size=payload.page_size,
            created_by=actor.identity_id,
            created_at=now,
            updated_at=now,
        )
        adapter = self.registry.resolve(draft, actor=actor, required=True)
        assert adapter is not None
        capabilities = tuple(
            sorted(adapter.capabilities.supported, key=lambda item: item.value)
        )
        if not (
            adapter.capabilities.supports(BusinessDataSourceCapability.INCREMENTAL_SYNC)
            or adapter.capabilities.supports(BusinessDataSourceCapability.PAGED_DISCOVERY)
            or adapter.capabilities.supports(BusinessDataSourceCapability.DISCOVERY)
        ):
            raise BusinessDataSourceValidationError(
                "BusinessDataSource must support incremental sync or discovery"
            )
        item = draft.model_copy(
            update={
                "capabilities": capabilities,
                "credential_required": bool(
                    getattr(adapter, "credential_required", True)
                ),
            }
        )

        def apply(state: BusinessDataSourceState) -> BusinessDataSourceState:
            duplicate = next(
                (
                    row
                    for row in state.sources.values()
                    if row.organization_id == item.organization_id
                    and row.workspace_id == item.workspace_id
                    and row.source_type.casefold() == item.source_type.casefold()
                    and row.source_instance.rstrip("/")
                    == item.source_instance.rstrip("/")
                    and row.scope == item.scope
                    and row.object_type.casefold() == item.object_type.casefold()
                ),
                None,
            )
            if duplicate is not None:
                raise BusinessDataSourceConflictError(
                    "equivalent business data source already exists"
                )
            state.sources[item.id] = item
            return state

        self.store.update(apply)
        if (
            self.scheduler is not None
            and item.reconciliation_interval_seconds is not None
        ):
            try:
                schedule = self.scheduler.create(
                    ScheduleCreate(
                        name=f"Reconcile business data source {item.name}",
                        tenant_id=item.organization_id,
                        workspace_id=item.workspace_id,
                        trigger_type=self.SCHEDULE_TRIGGER,
                        payload={"source_id": item.id},
                        due_at=now + item.reconciliation_interval_seconds,
                        recurrence=ScheduleRecurrence(
                            kind=RecurrenceKind.INTERVAL,
                            interval_seconds=item.reconciliation_interval_seconds,
                        ),
                        misfire_policy=MisfirePolicy.BOUNDED_CATCH_UP,
                        misfire_grace_seconds=0.0,
                        catch_up_limit=1,
                    ),
                    actor_id=actor.identity_id,
                )
                item = self._update_source(
                    item.id,
                    {"schedule_id": schedule.id},
                )
            except Exception:
                def rollback(state: BusinessDataSourceState) -> BusinessDataSourceState:
                    state.sources.pop(item.id, None)
                    return state
                self.store.update(rollback)
                raise
        return item

    def set_status(
        self,
        source_id: str,
        status: BusinessDataSourceStatus,
        *,
        actor: AuthenticationActor,
    ) -> BusinessDataSourceRecord:
        current = self.get(source_id, actor=actor)
        if current.schedule_id and self.scheduler is not None:
            if (
                status == BusinessDataSourceStatus.ACTIVE
                and current.status in {
                    BusinessDataSourceStatus.PAUSED,
                    BusinessDataSourceStatus.QUARANTINED,
                }
            ):
                self.scheduler.resume(current.schedule_id, actor_id=actor.identity_id)
            elif (
                status in {
                    BusinessDataSourceStatus.PAUSED,
                    BusinessDataSourceStatus.QUARANTINED,
                }
                and current.status not in {
                    BusinessDataSourceStatus.PAUSED,
                    BusinessDataSourceStatus.QUARANTINED,
                }
            ):
                self.scheduler.pause(current.schedule_id, actor_id=actor.identity_id)
        return self._update_source(source_id, {"status": status})

    @staticmethod
    def _entity_id(
        source: BusinessDataSourceRecord,
        snapshot: BusinessDataSnapshot,
    ) -> str:
        material = (
            f"{source.organization_id}\n{source.workspace_id}\n"
            f"{source.entity_key_namespace.casefold()}\n"
            f"{snapshot.entity_key.casefold()}"
        ).encode("utf-8")
        return f"business-entity-{hashlib.sha256(material).hexdigest()[:32]}"

    @staticmethod
    def _snapshot_payload(snapshot: BusinessDataSnapshot) -> dict[str, object]:
        return {
            "object_type": snapshot.object_type,
            "external_id": snapshot.external_id,
            "entity_key": snapshot.entity_key,
            "entity_name": snapshot.entity_name,
            "fields": [
                {
                    "source_field": item.source_field,
                    "value": item.value,
                }
                for item in snapshot.fields
            ],
            "display_name": snapshot.display_name,
            "external_url": snapshot.external_url,
            "source_updated_at": snapshot.source_updated_at,
            "source_sequence": snapshot.source_sequence,
            "source_revision": snapshot.source_revision,
            "tombstone": snapshot.tombstone,
        }

    @staticmethod
    def _snapshot_from_payload(payload: dict[str, object]) -> BusinessDataSnapshot:
        raw_fields = payload.get("fields")
        fields: tuple[BusinessDataField, ...] = ()
        if isinstance(raw_fields, list):
            fields = tuple(
                BusinessDataField(
                    source_field=str(item.get("source_field") or ""),
                    value=item.get("value"),
                )
                for item in raw_fields
                if isinstance(item, dict)
            )
        return BusinessDataSnapshot(
            object_type=str(payload.get("object_type") or ""),
            external_id=str(payload.get("external_id") or ""),
            entity_key=str(payload.get("entity_key") or ""),
            entity_name=str(payload.get("entity_name") or ""),
            fields=fields,
            display_name=(
                str(payload["display_name"])
                if payload.get("display_name") is not None
                else None
            ),
            external_url=(
                str(payload["external_url"])
                if payload.get("external_url") is not None
                else None
            ),
            source_updated_at=(
                float(payload["source_updated_at"])
                if payload.get("source_updated_at") is not None
                else None
            ),
            source_sequence=(
                int(payload["source_sequence"])
                if payload.get("source_sequence") is not None
                else None
            ),
            source_revision=(
                str(payload["source_revision"])
                if payload.get("source_revision") is not None
                else None
            ),
            tombstone=bool(payload.get("tombstone", False)),
        )

    @staticmethod
    def _snapshot_fingerprint(
        source_id: str,
        snapshot: BusinessDataSnapshot,
    ) -> str:
        payload = BusinessDataSourceService._snapshot_payload(snapshot)
        material = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()[:32]
        return f"snapshot:{source_id}:{snapshot.external_id}:{digest}"

    def _record_receipt(
        self,
        receipt: BusinessDataProjectionReceipt,
    ) -> BusinessDataProjectionReceipt:
        saved: list[BusinessDataProjectionReceipt] = []

        def apply(state: BusinessDataSourceState) -> BusinessDataSourceState:
            existing = state.projection_receipts.get(receipt.canonical_event_id)
            if existing is not None:
                saved.append(existing)
                return state
            state.projection_receipts[receipt.canonical_event_id] = receipt
            if len(state.projection_receipts) > 20_000:
                ordered = sorted(
                    state.projection_receipts.values(),
                    key=lambda item: (item.projected_at, item.canonical_event_id),
                    reverse=True,
                )
                state.projection_receipts = {
                    item.canonical_event_id: item for item in ordered[:20_000]
                }
            saved.append(receipt)
            return state

        self.store.update(apply)
        return saved[0]

    def _binding(
        self,
        state: BusinessDataSourceState,
        source: BusinessDataSourceRecord,
        snapshot: BusinessDataSnapshot,
    ) -> BusinessEntityBinding | None:
        key = (
            source.organization_id,
            source.workspace_id,
            source.entity_key_namespace.casefold(),
            snapshot.entity_key.casefold(),
        )
        return next((item for item in state.bindings if item.key() == key), None)

    def _ensure_entity(
        self,
        source: BusinessDataSourceRecord,
        snapshot: BusinessDataSnapshot,
        *,
        actor: AuthenticationActor,
    ):
        state = self.store.load()
        binding = self._binding(state, source, snapshot)
        deterministic_id = self._entity_id(source, snapshot)
        if binding is not None:
            entity = self.business_context.get_entity(
                binding.business_entity_id,
                actor=actor,
            )
            if entity.entity_type != source.entity_type:
                raise BusinessDataSourceConflictError(
                    "business entity key is bound with a different entity type"
                )
            return entity
        try:
            entity = self.business_context.get_entity(
                deterministic_id,
                actor=actor,
            )
        except BusinessContextNotFoundError:
            try:
                entity = self.business_context.create_entity(
                    BusinessEntityCreate(
                        entity_type=source.entity_type,
                        name=snapshot.entity_name,
                        classification=source.classification,
                        field_provenance=(
                            BusinessFieldProvenance(
                                field_name="name",
                                provider=source.provider_id,
                                authority=source.entity_name_authority,
                                priority=source.entity_name_priority,
                                source_revision=snapshot.source_revision,
                                observed_at=snapshot.source_updated_at,
                            ),
                        ),
                    ),
                    actor=actor,
                    entity_id=deterministic_id,
                )
            except BusinessContextConflictError:
                entity = self.business_context.get_entity(
                    deterministic_id,
                    actor=actor,
                )
        if entity.entity_type != source.entity_type:
            raise BusinessDataSourceConflictError(
                "business entity key resolves to a different entity type"
            )

        candidate = BusinessEntityBinding(
            organization_id=source.organization_id,
            workspace_id=source.workspace_id,
            entity_key_namespace=source.entity_key_namespace,
            entity_key=snapshot.entity_key,
            business_entity_id=entity.id,
            created_by_source_id=source.id,
            created_at=float(self.clock()),
        )

        def bind(current: BusinessDataSourceState) -> BusinessDataSourceState:
            existing = self._binding(current, source, snapshot)
            if existing is None:
                current.bindings.append(candidate)
            elif existing.business_entity_id != entity.id:
                raise BusinessDataSourceConflictError(
                    "business entity key is already bound to a different entity"
                )
            return current

        self.store.update(bind)
        return entity

    @staticmethod
    def _incoming_is_stale(existing, snapshot: BusinessDataSnapshot) -> bool:
        if existing.source_sequence is not None:
            if snapshot.source_sequence is None:
                return True
            return snapshot.source_sequence <= existing.source_sequence
        if (
            existing.source_updated_at is not None
            and snapshot.source_updated_at is None
        ):
            return True
        if (
            existing.source_updated_at is not None
            and snapshot.source_updated_at is not None
        ):
            if snapshot.source_updated_at < existing.source_updated_at:
                return True
            if snapshot.source_updated_at == existing.source_updated_at:
                return (
                    snapshot.source_revision == existing.source_revision
                    or snapshot.source_revision is None
                    or existing.source_revision is None
                )
        return False

    def _should_update_entity_name(
        self,
        entity,
        source: BusinessDataSourceRecord,
        snapshot: BusinessDataSnapshot,
    ) -> bool:
        current = next(
            (
                item
                for item in entity.field_provenance
                if item.field_name.casefold() == "name"
            ),
            None,
        )
        if current is None:
            return True
        incoming_rank = FACT_AUTHORITY_RANK[source.entity_name_authority]
        current_rank = FACT_AUTHORITY_RANK[current.authority]
        if incoming_rank != current_rank:
            return incoming_rank > current_rank
        if source.entity_name_priority != current.priority:
            return source.entity_name_priority > current.priority
        incoming_at = snapshot.source_updated_at or 0.0
        current_at = current.observed_at or 0.0
        return incoming_at >= current_at

    def _project_snapshot(
        self,
        source: BusinessDataSourceRecord,
        snapshot: BusinessDataSnapshot,
        *,
        actor: AuthenticationActor,
        canonical_event_id: str,
    ) -> BusinessDataProjectionReceipt:
        existing_receipt = self.store.load().projection_receipts.get(
            canonical_event_id
        )
        if existing_receipt is not None:
            return existing_receipt

        entity = self._ensure_entity(source, snapshot, actor=actor)
        external = self.business_context.find_external_record(
            actor=actor,
            system=source.source_type,
            provider_instance=source.source_instance,
            object_type=snapshot.object_type,
            external_id=snapshot.external_id,
        )
        if external is not None and self._incoming_is_stale(external, snapshot):
            return self._record_receipt(
                BusinessDataProjectionReceipt(
                    canonical_event_id=canonical_event_id,
                    source_id=source.id,
                    external_record_ref_id=external.id,
                    business_entity_id=entity.id,
                    outcome="stale",
                    reason="provider snapshot is not newer than canonical source position",
                    projected_at=float(self.clock()),
                )
            )

        if external is None:
            external = self.business_context.create_external_record(
                ExternalRecordRefCreate(
                    system=source.source_type,
                    provider=source.provider_id,
                    provider_instance=source.source_instance,
                    object_type=snapshot.object_type,
                    external_id=snapshot.external_id,
                    display_name=snapshot.display_name or snapshot.entity_name,
                    external_url=snapshot.external_url,
                    business_entity_ids=(entity.id,),
                    classification=source.classification,
                    source_updated_at=snapshot.source_updated_at,
                    source_sequence=snapshot.source_sequence,
                    source_revision=snapshot.source_revision,
                    synced_at=float(self.clock()),
                ),
                actor=actor,
            )
        else:
            external = self.business_context.update_external_record(
                external.id,
                ExternalRecordRefUpdate(
                    display_name=snapshot.display_name or snapshot.entity_name,
                    external_url=snapshot.external_url,
                    business_entity_ids=tuple(
                        dict.fromkeys((*external.business_entity_ids, entity.id))
                    ),
                    lifecycle=(
                        ExternalRecordLifecycle.REVOKED
                        if snapshot.tombstone
                        else ExternalRecordLifecycle.ACTIVE
                    ),
                    source_updated_at=snapshot.source_updated_at,
                    source_sequence=snapshot.source_sequence,
                    source_revision=snapshot.source_revision,
                    synced_at=float(self.clock()),
                ),
                actor=actor,
            )

        if snapshot.tombstone:
            if external.lifecycle != ExternalRecordLifecycle.REVOKED:
                external = self.business_context.update_external_record(
                    external.id,
                    ExternalRecordRefUpdate(
                        lifecycle=ExternalRecordLifecycle.REVOKED,
                        source_updated_at=snapshot.source_updated_at,
                        source_sequence=snapshot.source_sequence,
                        source_revision=snapshot.source_revision,
                        synced_at=float(self.clock()),
                    ),
                    actor=actor,
                )
            return self._record_receipt(
                BusinessDataProjectionReceipt(
                    canonical_event_id=canonical_event_id,
                    source_id=source.id,
                    external_record_ref_id=external.id,
                    business_entity_id=entity.id,
                    outcome="tombstone",
                    projected_at=float(self.clock()),
                )
            )

        entity = self.business_context.get_entity(entity.id, actor=actor)
        current_name_provenance = next(
            (
                item
                for item in entity.field_provenance
                if item.field_name.casefold() == "name"
            ),
            None,
        )
        if (
            entity.lifecycle == BusinessEntityLifecycle.ACTIVE
            and self._should_update_entity_name(entity, source, snapshot)
            and (
                entity.name != snapshot.entity_name
                or current_name_provenance is None
                or current_name_provenance.external_record_ref_id != external.id
            )
        ):
            other = tuple(
                item
                for item in entity.field_provenance
                if item.field_name.casefold() != "name"
            )
            entity = self.business_context.update_entity(
                entity.id,
                BusinessEntityUpdate(
                    name=snapshot.entity_name,
                    field_provenance=(
                        *other,
                        BusinessFieldProvenance(
                            field_name="name",
                            external_record_ref_id=external.id,
                            provider=source.provider_id,
                            authority=source.entity_name_authority,
                            priority=source.entity_name_priority,
                            source_revision=snapshot.source_revision,
                            observed_at=snapshot.source_updated_at,
                        ),
                    ),
                ),
                actor=actor,
            )

        values = {
            item.source_field.casefold(): item.value
            for item in snapshot.fields
        }
        fact_ids: list[str] = []
        for mapping in source.field_mappings:
            value = values.get(mapping.source_field.casefold())
            if value is None:
                continue
            payload = CompanyFactCreate(
                business_entity_id=entity.id,
                key=mapping.fact_key,
                value_type=mapping.value_type,
                value=value,
                unit=mapping.unit,
                source=CompanyFactSource(
                    source=f"{source.source_type}.{snapshot.object_type}.{mapping.source_field}",
                    provider=source.provider_id,
                    external_record_ref_id=external.id,
                    authority=mapping.authority,
                    priority=mapping.priority,
                    source_revision=snapshot.source_revision,
                    source_observed_at=snapshot.source_updated_at,
                ),
                quality=mapping.quality,
                confidence=mapping.confidence,
                observed_at=snapshot.source_updated_at,
                freshness_seconds=mapping.freshness_seconds,
                classification=mapping.classification,
            )
            source_facts = [
                item
                for item in self.business_context.list_facts(
                    actor=actor,
                    business_entity_id=entity.id,
                    key=mapping.fact_key,
                    include_inactive=False,
                    limit=500,
                )
                if item.source.external_record_ref_id == external.id
                and item.lifecycle == CompanyFactLifecycle.ACTIVE
            ]
            source_facts.sort(
                key=lambda item: (item.observed_at, item.id),
                reverse=True,
            )
            if source_facts:
                current = source_facts[0]
                if (
                    current.value == value
                    and current.unit == mapping.unit
                    and current.source.source_revision == snapshot.source_revision
                ):
                    continue
                fact = self.business_context.supersede_fact(
                    current.id,
                    payload,
                    actor=actor,
                )
            else:
                fact = self.business_context.create_fact(payload, actor=actor)
            fact_ids.append(fact.id)

        return self._record_receipt(
            BusinessDataProjectionReceipt(
                canonical_event_id=canonical_event_id,
                source_id=source.id,
                external_record_ref_id=external.id,
                business_entity_id=entity.id,
                fact_ids=tuple(fact_ids),
                outcome="projected",
                projected_at=float(self.clock()),
            )
        )

    async def ingest_snapshot(
        self,
        source: BusinessDataSourceRecord,
        snapshot: BusinessDataSnapshot,
        *,
        actor: AuthenticationActor,
        event_id: str | None = None,
        event_kind: BusinessDataEventKind | None = None,
        occurred_at: float | None = None,
    ) -> BusinessDataProjectionReceipt:
        kind = (
            event_kind
            or (
                BusinessDataEventKind.TOMBSTONE
                if snapshot.tombstone
                else BusinessDataEventKind.UPSERT
            )
        )
        idempotency_key = (
            f"provider-event:{event_id}"
            if event_id
            else self._snapshot_fingerprint(source.id, snapshot)
        )
        delivery = await self.canonical_events.ingest(
            event_type=CanonicalEventType.BUSINESS_DATA,
            source=f"business-data:{source.source_type}:{source.id}",
            idempotency_key=idempotency_key,
            occurred_at=occurred_at or snapshot.source_updated_at,
            tenant_id=source.organization_id,
            workspace_id=source.workspace_id,
            payload={
                "source_id": source.id,
                "event_kind": kind.value,
                "snapshot": self._snapshot_payload(snapshot),
            },
        )
        existing = self.store.load().projection_receipts.get(
            delivery.event.event_id
        )
        if not delivery.inserted and existing is not None:
            return existing.model_copy(
                update={
                    "outcome": "duplicate",
                    "reason": "canonical event was already projected",
                }
            )
        return self._project_snapshot(
            source,
            snapshot,
            actor=actor,
            canonical_event_id=delivery.event.event_id,
        )

    def _advance_page(
        self,
        source_id: str,
        page: BusinessDataPage,
        *,
        projected: int,
        duplicates: int,
        stale: int,
    ) -> BusinessDataSourceRecord:
        current = self.store.load().sources[source_id]
        next_cursor = (
            page.next_cursor
            if page.next_cursor is not None
            else page.checkpoint
            if page.checkpoint is not None
            else current.cursor
        )
        checkpoint = page.checkpoint or current.checkpoint
        return self._update_source(
            source_id,
            {
                "cursor": next_cursor,
                "checkpoint": checkpoint,
                "projected_records": current.projected_records + projected,
                "duplicate_events": current.duplicate_events + duplicates,
                "stale_events": current.stale_events + stale,
            },
        )

    def _mark_failure(
        self,
        source: BusinessDataSourceRecord,
        exc: Exception,
        *,
        actor: AuthenticationActor,
    ) -> None:
        now = float(self.clock())
        self._update_source(
            source.id,
            {
                "status": BusinessDataSourceStatus.DEGRADED,
                "last_error": f"{type(exc).__name__}: {exc}"[:1000],
                "last_error_at": now,
            },
        )
        if self.provider_capacity is None:
            return
        classified = self.provider_capacity.report_exception(
            source.provider_id,
            source.id,
            exc,
            actor=actor,
            source="business-data-sync",
        )
        if classified is None:
            self.provider_capacity.report(
                ProviderCapacityReport(
                    provider_id=source.provider_id,
                    runtime_id=source.id,
                    status=ProviderCapacityStatus.UNAVAILABLE,
                    reason=str(exc)[:500],
                    retry_at=now + float(
                        source.reconciliation_interval_seconds or 60
                    ),
                    source="business-data-sync",
                    observed_at=now,
                ),
                actor=actor,
            )

    async def sync(
        self,
        source_id: str,
        *,
        actor: AuthenticationActor,
        max_pages: int = MAX_PAGES_PER_RUN,
        full_resync: bool = False,
    ) -> BusinessDataSyncResult:
        if max_pages < 1 or max_pages > self.MAX_PAGES_PER_RUN:
            raise BusinessDataSourceValidationError(
                f"max_pages must be between 1 and {self.MAX_PAGES_PER_RUN}"
            )
        source = self.get(source_id, actor=actor)
        if source.status in {
            BusinessDataSourceStatus.PAUSED,
            BusinessDataSourceStatus.QUARANTINED,
        }:
            raise BusinessDataSourceUnavailableError(
                f"business data source is {source.status.value}"
            )
        if self.provider_capacity is not None:
            blocking = self.provider_capacity.blocking_record(
                source.provider_id,
                source.id,
                actor=actor,
            )
            if blocking is not None:
                raise BusinessDataSourceUnavailableError(
                    f"provider capacity blocks synchronization until {blocking.retry_at}"
                )

        adapter = self.registry.resolve(source, actor=actor, required=True)
        assert adapter is not None
        cursor_before = source.cursor
        cursor = None if full_resync else source.cursor
        pages = records_seen = projected = duplicates = stale = tombstones = 0
        exhausted = False
        self._update_source(
            source.id,
            {
                "last_sync_started_at": float(self.clock()),
                "last_error": None,
            },
        )
        try:
            while pages < max_pages:
                if adapter.capabilities.supports(
                    BusinessDataSourceCapability.INCREMENTAL_SYNC
                ) and not full_resync:
                    page = await adapter.sync_since(
                        scope=source.scope,
                        cursor=cursor,
                        limit=source.page_size,
                    )
                else:
                    adapter.capabilities.require(
                        BusinessDataSourceCapability.PAGED_DISCOVERY
                        if adapter.capabilities.supports(
                            BusinessDataSourceCapability.PAGED_DISCOVERY
                        )
                        else BusinessDataSourceCapability.DISCOVERY
                    )
                    page = await adapter.discover_page(
                        scope=source.scope,
                        cursor=cursor,
                        limit=source.page_size,
                    )
                pages += 1
                page_projected = page_duplicates = page_stale = 0
                for snapshot in page.items:
                    records_seen += 1
                    if snapshot.object_type.casefold() != source.object_type.casefold():
                        raise BusinessDataSourceValidationError(
                            "adapter returned an object type outside configured source"
                        )
                    receipt = await self.ingest_snapshot(
                        source,
                        snapshot,
                        actor=actor,
                    )
                    if receipt.outcome == "projected":
                        projected += 1
                        page_projected += 1
                    elif receipt.outcome == "stale":
                        stale += 1
                        page_stale += 1
                    elif receipt.outcome == "tombstone":
                        tombstones += 1
                        page_projected += 1
                    else:
                        duplicates += 1
                        page_duplicates += 1

                before_advance = cursor
                source = self._advance_page(
                    source.id,
                    page,
                    projected=page_projected,
                    duplicates=page_duplicates,
                    stale=page_stale,
                )
                cursor = source.cursor
                if not page.exhausted and cursor == before_advance:
                    raise BusinessDataSourceValidationError(
                        "non-exhausted page did not advance reconciliation cursor"
                    )
                exhausted = page.exhausted
                if exhausted:
                    break

            now = float(self.clock())
            source = self._update_source(
                source.id,
                {
                    "status": BusinessDataSourceStatus.ACTIVE,
                    "last_sync_completed_at": now,
                    "last_success_at": now,
                    "last_error": None,
                    "last_error_at": None,
                },
            )
            if self.provider_capacity is not None:
                self.provider_capacity.mark_available(
                    source.provider_id,
                    source.id,
                    actor=actor,
                    source="business-data-sync",
                )
            return BusinessDataSyncResult(
                source_id=source.id,
                pages=pages,
                records_seen=records_seen,
                projected=projected,
                duplicates=duplicates,
                stale=stale,
                tombstones=tombstones,
                cursor_before=cursor_before,
                cursor_after=source.cursor,
                checkpoint=source.checkpoint,
                exhausted=exhausted,
            )
        except Exception as exc:
            self._mark_failure(source, exc, actor=actor)
            raise

    async def ingest_provider_event(
        self,
        source_id: str,
        payload: object,
        *,
        actor: AuthenticationActor,
    ) -> BusinessDataProjectionReceipt | None:
        source = self.get(source_id, actor=actor)
        adapter = self.registry.resolve(source, actor=actor, required=True)
        assert adapter is not None
        adapter.capabilities.require(BusinessDataSourceCapability.EVENTS)
        event = await adapter.normalize_event(payload)
        if event is None:
            return None
        receipt = await self.ingest_snapshot(
            source,
            event.snapshot,
            actor=actor,
            event_id=event.event_id,
            event_kind=event.event_kind,
            occurred_at=event.occurred_at,
        )
        # Webhook/event delivery does not advance the durable reconciliation
        # cursor. Opaque provider cursors cannot be safely ordered across
        # out-of-order event delivery; scheduled/manual reconciliation owns
        # cursor advancement after a fully projected page.
        return receipt

    async def handle_canonical_event(
        self,
        event: CanonicalEventEnvelope,
    ) -> None:
        if event.event_type == CanonicalEventType.BUSINESS_DATA.value:
            source_id = str(event.payload.get("source_id") or "").strip()
            raw_snapshot = event.payload.get("snapshot")
            if not source_id or not isinstance(raw_snapshot, dict):
                return
            state = self.store.load()
            source = state.sources.get(source_id)
            if source is None:
                return
            actor = self._system_actor(source)
            snapshot = self._snapshot_from_payload(raw_snapshot)
            self._project_snapshot(
                source,
                snapshot,
                actor=actor,
                canonical_event_id=event.event_id,
            )
            return

        if event.event_type != CanonicalEventType.SCHEDULE.value:
            return
        if event.payload.get("trigger_type") != self.SCHEDULE_TRIGGER:
            return
        nested = event.payload.get("payload")
        if not isinstance(nested, dict):
            return
        source_id = str(nested.get("source_id") or "").strip()
        if not source_id:
            return
        source = self.store.load().sources.get(source_id)
        if source is None or source.status in {
            BusinessDataSourceStatus.PAUSED,
            BusinessDataSourceStatus.QUARANTINED,
        }:
            return
        actor = self._system_actor(source)
        try:
            await self.sync(source_id, actor=actor)
        except BusinessDataSourceUnavailableError:
            return
        except Exception:
            return

    def drift(
        self,
        source_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[dict[str, object], ...]:
        source = self.get(source_id, actor=actor)
        rows: list[dict[str, object]] = []
        for binding in self.store.load().bindings:
            if (
                binding.organization_id != source.organization_id
                or binding.workspace_id != source.workspace_id
                or binding.entity_key_namespace.casefold()
                != source.entity_key_namespace.casefold()
            ):
                continue
            entity = self.business_context.get_entity(
                binding.business_entity_id,
                actor=actor,
            )
            source_refs = [
                item
                for item in self.business_context.list_external_records(
                    actor=actor,
                    business_entity_id=entity.id,
                    include_inactive=True,
                    limit=500,
                )
                if item.system.casefold() == source.source_type.casefold()
                and (item.provider_instance or "").rstrip("/")
                == source.source_instance.rstrip("/")
            ]
            for external in source_refs:
                conflicts: list[dict[str, object]] = []
                for mapping in source.field_mappings:
                    resolved = self.business_context.resolve_fact(
                        entity.id,
                        mapping.fact_key,
                        actor=actor,
                    )
                    if resolved.conflict:
                        conflicts.append(
                            {
                                "fact_key": mapping.fact_key,
                                "selected_fact_id": (
                                    resolved.selected.id
                                    if resolved.selected is not None
                                    else None
                                ),
                                "conflict_fact_ids": resolved.conflict_fact_ids,
                            }
                        )
                rows.append(
                    {
                        "business_entity_id": entity.id,
                        "entity_name": entity.name,
                        "external_record_ref_id": external.id,
                        "external_id": external.external_id,
                        "source_lifecycle": external.lifecycle.value,
                        "source_updated_at": external.source_updated_at,
                        "source_sequence": external.source_sequence,
                        "source_revision": external.source_revision,
                        "conflicts": conflicts,
                    }
                )
        rows.sort(
            key=lambda item: (
                str(item["entity_name"]).casefold(),
                str(item["external_id"]),
            )
        )
        return tuple(rows)
