from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from codex_web.compatibility import ContractVersion
from codex_web.definitions import (
    DEFINITION_SCOPE_PRECEDENCE,
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionLifecycle,
    DefinitionPublicationApproval,
    DefinitionPublicationAssessment,
    DefinitionPublishRequest,
    DefinitionRecord,
    DefinitionReference,
    DefinitionRollbackRequest,
    DefinitionScope,
    definition_checksum,
    definition_is_effective,
    reference_for,
)
from codex_web.storage.definition_registry import DefinitionRegistryStore


class DefinitionError(ValueError):
    pass


class DefinitionNotFoundError(LookupError):
    pass


class DefinitionConflictError(RuntimeError):
    pass


class DefinitionCompatibilityError(RuntimeError):
    pass


DefinitionValidator = Callable[[dict[str, Any]], dict[str, Any]]
DefinitionChangeNotifier = Callable[[dict[str, Any]], None]
DefinitionUsageProvider = Callable[[DefinitionReference], list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class DefinitionPublicationGuardResult:
    change_classes: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    requires_approval: bool = False


DefinitionPublicationGuard = Callable[
    [DefinitionRecord, DefinitionRecord | None],
    DefinitionPublicationGuardResult,
]


@dataclass(frozen=True, slots=True)
class DefinitionKindSchema:
    kind: str
    schema_version: str
    validate: DefinitionValidator


class DefinitionSchemaRegistry:
    """Code-owned definition schemas/interpreters; payload values remain data."""

    def __init__(self) -> None:
        self._schemas: dict[tuple[str, str], DefinitionKindSchema] = {}

    def register(self, schema: DefinitionKindSchema) -> None:
        key = (schema.kind, schema.schema_version)
        if key in self._schemas:
            raise DefinitionConflictError(
                f"definition schema already registered: {schema.kind}@{schema.schema_version}"
            )
        self._schemas[key] = schema

    def get(self, kind: str, schema_version: str) -> DefinitionKindSchema:
        try:
            return self._schemas[(kind, schema_version)]
        except KeyError as exc:
            raise DefinitionCompatibilityError(
                f"unsupported definition schema: {kind}@{schema_version}"
            ) from exc

    def metadata(self) -> list[dict[str, str]]:
        return [
            {"kind": kind, "schema_version": version}
            for kind, version in sorted(self._schemas)
        ]


class DefinitionRegistryService:
    """Versioned definition lifecycle with deterministic scope resolution."""

    def __init__(
        self,
        store: DefinitionRegistryStore,
        *,
        schemas: DefinitionSchemaRegistry | None = None,
        engine_version: str = "1.0",
        notifier: DefinitionChangeNotifier | None = None,
    ) -> None:
        self.store = store
        self.schemas = schemas or DefinitionSchemaRegistry()
        self.engine_version = str(ContractVersion.parse(engine_version))
        self.notifier = notifier
        self._usage_providers: list[DefinitionUsageProvider] = []
        self._publication_guards: dict[str, DefinitionPublicationGuard] = {}
        self._cache: dict[tuple[str, str, str | None, str | None, str | None], DefinitionRecord] = {}

    def register_schema(self, schema: DefinitionKindSchema) -> None:
        self.schemas.register(schema)
        self._cache.clear()

    def register_usage_provider(self, provider: DefinitionUsageProvider) -> None:
        if provider not in self._usage_providers:
            self._usage_providers.append(provider)

    def register_publication_guard(
        self,
        kind: str,
        guard: DefinitionPublicationGuard,
    ) -> None:
        existing = self._publication_guards.get(kind)
        if existing is not None and existing is not guard:
            raise DefinitionConflictError(
                f"definition publication guard already registered: {kind}"
            )
        self._publication_guards[kind] = guard

    @staticmethod
    def _scope_id(scope_type: DefinitionScope, scope_id: str | None) -> str | None:
        if scope_type == DefinitionScope.GLOBAL:
            return None
        normalized = str(scope_id or "").strip()
        if not normalized:
            raise DefinitionError(f"{scope_type.value} definition requires scope_id")
        return normalized

    @classmethod
    def _same_slot(
        cls,
        record: DefinitionRecord,
        *,
        definition_id: str,
        kind: str,
        scope_type: DefinitionScope,
        scope_id: str | None,
    ) -> bool:
        return (
            record.definition_id == definition_id
            and record.kind == kind
            and record.scope_type == scope_type
            and record.scope_id == cls._scope_id(scope_type, scope_id)
        )

    def _validate_engine(self, record: DefinitionRecord) -> None:
        engine = ContractVersion.parse(self.engine_version)
        if record.min_engine_version is not None:
            if engine < ContractVersion.parse(record.min_engine_version):
                raise DefinitionCompatibilityError(
                    f"definition {record.definition_id} requires engine >= {record.min_engine_version}"
                )
        if record.max_engine_version is not None:
            if engine > ContractVersion.parse(record.max_engine_version):
                raise DefinitionCompatibilityError(
                    f"definition {record.definition_id} requires engine <= {record.max_engine_version}"
                )

    def _validated_record(self, record: DefinitionRecord) -> DefinitionRecord:
        self._validate_engine(record)
        schema = self.schemas.get(record.kind, record.definition_schema_version)
        normalized = schema.validate(dict(record.payload))
        checksum = definition_checksum(
            definition_id=record.definition_id,
            kind=record.kind,
            definition_schema_version=record.definition_schema_version,
            payload=normalized,
        )
        if checksum != record.checksum:
            raise DefinitionError("definition payload normalization changed checksum")
        return record

    def _notify(self, event_type: str, record: DefinitionRecord) -> None:
        self._cache.clear()
        if self.notifier is None:
            return
        self.notifier(
            {
                "type": event_type,
                "definition_id": record.definition_id,
                "kind": record.kind,
                "record_id": record.record_id,
                "revision": record.revision,
                "scope_type": record.scope_type.value,
                "scope_id": record.scope_id,
                "checksum": record.checksum,
            }
        )

    def list_records(
        self,
        *,
        kind: str | None = None,
        definition_id: str | None = None,
        scope_type: DefinitionScope | None = None,
        scope_id: str | None = None,
    ) -> list[DefinitionRecord]:
        records = self.store.load()
        if kind is not None:
            records = [record for record in records if record.kind == kind]
        if definition_id is not None:
            records = [record for record in records if record.definition_id == definition_id]
        if scope_type is not None:
            expected_scope = self._scope_id(scope_type, scope_id)
            records = [
                record
                for record in records
                if record.scope_type == scope_type and record.scope_id == expected_scope
            ]
        return sorted(
            records,
            key=lambda record: (
                record.kind,
                record.definition_id,
                DEFINITION_SCOPE_PRECEDENCE[record.scope_type],
                record.scope_id or "",
                record.revision,
            ),
        )

    def get_record(self, record_id: str) -> DefinitionRecord:
        for record in self.store.load():
            if record.record_id == record_id:
                return record
        raise DefinitionNotFoundError(f"definition record not found: {record_id}")

    def create_draft(self, payload: DefinitionDraftCreate) -> DefinitionRecord:
        schema = self.schemas.get(payload.kind, payload.definition_schema_version)
        normalized_payload = schema.validate(dict(payload.payload))
        scope_id = self._scope_id(payload.scope_type, payload.scope_id)
        created: list[DefinitionRecord] = []

        def update(records: list[DefinitionRecord]) -> list[DefinitionRecord]:
            revision = (
                max(
                    (
                        record.revision
                        for record in records
                        if self._same_slot(
                            record,
                            definition_id=payload.definition_id,
                            kind=payload.kind,
                            scope_type=payload.scope_type,
                            scope_id=scope_id,
                        )
                    ),
                    default=0,
                )
                + 1
            )
            record = DefinitionRecord(
                definition_id=payload.definition_id,
                kind=payload.kind,
                definition_schema_version=payload.definition_schema_version,
                revision=revision,
                scope_type=payload.scope_type,
                scope_id=scope_id,
                payload=normalized_payload,
                checksum=definition_checksum(
                    definition_id=payload.definition_id,
                    kind=payload.kind,
                    definition_schema_version=payload.definition_schema_version,
                    payload=normalized_payload,
                ),
                created_by=payload.actor,
                create_reason=payload.reason,
                effective_from=payload.effective_from,
                effective_until=payload.effective_until,
                min_engine_version=payload.min_engine_version,
                max_engine_version=payload.max_engine_version,
            )
            created.append(record)
            return [*records, record]

        self.store.update(update)
        self._notify("definition.draft_created", created[0])
        return created[0]

    def validate(self, record_id: str, *, actor: str) -> DefinitionRecord:
        validated: list[DefinitionRecord] = []

        def update(records: list[DefinitionRecord]) -> list[DefinitionRecord]:
            selected = next((record for record in records if record.record_id == record_id), None)
            if selected is None:
                raise DefinitionNotFoundError(f"definition record not found: {record_id}")
            if selected.lifecycle not in {DefinitionLifecycle.DRAFT, DefinitionLifecycle.VALIDATED}:
                raise DefinitionConflictError("only draft/validated definitions can be validated")
            self._validated_record(selected)
            result = selected.model_copy(
                update={
                    "lifecycle": DefinitionLifecycle.VALIDATED,
                    "validated_by": actor,
                    "validated_at": time.time(),
                }
            )
            validated.append(result)
            return [result if item.record_id == record_id else item for item in records]

        self.store.update(update)
        self._notify("definition.validated", validated[0])
        return validated[0]

    @staticmethod
    def _publication_fingerprint(
        selected: DefinitionRecord,
        active: DefinitionRecord | None,
        result: DefinitionPublicationGuardResult,
    ) -> str:
        payload = {
            "record_id": selected.record_id,
            "candidate_revision": selected.revision,
            "candidate_checksum": selected.checksum,
            "active_record_id": active.record_id if active else None,
            "active_revision": active.revision if active else None,
            "active_checksum": active.checksum if active else None,
            "change_classes": list(result.change_classes),
            "reasons": list(result.reasons),
            "requires_approval": result.requires_approval,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()

    def _publication_assessment(
        self,
        selected: DefinitionRecord,
        active: DefinitionRecord | None,
    ) -> DefinitionPublicationAssessment:
        guard = self._publication_guards.get(selected.kind)
        result = (
            guard(selected, active)
            if guard is not None
            else DefinitionPublicationGuardResult()
        )
        return DefinitionPublicationAssessment(
            record_id=selected.record_id,
            candidate_revision=selected.revision,
            candidate_checksum=selected.checksum,
            active_record_id=active.record_id if active else None,
            active_revision=active.revision if active else None,
            change_classes=tuple(dict.fromkeys(result.change_classes)),
            reasons=tuple(dict.fromkeys(result.reasons)),
            requires_approval=result.requires_approval,
            fingerprint=self._publication_fingerprint(selected, active, result),
        )

    def publication_preflight(
        self,
        record_id: str,
    ) -> DefinitionPublicationAssessment:
        records = self.store.load()
        selected = next(
            (record for record in records if record.record_id == record_id),
            None,
        )
        if selected is None:
            raise DefinitionNotFoundError(
                f"definition record not found: {record_id}"
            )
        if selected.lifecycle not in {
            DefinitionLifecycle.DRAFT,
            DefinitionLifecycle.VALIDATED,
        }:
            raise DefinitionConflictError(
                "publication preflight requires draft/validated definition"
            )
        selected = self._validated_record(selected)
        active = self._active_same_slot(records, selected)
        return self._publication_assessment(selected, active)

    def record_publication_approval(
        self,
        record_id: str,
        *,
        actor: str,
        organization_id: str,
        workspace_id: str,
        reason: str,
    ) -> DefinitionPublicationApproval:
        assessment = self.publication_preflight(record_id)
        if not assessment.requires_approval:
            raise DefinitionConflictError(
                "publication approval is not required for this revision"
            )
        approved: list[DefinitionPublicationApproval] = []

        def update(records: list[DefinitionRecord]) -> list[DefinitionRecord]:
            selected = next(
                (record for record in records if record.record_id == record_id),
                None,
            )
            if selected is None:
                raise DefinitionNotFoundError(
                    f"definition record not found: {record_id}"
                )
            if selected.created_by == actor:
                raise DefinitionConflictError(
                    "definition creator cannot approve their own sensitive publication"
                )
            current = self._publication_assessment(
                self._validated_record(selected),
                self._active_same_slot(records, selected),
            )
            if current.fingerprint != assessment.fingerprint:
                raise DefinitionConflictError(
                    "definition publication assessment changed before approval"
                )
            existing = next(
                (
                    item
                    for item in selected.publication_approvals
                    if item.fingerprint == current.fingerprint
                    and item.approved_by == actor
                ),
                None,
            )
            if existing is not None:
                approved.append(existing)
                return records
            evidence = DefinitionPublicationApproval(
                record_id=selected.record_id,
                fingerprint=current.fingerprint,
                active_revision=current.active_revision,
                approved_by=actor,
                organization_id=organization_id,
                workspace_id=workspace_id,
                reason=reason,
            )
            approved.append(evidence)
            replacement = selected.model_copy(
                update={
                    "publication_approvals": (
                        *selected.publication_approvals,
                        evidence,
                    )
                }
            )
            return [
                replacement if item.record_id == selected.record_id else item
                for item in records
            ]

        self.store.update(update)
        record = self.get_record(record_id)
        self._notify("definition.publication_approved", record)
        return approved[0]

    def _active_same_slot(
        self,
        records: list[DefinitionRecord],
        selected: DefinitionRecord,
    ) -> DefinitionRecord | None:
        values = [
            record
            for record in records
            if record.lifecycle == DefinitionLifecycle.PUBLISHED
            and self._same_slot(
                record,
                definition_id=selected.definition_id,
                kind=selected.kind,
                scope_type=selected.scope_type,
                scope_id=selected.scope_id,
            )
        ]
        if len(values) > 1:
            raise DefinitionConflictError("multiple active definitions exist for one canonical slot")
        return values[0] if values else None

    def publish(self, record_id: str, request: DefinitionPublishRequest) -> DefinitionRecord:
        published: list[DefinitionRecord] = []

        def update(records: list[DefinitionRecord]) -> list[DefinitionRecord]:
            selected = next((record for record in records if record.record_id == record_id), None)
            if selected is None:
                raise DefinitionNotFoundError(f"definition record not found: {record_id}")
            if selected.lifecycle not in {DefinitionLifecycle.DRAFT, DefinitionLifecycle.VALIDATED}:
                raise DefinitionConflictError("only draft/validated definitions can be published")
            selected = self._validated_record(selected)
            active = self._active_same_slot(records, selected)
            active_revision = active.revision if active else None
            if (
                request.expected_active_revision is not None
                and request.expected_active_revision != active_revision
            ):
                raise DefinitionConflictError("active definition revision changed before publication")

            assessment = self._publication_assessment(selected, active)
            approval = None
            if assessment.requires_approval:
                if not request.publication_approval_id:
                    raise DefinitionConflictError(
                        "sensitive definition publication requires approved preflight evidence"
                    )
                approval = next(
                    (
                        item
                        for item in selected.publication_approvals
                        if item.id == request.publication_approval_id
                    ),
                    None,
                )
                if approval is None:
                    raise DefinitionConflictError(
                        "definition publication approval evidence not found"
                    )
                if (
                    approval.record_id != selected.record_id
                    or approval.fingerprint != assessment.fingerprint
                    or approval.active_revision != assessment.active_revision
                ):
                    raise DefinitionConflictError(
                        "definition publication approval is stale for current preflight"
                    )
                if approval.approved_by == request.actor:
                    raise DefinitionConflictError(
                        "definition publisher cannot use their own approval"
                    )

            approval_metadata = dict(request.approval_metadata)
            approval_metadata.update(
                {
                    "publication_preflight_fingerprint": assessment.fingerprint,
                    "publication_change_classes": ",".join(
                        assessment.change_classes
                    ),
                    "publication_requires_approval": (
                        "true" if assessment.requires_approval else "false"
                    ),
                }
            )
            if approval is not None:
                approval_metadata.update(
                    {
                        "publication_approval_id": approval.id,
                        "publication_approved_by": approval.approved_by,
                        "publication_approved_at": str(approval.approved_at),
                    }
                )

            now = time.time()
            current = selected.model_copy(
                update={
                    "lifecycle": DefinitionLifecycle.PUBLISHED,
                    "validated_by": selected.validated_by or request.actor,
                    "validated_at": selected.validated_at or now,
                    "published_by": request.actor,
                    "publish_reason": request.reason,
                    "published_at": now,
                    "approval_metadata": approval_metadata,
                    "supersedes_record_id": active.record_id if active else None,
                }
            )
            result: list[DefinitionRecord] = []
            for record in records:
                if active is not None and record.record_id == active.record_id:
                    result.append(
                        record.model_copy(
                            update={
                                "lifecycle": DefinitionLifecycle.SUPERSEDED,
                                "superseded_by_record_id": current.record_id,
                            }
                        )
                    )
                elif record.record_id == selected.record_id:
                    result.append(current)
                else:
                    result.append(record)
            published.append(current)
            return result

        self.store.update(update)
        self._notify("definition.published", published[0])
        return published[0]

    def quarantine(self, record_id: str, *, actor: str, reason: str) -> DefinitionRecord:
        changed: list[DefinitionRecord] = []

        def update(records: list[DefinitionRecord]) -> list[DefinitionRecord]:
            selected = next((record for record in records if record.record_id == record_id), None)
            if selected is None:
                raise DefinitionNotFoundError(f"definition record not found: {record_id}")
            current = selected.model_copy(
                update={
                    "lifecycle": DefinitionLifecycle.QUARANTINED,
                    "publish_reason": f"quarantined by {actor}: {reason}",
                }
            )
            changed.append(current)
            return [current if record.record_id == record_id else record for record in records]

        self.store.update(update)
        self._notify("definition.quarantined", changed[0])
        return changed[0]

    def rollback(self, request: DefinitionRollbackRequest) -> DefinitionRecord:
        scope_id = self._scope_id(request.scope_type, request.scope_id)
        target = next(
            (
                record
                for record in self.store.load()
                if self._same_slot(
                    record,
                    definition_id=request.definition_id,
                    kind=request.kind,
                    scope_type=request.scope_type,
                    scope_id=scope_id,
                )
                and record.revision == request.target_revision
            ),
            None,
        )
        if target is None:
            raise DefinitionNotFoundError("definition rollback target not found")
        draft = self.create_draft(
            DefinitionDraftCreate(
                definition_id=target.definition_id,
                kind=target.kind,
                definition_schema_version=target.definition_schema_version,
                scope_type=target.scope_type,
                scope_id=target.scope_id,
                payload=target.payload,
                actor=request.actor,
                reason=request.reason or f"rollback to revision {target.revision}",
                effective_from=target.effective_from,
                effective_until=target.effective_until,
                min_engine_version=target.min_engine_version,
                max_engine_version=target.max_engine_version,
            )
        )

        def mark(records: list[DefinitionRecord]) -> list[DefinitionRecord]:
            return [
                record.model_copy(update={"rollback_of_record_id": target.record_id})
                if record.record_id == draft.record_id
                else record
                for record in records
            ]

        self.store.update(mark)
        return self.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor=request.actor,
                reason=request.reason or f"rollback to revision {target.revision}",
                expected_active_revision=request.expected_active_revision,
            ),
        )

    @staticmethod
    def _matches_context(record: DefinitionRecord, context: DefinitionContext) -> bool:
        expected = context.scope_ids()[record.scope_type]
        if record.scope_type == DefinitionScope.GLOBAL:
            return True
        return bool(expected and expected == record.scope_id)

    def resolve(
        self,
        *,
        definition_id: str,
        kind: str,
        context: DefinitionContext | None = None,
        now: float | None = None,
    ) -> DefinitionRecord:
        context = context or DefinitionContext()
        cache_key = (
            kind,
            definition_id,
            context.organization_id,
            context.workspace_id,
            context.project_id,
        )
        if now is None and cache_key in self._cache:
            return self._cache[cache_key].model_copy(deep=True)

        candidates = [
            record
            for record in self.store.load()
            if record.definition_id == definition_id
            and record.kind == kind
            and definition_is_effective(record, now=now)
            and self._matches_context(record, context)
        ]
        if not candidates:
            raise DefinitionNotFoundError(
                f"no active definition for {kind}:{definition_id}"
            )
        by_scope: dict[tuple[DefinitionScope, str | None], list[DefinitionRecord]] = {}
        for record in candidates:
            by_scope.setdefault((record.scope_type, record.scope_id), []).append(record)
        ambiguous = [slot for slot, records in by_scope.items() if len(records) > 1]
        if ambiguous:
            raise DefinitionConflictError("multiple active definitions exist for one canonical slot")

        selected = max(
            candidates,
            key=lambda record: (
                DEFINITION_SCOPE_PRECEDENCE[record.scope_type],
                record.revision,
            ),
        )
        selected = self._validated_record(selected)
        if now is None:
            self._cache[cache_key] = selected.model_copy(deep=True)
        return selected

    def reference(
        self,
        *,
        definition_id: str,
        kind: str,
        context: DefinitionContext | None = None,
    ) -> DefinitionReference:
        return reference_for(
            self.resolve(definition_id=definition_id, kind=kind, context=context)
        )

    def export(self, *, kind: str | None = None) -> dict[str, Any]:
        records = self.list_records(kind=kind)
        return {
            "format": "codex-web-definitions",
            "version": "1.0",
            "records": [record.model_dump(mode="json") for record in records],
        }

    def import_records(
        self,
        payload: dict[str, Any],
        *,
        actor: str,
    ) -> list[DefinitionRecord]:
        if payload.get("format") != "codex-web-definitions" or payload.get("version") != "1.0":
            raise DefinitionCompatibilityError("unsupported definition import format")
        imported: list[DefinitionRecord] = []
        for raw in payload.get("records", []):
            source = DefinitionRecord.model_validate(raw)
            self._validated_record(source)
            draft = self.create_draft(
                DefinitionDraftCreate(
                    definition_id=source.definition_id,
                    kind=source.kind,
                    definition_schema_version=source.definition_schema_version,
                    scope_type=source.scope_type,
                    scope_id=source.scope_id,
                    payload=source.payload,
                    actor=actor,
                    reason=f"imported from {source.record_id}",
                    effective_from=source.effective_from,
                    effective_until=source.effective_until,
                    min_engine_version=source.min_engine_version,
                    max_engine_version=source.max_engine_version,
                )
            )
            imported.append(draft)
        return imported

    def bootstrap(
        self,
        seeds: list[DefinitionDraftCreate],
        *,
        actor: str = "bootstrap",
    ) -> list[DefinitionRecord]:
        created: list[DefinitionRecord] = []
        for seed in seeds:
            seed_scope_id = self._scope_id(seed.scope_type, seed.scope_id)
            existing = [
                record
                for record in self.store.load()
                if self._same_slot(
                    record,
                    definition_id=seed.definition_id,
                    kind=seed.kind,
                    scope_type=seed.scope_type,
                    scope_id=seed_scope_id,
                )
            ]
            if existing:
                continue
            draft = self.create_draft(seed.model_copy(update={"actor": actor}))
            created.append(
                self.publish(
                    draft.record_id,
                    DefinitionPublishRequest(actor=actor, reason="initial bootstrap"),
                )
            )
        return created

    def bootstrap_status(self) -> dict[str, Any]:
        records = self.store.load()
        active = [record for record in records if record.lifecycle == DefinitionLifecycle.PUBLISHED]
        return {
            "records": len(records),
            "active": len(active),
            "schemas": self.schemas.metadata(),
            "engine_version": self.engine_version,
        }

    def diff(self, left_record_id: str, right_record_id: str) -> dict[str, Any]:
        left = self.get_record(left_record_id)
        right = self.get_record(right_record_id)
        if left.definition_id != right.definition_id or left.kind != right.kind:
            raise DefinitionError("definition diff requires the same definition_id and kind")

        changed_paths: list[str] = []

        def walk(path: str, before: Any, after: Any) -> None:
            if type(before) is not type(after):
                changed_paths.append(path or "$")
                return
            if isinstance(before, dict):
                for key in sorted(set(before) | set(after)):
                    child = f"{path}.{key}" if path else key
                    if key not in before or key not in after:
                        changed_paths.append(child)
                    else:
                        walk(child, before[key], after[key])
                return
            if isinstance(before, list):
                if before != after:
                    changed_paths.append(path or "$")
                return
            if before != after:
                changed_paths.append(path or "$")

        walk("", left.payload, right.payload)
        return {
            "definition_id": left.definition_id,
            "kind": left.kind,
            "left": {
                "record_id": left.record_id,
                "revision": left.revision,
                "checksum": left.checksum,
            },
            "right": {
                "record_id": right.record_id,
                "revision": right.revision,
                "checksum": right.checksum,
            },
            "changed_paths": changed_paths,
            "changed": bool(changed_paths),
        }

    def usage(self, record_id: str) -> dict[str, Any]:
        record = self.get_record(record_id)
        reference = reference_for(record)
        items: list[dict[str, Any]] = []
        for provider in self._usage_providers:
            items.extend(provider(reference) or [])
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            object_type = str(item.get("object_type") or "unknown")
            object_id = str(item.get("object_id") or "")
            unique[(object_type, object_id)] = dict(item)
        values = sorted(
            unique.values(),
            key=lambda item: (
                str(item.get("object_type") or ""),
                str(item.get("object_id") or ""),
            ),
        )
        return {
            "reference": reference.model_dump(mode="json"),
            "items": values,
            "count": len(values),
        }

