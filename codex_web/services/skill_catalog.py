from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from codex_web.identity import AuthenticationActor
from codex_web.services.skills import SkillConflict, SkillNotFound, SkillService
from codex_web.skill_catalog import (
    SkillSource,
    SkillSourceCreate,
    SkillSourceImport,
    SkillSourceHealthReport,
    SkillSourceLifecycle,
    SkillSourceSyncRequest,
    SkillSourceUpdate,
    SkillSourceTransport,
    SkillSourceType,
    UiSkillsDiscoveryRequest,
    UiSkillsImportMode,
    UiSkillsImportRequest,
)
from codex_web.skills import SkillOrigin, SkillProvenance, SkillUpdate
from codex_web.skill_security import SkillScanMode, SkillScanRequest
from codex_web.storage.skill_catalog import SkillSourceStore
from codex_web.services.ui_skills import UiSkillsAdapter


class SkillSourceError(RuntimeError):
    pass


class SkillSourceNotFound(SkillSourceError):
    pass


class SkillSourceConflict(SkillSourceError):
    pass


class SkillCatalogService:
    """Persist approved source configuration and normalize bounded catalog bundles.

    Source payloads are untrusted data. Synchronization creates Definition Registry
    drafts only; publication remains a separate reviewed Skill action.
    """

    def __init__(
        self,
        store: SkillSourceStore,
        skills: SkillService,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.skills = skills
        self.clock = clock

    @staticmethod
    def _require_admin(actor: AuthenticationActor) -> None:
        SkillService._require_mutation(actor)

    def list(self, *, actor: AuthenticationActor) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self.store.list(actor.organization_id, actor.workspace_id)
        ]

    def get(self, source_id: str, *, actor: AuthenticationActor) -> SkillSource:
        item = self.store.get(actor.organization_id, actor.workspace_id, source_id)
        if item is None:
            raise SkillSourceNotFound("Skill source not found")
        return item

    def create(
        self, payload: SkillSourceCreate, *, actor: AuthenticationActor
    ) -> dict[str, Any]:
        self._require_admin(actor)
        if self.store.get(actor.organization_id, actor.workspace_id, payload.source_id):
            raise SkillSourceConflict("Skill source already exists")
        now = float(self.clock())
        item = SkillSource(
            **payload.model_dump(mode="python"),
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            created_by=actor.identity_id,
            created_at=now,
            updated_at=now,
        )
        return self.store.put(item).model_dump(mode="json")

    def update(
        self, source_id: str, payload: SkillSourceUpdate, *, actor: AuthenticationActor
    ) -> dict[str, Any]:
        self._require_admin(actor)
        current = self.get(source_id, actor=actor)
        updates = {
            key: getattr(payload, key)
            for key in payload.model_fields_set
            if getattr(payload, key) is not None
        }
        item = SkillSource.model_validate(
            current.model_copy(
                update={**updates, "updated_at": float(self.clock())}
            ).model_dump(mode="python")
        )
        return self.store.put(item).model_dump(mode="json")

    @staticmethod
    def _digest(entry: Any) -> str:
        payload = entry.model_dump(mode="json", exclude={"reason", "provenance"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _only_local_taxonomy_changed(
        previous: dict[str, Any], latest: dict[str, Any]
    ) -> bool:
        previous_skill = dict(previous["skill"])
        latest_skill = dict(latest["skill"])
        for field in ("categories", "applicability_tags"):
            previous_skill.pop(field, None)
            latest_skill.pop(field, None)
        return previous_skill == latest_skill

    def sync(
        self,
        source_id: str,
        payload: SkillSourceSyncRequest,
        *,
        actor: AuthenticationActor,
        source_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self._require_admin(actor)
        source = self.get(source_id, actor=actor)
        if source.lifecycle != SkillSourceLifecycle.ACTIVE:
            raise SkillSourceConflict("archived Skill source cannot synchronize")
        if source.source_type == SkillSourceType.UI_SKILLS and source_metadata is None:
            raise SkillSourceConflict(
                "ui-skills synchronization requires a governed discovery snapshot"
            )
        imports = {item.upstream_id: item for item in source.imports}
        results: list[dict[str, Any]] = []
        now = float(self.clock())
        source_metadata = source_metadata or {}

        for entry in payload.entries:
            manifest = entry.manifest
            digest = self._digest(manifest)
            prior = imports.get(entry.upstream_id)
            metadata = source_metadata.get(entry.upstream_id, {})
            provenance = SkillProvenance(
                source_type=source.source_type.value,
                source_ref=source.location,
                source_id=source.source_id,
                upstream_id=entry.upstream_id,
                source_revision=payload.source_revision,
                upstream_digest=digest,
                imported_at=now,
                origin=SkillOrigin.IMPORTED,
                upstream_categories=tuple(metadata.get("upstream_categories", ())),
                upstream_location=metadata.get("upstream_location"),
                source_transport=str(
                    metadata.get("source_transport", source.transport.value)
                ),
            )
            if prior is not None:
                if prior.skill_id != manifest.skill_id:
                    raise SkillSourceConflict(
                        f"upstream item {entry.upstream_id} cannot change canonical Skill ID"
                    )
                latest = self.skills.get(prior.skill_id, actor=actor)
                if latest["recordId"] != prior.record_id:
                    previous = self.skills.get(
                        prior.skill_id, actor=actor, revision=prior.revision
                    )
                    if not self._only_local_taxonomy_changed(previous, latest):
                        raise SkillSourceConflict(
                            f"Skill {prior.skill_id} has local changes; synchronize requires an explicit override or fork"
                        )
                    manifest = manifest.model_copy(
                        update={
                            "categories": tuple(latest["skill"].get("categories", ())),
                            "applicability_tags": tuple(
                                latest["skill"].get("applicability_tags", ())
                            ),
                        }
                    )
                if prior.upstream_digest == digest:
                    results.append(
                        {
                            "upstreamId": entry.upstream_id,
                            "skillId": prior.skill_id,
                            "status": "unchanged",
                            "revision": prior.revision,
                        }
                    )
                    continue
                saved = self.skills.update(
                    prior.skill_id,
                    SkillUpdate(
                        name=manifest.name,
                        description=manifest.description,
                        instructions=manifest.instructions,
                        categories=manifest.categories,
                        applicability_tags=manifest.applicability_tags,
                        capability_tags=manifest.capability_tags,
                        assets=manifest.assets,
                        required_provider_capabilities=manifest.required_provider_capabilities,
                        required_worker_capabilities=manifest.required_worker_capabilities,
                        input_expectations=manifest.input_expectations,
                        output_expectations=manifest.output_expectations,
                        provenance=provenance,
                        reason=f"synchronize Skill source {source.source_id} at {payload.source_revision}",
                    ),
                    actor=actor,
                )
                status = "updated_draft"
            else:
                try:
                    self.skills.get(manifest.skill_id, actor=actor)
                except SkillNotFound:
                    pass
                else:
                    raise SkillSourceConflict(
                        f"Skill {manifest.skill_id} already exists outside this source"
                    )
                saved = self.skills.create(
                    manifest.model_copy(
                        update={
                            "provenance": provenance,
                            "reason": f"import Skill source {source.source_id} at {payload.source_revision}",
                        }
                    ),
                    actor=actor,
                )
                status = "created_draft"
            imported = SkillSourceImport(
                upstream_id=entry.upstream_id,
                skill_id=saved["skillId"],
                record_id=saved["recordId"],
                revision=saved["revision"],
                source_revision=payload.source_revision,
                upstream_digest=digest,
                imported_at=now,
                upstream_categories=tuple(metadata.get("upstream_categories", ())),
                upstream_location=metadata.get("upstream_location"),
                source_transport=SkillSourceTransport(
                    metadata.get("source_transport", source.transport.value)
                ),
            )
            imports[entry.upstream_id] = imported
            security = None
            if (
                self.skills.security is not None
                and self.skills.security.policy(actor).scan_imported_on_ingest
            ):
                policy = self.skills.security.policy(actor)
                security = self.skills.security.scan(
                    saved,
                    SkillScanRequest(
                        mode=(
                            SkillScanMode.SEMANTIC
                            if policy.semantic_for_untrusted_sources
                            and source.trust.value != "approved"
                            else SkillScanMode.STATIC
                        )
                    ),
                    actor=actor,
                )
            results.append(
                {
                    "upstreamId": entry.upstream_id,
                    "skillId": saved["skillId"],
                    "status": status,
                    "revision": saved["revision"],
                    "recordId": saved["recordId"],
                    "security": security,
                }
            )

        updated = SkillSource.model_validate(
            source.model_copy(
                update={
                    "imports": tuple(imports.values()),
                    "last_sync_at": now,
                    "last_sync_revision": payload.source_revision,
                    "last_sync_status": "succeeded",
                    "last_sync_error": None,
                    "updated_at": now,
                }
            ).model_dump(mode="python")
        )
        self.store.put(updated)
        return {
            "source": updated.model_dump(mode="json"),
            "items": results,
            "count": len(results),
            "publicationRequired": True,
        }

    def discovery(
        self, source_id: str, *, actor: AuthenticationActor
    ) -> dict[str, Any]:
        source = self.get(source_id, actor=actor)
        snapshot = self.store.get_discovery(
            actor.organization_id, actor.workspace_id, source_id
        )
        imported = {item.upstream_id: item for item in source.imports}
        entries = (
            []
            if snapshot is None
            else [
                {
                    "upstream_id": item.upstream_id,
                    "name": item.name,
                    "description": item.description,
                    "categories": list(item.categories),
                    "tags": list(item.tags),
                    "upstream_location": item.upstream_location,
                    "imported": item.upstream_id in imported,
                    "import_revision": (
                        imported[item.upstream_id].revision
                        if item.upstream_id in imported
                        else None
                    ),
                }
                for item in snapshot.entries
            ]
        )
        return {
            "sourceId": source.source_id,
            "sourceRevision": snapshot.source_revision if snapshot else None,
            "transport": (
                snapshot.transport.value if snapshot else source.transport.value
            ),
            "providerEvidenceId": snapshot.provider_evidence_id if snapshot else None,
            "categories": list(source.discovered_categories),
            "items": entries,
            "count": len(entries),
        }

    def discover_ui_skills(
        self,
        source_id: str,
        payload: UiSkillsDiscoveryRequest,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        self._require_admin(actor)
        source = self.get(source_id, actor=actor)
        if source.source_type != SkillSourceType.UI_SKILLS:
            raise SkillSourceConflict("discovery endpoint requires a ui-skills source")
        if source.lifecycle != SkillSourceLifecycle.ACTIVE:
            raise SkillSourceConflict("archived Skill source cannot discover")
        if payload.transport != source.transport:
            raise SkillSourceConflict(
                "discovery transport does not match source configuration"
            )
        # Validate that the configured transport has a bounded provider/worker plan.
        UiSkillsAdapter.transport_plan(payload.transport)
        now = float(self.clock())
        categories = tuple(
            sorted(
                {category for item in payload.entries for category in item.categories},
                key=str.casefold,
            )
        )
        available = {item.upstream_id for item in payload.entries}
        imports = tuple(
            item.model_copy(
                update={"upstream_available": item.upstream_id in available}
            )
            for item in source.imports
        )
        updated = SkillSource.model_validate(
            source.model_copy(
                update={
                    "imports": imports,
                    "last_health_at": now,
                    "health_status": "healthy",
                    "discovered_count": len(payload.entries),
                    "discovered_categories": categories,
                    "last_sync_status": "discovered",
                    "last_sync_revision": payload.source_revision,
                    "updated_at": now,
                }
            ).model_dump(mode="python")
        )
        self.store.put_discovery(
            actor.organization_id, actor.workspace_id, source_id, payload
        )
        self.store.put(updated)
        result = self.discovery(source_id, actor=actor)
        if source.automatic_sync and source.auto_import_categories:
            approved_categories = {
                category.casefold() for category in source.auto_import_categories
            }
            selected = tuple(
                item.upstream_id
                for item in payload.entries
                if {category.casefold() for category in item.categories}.intersection(
                    approved_categories
                )
            )
            if selected:
                result["automaticImport"] = self.import_ui_skills(
                    source_id,
                    UiSkillsImportRequest(
                        mode=UiSkillsImportMode.SELECTED,
                        upstream_ids=selected,
                    ),
                    actor=actor,
                )
        return result

    def import_ui_skills(
        self,
        source_id: str,
        payload: UiSkillsImportRequest,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        self._require_admin(actor)
        source = self.get(source_id, actor=actor)
        if source.source_type != SkillSourceType.UI_SKILLS:
            raise SkillSourceConflict("ui-skills import requires a ui-skills source")
        snapshot = self.store.get_discovery(
            actor.organization_id, actor.workspace_id, source_id
        )
        if snapshot is None:
            raise SkillSourceConflict(
                "ui-skills source has no governed discovery snapshot"
            )
        by_id = {item.upstream_id: item for item in snapshot.entries}
        if payload.mode == UiSkillsImportMode.ALL:
            selected = list(snapshot.entries)
        elif payload.mode == UiSkillsImportMode.CATEGORY:
            category = str(payload.category).casefold()
            selected = [
                item
                for item in snapshot.entries
                if any(value.casefold() == category for value in item.categories)
            ]
        else:
            missing = [item for item in payload.upstream_ids if item not in by_id]
            if missing:
                raise SkillSourceConflict(
                    f"ui-skills discovery does not contain: {', '.join(missing)}"
                )
            selected = [by_id[item] for item in payload.upstream_ids]
        if not selected:
            raise SkillSourceConflict("ui-skills import selection is empty")
        entries = tuple(UiSkillsAdapter.normalize(item) for item in selected)
        metadata = {
            item.upstream_id: {
                "upstream_categories": item.categories,
                "upstream_location": item.upstream_location,
                "source_transport": snapshot.transport.value,
            }
            for item in selected
        }
        return self.sync(
            source_id,
            SkillSourceSyncRequest(
                source_revision=snapshot.source_revision,
                entries=entries,
            ),
            actor=actor,
            source_metadata=metadata,
        )

    def record_health(
        self,
        source_id: str,
        payload: SkillSourceHealthReport,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        self._require_admin(actor)
        source = self.get(source_id, actor=actor)
        if source.source_type != SkillSourceType.UI_SKILLS:
            raise SkillSourceConflict(
                "health reporting endpoint requires a ui-skills source"
            )
        now = float(self.clock())
        updated = SkillSource.model_validate(
            source.model_copy(
                update={
                    "last_health_at": now,
                    "health_status": payload.status.value,
                    "last_sync_error": payload.error,
                    "updated_at": now,
                }
            ).model_dump(mode="python")
        )
        self.store.put(updated)
        return {
            "source": updated.model_dump(mode="json"),
            "providerEvidenceId": payload.provider_evidence_id,
        }
