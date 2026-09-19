from __future__ import annotations

import time
from typing import Any

from codex_web.business_context import (
    FACT_AUTHORITY_RANK,
    FACT_QUALITY_RANK,
    BusinessContextState,
    BusinessEntity,
    BusinessEntityCreate,
    BusinessEntityLifecycle,
    BusinessEntityRelationship,
    BusinessEntityRelationshipCreate,
    BusinessEntityUpdate,
    CompanyFact,
    CompanyFactCreate,
    CompanyFactLifecycle,
    ExternalRecordLifecycle,
    ExternalRecordRef,
    ExternalRecordRefCreate,
    ExternalRecordRefUpdate,
    FactFreshness,
    FactResolution,
    fact_value_fingerprint,
)
from codex_web.data_governance import (
    DataCategory,
    GovernedDataCreate,
    GovernedDataRecord,
    GovernanceAction,
)
from codex_web.identity import AuthenticationActor, TenantScope
from codex_web.services.data_governance import DataGovernanceService
from codex_web.storage.business_context import BusinessContextStore


class BusinessContextError(RuntimeError):
    pass


class BusinessContextNotFoundError(BusinessContextError):
    pass


class BusinessContextConflictError(BusinessContextError):
    pass


class BusinessContextValidationError(BusinessContextError):
    pass


class BusinessContextService:
    """Tenant-scoped business references and selected governed company facts.

    This service intentionally stores lightweight canonical references and
    selected bounded facts, never wholesale provider payloads.
    """

    MAX_LIST = 500

    def __init__(
        self,
        store: BusinessContextStore,
        *,
        governance: DataGovernanceService | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.governance = governance
        self.clock = clock

    @staticmethod
    def _visible(item: Any, scope: TenantScope) -> bool:
        return (
            item.organization_id == scope.organization_id
            and item.workspace_id == scope.workspace_id
        )

    @staticmethod
    def _scope(actor: AuthenticationActor) -> TenantScope:
        return actor.tenant

    def _entity(
        self,
        state: BusinessContextState,
        entity_id: str,
        scope: TenantScope,
    ) -> BusinessEntity:
        item = next(
            (
                row
                for row in state.entities
                if row.id == entity_id and self._visible(row, scope)
            ),
            None,
        )
        if item is None:
            raise BusinessContextNotFoundError("business entity not found")
        return item

    def _external(
        self,
        state: BusinessContextState,
        ref_id: str,
        scope: TenantScope,
    ) -> ExternalRecordRef:
        item = next(
            (
                row
                for row in state.external_records
                if row.id == ref_id and self._visible(row, scope)
            ),
            None,
        )
        if item is None:
            raise BusinessContextNotFoundError("external record reference not found")
        return item

    def _fact(
        self,
        state: BusinessContextState,
        fact_id: str,
        scope: TenantScope,
    ) -> CompanyFact:
        item = next(
            (
                row
                for row in state.facts
                if row.id == fact_id and self._visible(row, scope)
            ),
            None,
        )
        if item is None:
            raise BusinessContextNotFoundError("company fact not found")
        return item

    @staticmethod
    def _project_for_entity(entity: BusinessEntity) -> str | None:
        return (
            entity.links.project_ids[0]
            if len(entity.links.project_ids) == 1
            else None
        )

    def _governance_register(
        self,
        *,
        object_type: str,
        object_id: str,
        classification,
        actor: AuthenticationActor,
        project_id: str | None = None,
        retention_policy_ref: str | None = None,
        retention_expires_at: float | None = None,
        source_record_ids: tuple[str, ...] = (),
        reason: str,
    ) -> GovernedDataRecord | None:
        if self.governance is None:
            return None
        record = self.governance.register_domain_record(
            GovernedDataCreate(
                project_id=project_id,
                object_type=object_type,
                object_id=object_id,
                category=DataCategory.OTHER,
                classification=classification,
                retention_policy_ref=retention_policy_ref,
                retention_expires_at=retention_expires_at,
                source_record_ids=source_record_ids,
                reason=reason,
            ),
            actor=actor,
        )
        return record

    def _remove_object(self, object_type: str, object_id: str) -> None:
        def apply(state: BusinessContextState) -> BusinessContextState:
            if object_type == "business_entity":
                state.entities = [item for item in state.entities if item.id != object_id]
                state.relationships = [
                    item
                    for item in state.relationships
                    if item.from_entity_id != object_id
                    and item.to_entity_id != object_id
                ]
            elif object_type == "external_record_ref":
                state.external_records = [
                    item for item in state.external_records if item.id != object_id
                ]
            elif object_type == "company_fact":
                state.facts = [item for item in state.facts if item.id != object_id]
            return state

        self.store.update(apply)

    def _attach_governance(
        self,
        object_type: str,
        object_id: str,
        governance_record: GovernedDataRecord | None,
    ) -> None:
        if governance_record is None:
            return

        update = {
            "governance_record_id": governance_record.id,
            "classification": governance_record.classification,
        }

        def apply(state: BusinessContextState) -> BusinessContextState:
            if object_type == "business_entity":
                state.entities = [
                    item.model_copy(update=update)
                    if item.id == object_id
                    else item
                    for item in state.entities
                ]
            elif object_type == "external_record_ref":
                state.external_records = [
                    item.model_copy(update=update)
                    if item.id == object_id
                    else item
                    for item in state.external_records
                ]
            elif object_type == "company_fact":
                state.facts = [
                    item.model_copy(update=update)
                    if item.id == object_id
                    else item
                    for item in state.facts
                ]
            return state

        self.store.update(apply)

    def create_entity(
        self,
        payload: BusinessEntityCreate,
        *,
        actor: AuthenticationActor,
        entity_id: str | None = None,
    ) -> BusinessEntity:
        now = float(self.clock())
        scope = self._scope(actor)
        state = self.store.load()
        source_governance_ids: list[str] = []
        for provenance in payload.field_provenance:
            if provenance.external_record_ref_id:
                ref = self._external(
                    state,
                    provenance.external_record_ref_id,
                    scope,
                )
                if ref.governance_record_id:
                    source_governance_ids.append(ref.governance_record_id)

        item = BusinessEntity(
            **({"id": entity_id} if entity_id is not None else {}),
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            entity_type=payload.entity_type,
            name=payload.name,
            description=payload.description,
            classification=payload.classification,
            links=payload.links,
            field_provenance=payload.field_provenance,
            created_by=actor.identity_id,
            created_at=now,
            updated_at=now,
        )

        def apply(current: BusinessContextState) -> BusinessContextState:
            existing = next(
                (
                    row
                    for row in current.entities
                    if row.id == item.id
                    and self._visible(row, scope)
                ),
                None,
            )
            if existing is not None:
                raise BusinessContextConflictError(
                    "business entity id already exists in workspace"
                )
            current.entities.append(item)
            return current

        self.store.update(apply)
        try:
            governance_record = self._governance_register(
                object_type="business_entity",
                object_id=item.id,
                classification=item.classification,
                actor=actor,
                project_id=self._project_for_entity(item),
                retention_policy_ref=payload.retention_policy_ref,
                retention_expires_at=payload.retention_expires_at,
                source_record_ids=tuple(dict.fromkeys(source_governance_ids)),
                reason="business entity created",
            )
        except Exception:
            self._remove_object("business_entity", item.id)
            raise
        self._attach_governance("business_entity", item.id, governance_record)
        return self.get_entity(item.id, actor=actor)

    def get_entity(
        self,
        entity_id: str,
        *,
        actor: AuthenticationActor,
    ) -> BusinessEntity:
        return self._entity(self.store.load(), entity_id, self._scope(actor))

    def list_entities(
        self,
        *,
        actor: AuthenticationActor,
        entity_type=None,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> tuple[BusinessEntity, ...]:
        if limit < 1 or limit > self.MAX_LIST:
            raise BusinessContextValidationError(
                f"limit must be between 1 and {self.MAX_LIST}"
            )
        scope = self._scope(actor)
        rows = [
            item
            for item in self.store.load().entities
            if self._visible(item, scope)
        ]
        if entity_type is not None:
            rows = [item for item in rows if item.entity_type == entity_type]
        if not include_inactive:
            rows = [
                item
                for item in rows
                if item.lifecycle == BusinessEntityLifecycle.ACTIVE
            ]
        rows.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        return tuple(rows[:limit])

    def update_entity(
        self,
        entity_id: str,
        payload: BusinessEntityUpdate,
        *,
        actor: AuthenticationActor,
    ) -> BusinessEntity:
        scope = self._scope(actor)
        updated: list[BusinessEntity] = []

        def apply(state: BusinessContextState) -> BusinessContextState:
            current = self._entity(state, entity_id, scope)
            changes = payload.model_dump(mode="python", exclude_unset=True)
            if not changes:
                raise BusinessContextConflictError(
                    "business entity update contains no changes"
                )
            requested_lifecycle = changes.get("lifecycle")
            if requested_lifecycle in {
                BusinessEntityLifecycle.REDACTED,
                BusinessEntityLifecycle.ANONYMIZED,
                BusinessEntityLifecycle.DELETED,
            }:
                raise BusinessContextConflictError(
                    "redact/anonymize/delete business entity through DataGovernance"
                )
            changes["updated_at"] = float(self.clock())
            replacement = current.model_copy(update=changes)
            state.entities = [
                replacement if item.id == current.id else item
                for item in state.entities
            ]
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def create_external_record(
        self,
        payload: ExternalRecordRefCreate,
        *,
        actor: AuthenticationActor,
    ) -> ExternalRecordRef:
        now = float(self.clock())
        scope = self._scope(actor)
        state = self.store.load()
        for entity_id in payload.business_entity_ids:
            self._entity(state, entity_id, scope)

        item = ExternalRecordRef(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            system=payload.system,
            provider=payload.provider,
            provider_instance=payload.provider_instance,
            object_type=payload.object_type,
            external_id=payload.external_id,
            display_name=payload.display_name,
            external_url=payload.external_url,
            business_entity_ids=payload.business_entity_ids,
            project_ids=payload.project_ids,
            resource_ids=payload.resource_ids,
            classification=payload.classification,
            source_updated_at=payload.source_updated_at,
            first_seen_at=now,
            synced_at=payload.synced_at or now,
            created_by=actor.identity_id,
        )
        if any(
            existing.stable_key() == item.stable_key()
            for existing in state.external_records
        ):
            raise BusinessContextConflictError(
                "external record identity already exists in workspace"
            )

        def apply(current: BusinessContextState) -> BusinessContextState:
            if any(
                existing.stable_key() == item.stable_key()
                for existing in current.external_records
            ):
                raise BusinessContextConflictError(
                    "external record identity already exists in workspace"
                )
            current.external_records.append(item)
            return current

        self.store.update(apply)
        source_ids = tuple(
            dict.fromkeys(
                entity.governance_record_id
                for entity_id in payload.business_entity_ids
                for entity in (self._entity(self.store.load(), entity_id, scope),)
                if entity.governance_record_id
            )
        )
        try:
            governance_record = self._governance_register(
                object_type="external_record_ref",
                object_id=item.id,
                classification=item.classification,
                actor=actor,
                project_id=(
                    payload.project_ids[0]
                    if len(payload.project_ids) == 1
                    else None
                ),
                retention_policy_ref=payload.retention_policy_ref,
                retention_expires_at=payload.retention_expires_at,
                source_record_ids=source_ids,
                reason="external business record reference created",
            )
        except Exception:
            self._remove_object("external_record_ref", item.id)
            raise
        self._attach_governance("external_record_ref", item.id, governance_record)
        return self.get_external_record(item.id, actor=actor)

    def get_external_record(
        self,
        ref_id: str,
        *,
        actor: AuthenticationActor,
    ) -> ExternalRecordRef:
        return self._external(self.store.load(), ref_id, self._scope(actor))

    def list_external_records(
        self,
        *,
        actor: AuthenticationActor,
        business_entity_id: str | None = None,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> tuple[ExternalRecordRef, ...]:
        if limit < 1 or limit > self.MAX_LIST:
            raise BusinessContextValidationError(
                f"limit must be between 1 and {self.MAX_LIST}"
            )
        scope = self._scope(actor)
        rows = [
            item
            for item in self.store.load().external_records
            if self._visible(item, scope)
        ]
        if business_entity_id is not None:
            self._entity(self.store.load(), business_entity_id, scope)
            rows = [
                item
                for item in rows
                if business_entity_id in item.business_entity_ids
            ]
        if not include_inactive:
            rows = [
                item
                for item in rows
                if item.lifecycle == ExternalRecordLifecycle.ACTIVE
            ]
        rows.sort(key=lambda item: (item.synced_at, item.id), reverse=True)
        return tuple(rows[:limit])

    def update_external_record(
        self,
        ref_id: str,
        payload: ExternalRecordRefUpdate,
        *,
        actor: AuthenticationActor,
    ) -> ExternalRecordRef:
        scope = self._scope(actor)
        updated: list[ExternalRecordRef] = []

        def apply(state: BusinessContextState) -> BusinessContextState:
            current = self._external(state, ref_id, scope)
            changes = payload.model_dump(mode="python", exclude_unset=True)
            if not changes:
                raise BusinessContextConflictError(
                    "external record update contains no changes"
                )
            for entity_id in changes.get(
                "business_entity_ids",
                current.business_entity_ids,
            ):
                self._entity(state, entity_id, scope)
            if "business_entity_ids" in changes:
                changes["business_entity_ids"] = tuple(
                    dict.fromkeys(changes["business_entity_ids"])
                )
            if "project_ids" in changes:
                changes["project_ids"] = tuple(
                    dict.fromkeys(changes["project_ids"])
                )
            if "resource_ids" in changes:
                changes["resource_ids"] = tuple(
                    dict.fromkeys(changes["resource_ids"])
                )
            lifecycle = changes.get("lifecycle")
            if lifecycle in {
                ExternalRecordLifecycle.REDACTED,
                ExternalRecordLifecycle.ANONYMIZED,
                ExternalRecordLifecycle.DELETED,
            }:
                raise BusinessContextConflictError(
                    "redact/anonymize/delete external record through DataGovernance"
                )
            if lifecycle == ExternalRecordLifecycle.REVOKED:
                changes["revoked_at"] = float(self.clock())
            elif lifecycle == ExternalRecordLifecycle.ACTIVE:
                changes["revoked_at"] = None
            replacement = current.model_copy(update=changes)
            state.external_records = [
                replacement if item.id == current.id else item
                for item in state.external_records
            ]
            updated.append(replacement)
            return state

        self.store.update(apply)
        return updated[0]

    def _build_fact(
        self,
        payload: CompanyFactCreate,
        *,
        actor: AuthenticationActor,
        supersedes_fact_id: str | None = None,
    ) -> tuple[CompanyFact, tuple[str, ...]]:
        scope = self._scope(actor)
        state = self.store.load()
        entity = self._entity(state, payload.business_entity_id, scope)
        if entity.lifecycle in {
            BusinessEntityLifecycle.REDACTED,
            BusinessEntityLifecycle.ANONYMIZED,
            BusinessEntityLifecycle.DELETED,
        }:
            raise BusinessContextConflictError(
                "cannot create facts for an unavailable business entity"
            )

        source_governance: list[str] = []
        if entity.governance_record_id:
            source_governance.append(entity.governance_record_id)
        if payload.source.external_record_ref_id:
            ref = self._external(
                state,
                payload.source.external_record_ref_id,
                scope,
            )
            if ref.lifecycle != ExternalRecordLifecycle.ACTIVE:
                raise BusinessContextConflictError(
                    "company fact source external record is not active"
                )
            if (
                ref.business_entity_ids
                and entity.id not in ref.business_entity_ids
            ):
                raise BusinessContextValidationError(
                    "external record source is not linked to business entity"
                )
            if ref.governance_record_id:
                source_governance.append(ref.governance_record_id)

        now = float(self.clock())
        observed_at = (
            payload.observed_at
            if payload.observed_at is not None
            else payload.source.source_observed_at
            if payload.source.source_observed_at is not None
            else now
        )
        item = CompanyFact(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            business_entity_id=payload.business_entity_id,
            key=payload.key,
            value_type=payload.value_type,
            value=payload.value,
            unit=payload.unit,
            source=payload.source,
            quality=payload.quality,
            confidence=payload.confidence,
            effective_from=payload.effective_from,
            effective_to=payload.effective_to,
            observed_at=observed_at,
            freshness_seconds=payload.freshness_seconds,
            classification=payload.classification,
            evidence_ids=payload.evidence_ids,
            supersedes_fact_id=supersedes_fact_id,
            created_by=actor.identity_id,
            created_at=now,
        )
        return item, tuple(dict.fromkeys(source_governance))

    def _register_fact_governance(
        self,
        item: CompanyFact,
        payload: CompanyFactCreate,
        source_governance: tuple[str, ...],
        *,
        actor: AuthenticationActor,
    ) -> None:
        try:
            governance_record = self._governance_register(
                object_type="company_fact",
                object_id=item.id,
                classification=item.classification,
                actor=actor,
                retention_policy_ref=payload.retention_policy_ref,
                retention_expires_at=payload.retention_expires_at,
                source_record_ids=source_governance,
                reason="company fact created",
            )
        except Exception:
            self._remove_object("company_fact", item.id)
            raise
        self._attach_governance("company_fact", item.id, governance_record)

    def create_fact(
        self,
        payload: CompanyFactCreate,
        *,
        actor: AuthenticationActor,
    ) -> CompanyFact:
        item, source_governance = self._build_fact(payload, actor=actor)

        def apply(state: BusinessContextState) -> BusinessContextState:
            state.facts.append(item)
            return state

        self.store.update(apply)
        self._register_fact_governance(
            item,
            payload,
            source_governance,
            actor=actor,
        )
        return self.get_fact(item.id, actor=actor)

    def supersede_fact(
        self,
        fact_id: str,
        payload: CompanyFactCreate,
        *,
        actor: AuthenticationActor,
    ) -> CompanyFact:
        scope = self._scope(actor)
        state = self.store.load()
        current = self._fact(state, fact_id, scope)
        if current.lifecycle != CompanyFactLifecycle.ACTIVE:
            raise BusinessContextConflictError(
                "only an active company fact can be superseded"
            )
        if (
            payload.business_entity_id != current.business_entity_id
            or payload.key.casefold() != current.key.casefold()
        ):
            raise BusinessContextValidationError(
                "replacement must keep the same business entity and fact key"
            )
        replacement, source_governance = self._build_fact(
            payload,
            actor=actor,
            supersedes_fact_id=current.id,
        )

        def apply(rows: BusinessContextState) -> BusinessContextState:
            selected = self._fact(rows, fact_id, scope)
            if selected.lifecycle != CompanyFactLifecycle.ACTIVE:
                raise BusinessContextConflictError(
                    "company fact was concurrently superseded"
                )
            old = selected.model_copy(
                update={
                    "lifecycle": CompanyFactLifecycle.SUPERSEDED,
                    "superseded_by_fact_id": replacement.id,
                }
            )
            rows.facts = [
                old if item.id == selected.id else item
                for item in rows.facts
            ]
            rows.facts.append(replacement)
            return rows

        self.store.update(apply)
        try:
            self._register_fact_governance(
                replacement,
                payload,
                source_governance,
                actor=actor,
            )
        except Exception:
            def rollback(rows: BusinessContextState) -> BusinessContextState:
                rows.facts = [
                    item for item in rows.facts if item.id != replacement.id
                ]
                rows.facts = [
                    item.model_copy(
                        update={
                            "lifecycle": CompanyFactLifecycle.ACTIVE,
                            "superseded_by_fact_id": None,
                        }
                    )
                    if item.id == current.id
                    else item
                    for item in rows.facts
                ]
                return rows

            self.store.update(rollback)
            raise
        return self.get_fact(replacement.id, actor=actor)

    def get_fact(
        self,
        fact_id: str,
        *,
        actor: AuthenticationActor,
    ) -> CompanyFact:
        return self._fact(self.store.load(), fact_id, self._scope(actor))

    def list_facts(
        self,
        *,
        actor: AuthenticationActor,
        business_entity_id: str | None = None,
        key: str | None = None,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> tuple[CompanyFact, ...]:
        if limit < 1 or limit > self.MAX_LIST:
            raise BusinessContextValidationError(
                f"limit must be between 1 and {self.MAX_LIST}"
            )
        scope = self._scope(actor)
        state = self.store.load()
        if business_entity_id is not None:
            self._entity(state, business_entity_id, scope)
        rows = [
            item
            for item in state.facts
            if self._visible(item, scope)
            and (
                business_entity_id is None
                or item.business_entity_id == business_entity_id
            )
            and (key is None or item.key.casefold() == key.casefold())
        ]
        if not include_inactive:
            rows = [
                item
                for item in rows
                if item.lifecycle == CompanyFactLifecycle.ACTIVE
            ]
        rows.sort(key=lambda item: (item.observed_at, item.id), reverse=True)
        return tuple(rows[:limit])

    @staticmethod
    def _fact_sort_key(item: CompanyFact) -> tuple[int, int, int, float, str]:
        return (
            FACT_AUTHORITY_RANK[item.source.authority],
            item.source.priority,
            FACT_QUALITY_RANK[item.quality],
            item.observed_at,
            item.id,
        )

    def resolve_fact(
        self,
        business_entity_id: str,
        key: str,
        *,
        actor: AuthenticationActor,
        at: float | None = None,
    ) -> FactResolution:
        scope = self._scope(actor)
        state = self.store.load()
        self._entity(state, business_entity_id, scope)
        now = float(self.clock()) if at is None else float(at)
        rows = [
            item
            for item in state.facts
            if self._visible(item, scope)
            and item.business_entity_id == business_entity_id
            and item.key.casefold() == key.casefold()
            and item.lifecycle == CompanyFactLifecycle.ACTIVE
            and (item.effective_from is None or item.effective_from <= now)
            and (item.effective_to is None or item.effective_to >= now)
        ]
        rows.sort(key=self._fact_sort_key, reverse=True)
        candidate_ids = tuple(item.id for item in rows)

        revoked_source: list[CompanyFact] = []
        usable: list[CompanyFact] = []
        external_by_id = {
            item.id: item
            for item in state.external_records
            if self._visible(item, scope)
        }
        for item in rows:
            ref_id = item.source.external_record_ref_id
            if ref_id is not None:
                ref = external_by_id.get(ref_id)
                if (
                    ref is None
                    or ref.lifecycle != ExternalRecordLifecycle.ACTIVE
                ):
                    revoked_source.append(item)
                    continue
            usable.append(item)

        stale = [
            item
            for item in usable
            if item.freshness_seconds is not None
            and now - item.observed_at > item.freshness_seconds
        ]
        stale_ids = {item.id for item in stale}
        fresh = [item for item in usable if item.id not in stale_ids]
        fresh.sort(key=self._fact_sort_key, reverse=True)

        if fresh:
            selected = fresh[0]
            fingerprints = {
                fact_value_fingerprint(item.value)
                for item in fresh
            }
            conflict = len(fingerprints) > 1
            conflict_ids = (
                tuple(item.id for item in fresh)
                if conflict
                else ()
            )
            return FactResolution(
                business_entity_id=business_entity_id,
                key=key,
                selected=selected,
                freshness=FactFreshness.FRESH,
                conflict=conflict,
                conflict_fact_ids=conflict_ids,
                stale_fact_ids=tuple(item.id for item in stale),
                revoked_source_fact_ids=tuple(
                    item.id for item in revoked_source
                ),
                candidate_fact_ids=candidate_ids,
                reason=(
                    "selected deterministically by source authority, priority, "
                    "quality, observation time and fact id; conflicting values "
                    "remain inspectable"
                    if conflict
                    else "selected highest-ranked fresh active fact"
                ),
                resolved_at=now,
            )
        if stale:
            return FactResolution(
                business_entity_id=business_entity_id,
                key=key,
                freshness=FactFreshness.STALE,
                stale_fact_ids=tuple(item.id for item in stale),
                revoked_source_fact_ids=tuple(
                    item.id for item in revoked_source
                ),
                candidate_fact_ids=candidate_ids,
                reason="only stale company facts are available",
                resolved_at=now,
            )
        if revoked_source:
            return FactResolution(
                business_entity_id=business_entity_id,
                key=key,
                freshness=FactFreshness.SOURCE_REVOKED,
                revoked_source_fact_ids=tuple(
                    item.id for item in revoked_source
                ),
                candidate_fact_ids=candidate_ids,
                reason="all matching facts depend on revoked/deleted source records",
                resolved_at=now,
            )
        return FactResolution(
            business_entity_id=business_entity_id,
            key=key,
            freshness=FactFreshness.MISSING,
            candidate_fact_ids=candidate_ids,
            reason="no active fact is effective at the requested time",
            resolved_at=now,
        )

    def create_relationship(
        self,
        payload: BusinessEntityRelationshipCreate,
        *,
        actor: AuthenticationActor,
    ) -> BusinessEntityRelationship:
        if payload.from_entity_id == payload.to_entity_id:
            raise BusinessContextValidationError(
                "business entity relationship cannot target itself"
            )
        scope = self._scope(actor)
        state = self.store.load()
        self._entity(state, payload.from_entity_id, scope)
        self._entity(state, payload.to_entity_id, scope)
        item = BusinessEntityRelationship(
            organization_id=scope.organization_id,
            workspace_id=scope.workspace_id,
            from_entity_id=payload.from_entity_id,
            to_entity_id=payload.to_entity_id,
            relationship_type=payload.relationship_type,
            created_by=actor.identity_id,
        )

        def apply(current: BusinessContextState) -> BusinessContextState:
            duplicate = next(
                (
                    row
                    for row in current.relationships
                    if self._visible(row, scope)
                    and row.from_entity_id == item.from_entity_id
                    and row.to_entity_id == item.to_entity_id
                    and row.relationship_type == item.relationship_type
                ),
                None,
            )
            if duplicate is not None:
                raise BusinessContextConflictError(
                    "business entity relationship already exists"
                )
            current.relationships.append(item)
            return current

        self.store.update(apply)
        return item

    def relationships(
        self,
        entity_id: str,
        *,
        actor: AuthenticationActor,
    ) -> tuple[BusinessEntityRelationship, ...]:
        scope = self._scope(actor)
        state = self.store.load()
        self._entity(state, entity_id, scope)
        rows = [
            item
            for item in state.relationships
            if self._visible(item, scope)
            and (
                item.from_entity_id == entity_id
                or item.to_entity_id == entity_id
            )
        ]
        rows.sort(key=lambda item: (item.created_at, item.id))
        return tuple(rows)

    def governance_action_handler(
        self,
        record: GovernedDataRecord,
        action: GovernanceAction,
    ) -> str | None:
        marker = {
            GovernanceAction.REDACT: "[redacted]",
            GovernanceAction.ANONYMIZE: "[anonymized]",
            GovernanceAction.DELETE: "[deleted]",
        }[action]
        entity_lifecycle = {
            GovernanceAction.REDACT: BusinessEntityLifecycle.REDACTED,
            GovernanceAction.ANONYMIZE: BusinessEntityLifecycle.ANONYMIZED,
            GovernanceAction.DELETE: BusinessEntityLifecycle.DELETED,
        }[action]
        external_lifecycle = {
            GovernanceAction.REDACT: ExternalRecordLifecycle.REDACTED,
            GovernanceAction.ANONYMIZE: ExternalRecordLifecycle.ANONYMIZED,
            GovernanceAction.DELETE: ExternalRecordLifecycle.DELETED,
        }[action]
        fact_lifecycle = {
            GovernanceAction.REDACT: CompanyFactLifecycle.REDACTED,
            GovernanceAction.ANONYMIZE: CompanyFactLifecycle.ANONYMIZED,
            GovernanceAction.DELETE: CompanyFactLifecycle.DELETED,
        }[action]

        def apply(state: BusinessContextState) -> BusinessContextState:
            if record.object_type == "business_entity":
                state.entities = [
                    item.model_copy(
                        update={
                            "name": marker,
                            "description": None,
                            "field_provenance": (),
                            "lifecycle": entity_lifecycle,
                            "updated_at": float(self.clock()),
                        }
                    )
                    if (
                        item.id == record.object_id
                        and item.organization_id == record.organization_id
                        and item.workspace_id == record.workspace_id
                    )
                    else item
                    for item in state.entities
                ]
            elif record.object_type == "external_record_ref":
                state.external_records = [
                    item.model_copy(
                        update={
                            "external_id": marker,
                            "display_name": marker,
                            "external_url": None,
                            "lifecycle": external_lifecycle,
                            "revoked_at": float(self.clock()),
                        }
                    )
                    if (
                        item.id == record.object_id
                        and item.organization_id == record.organization_id
                        and item.workspace_id == record.workspace_id
                    )
                    else item
                    for item in state.external_records
                ]
            elif record.object_type == "company_fact":
                state.facts = [
                    item.model_copy(
                        update={
                            "value": None,
                            "lifecycle": fact_lifecycle,
                        }
                    )
                    if (
                        item.id == record.object_id
                        and item.organization_id == record.organization_id
                        and item.workspace_id == record.workspace_id
                    )
                    else item
                    for item in state.facts
                ]
            else:
                raise BusinessContextValidationError(
                    f"unsupported governance object type: {record.object_type}"
                )
            return state

        self.store.update(apply)
        return (
            f"business-context:{record.object_type}:{record.object_id}:"
            f"{action.value}"
        )
