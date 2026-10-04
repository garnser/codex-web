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
    SkillSourceLifecycle,
    SkillSourceSyncRequest,
    SkillSourceUpdate,
)
from codex_web.skills import SkillOrigin, SkillProvenance, SkillUpdate
from codex_web.skill_security import SkillScanMode, SkillScanRequest
from codex_web.storage.skill_catalog import SkillSourceStore


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

    def create(self, payload: SkillSourceCreate, *, actor: AuthenticationActor) -> dict[str, Any]:
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

    def update(self, source_id: str, payload: SkillSourceUpdate, *, actor: AuthenticationActor) -> dict[str, Any]:
        self._require_admin(actor)
        current = self.get(source_id, actor=actor)
        updates = {
            key: getattr(payload, key)
            for key in payload.model_fields_set
            if getattr(payload, key) is not None
        }
        item = SkillSource.model_validate(
            current.model_copy(update={**updates, "updated_at": float(self.clock())}).model_dump(mode="python")
        )
        return self.store.put(item).model_dump(mode="json")

    @staticmethod
    def _digest(entry: Any) -> str:
        payload = entry.model_dump(mode="json", exclude={"reason", "provenance"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def sync(self, source_id: str, payload: SkillSourceSyncRequest, *, actor: AuthenticationActor) -> dict[str, Any]:
        self._require_admin(actor)
        source = self.get(source_id, actor=actor)
        if source.lifecycle != SkillSourceLifecycle.ACTIVE:
            raise SkillSourceConflict("archived Skill source cannot synchronize")
        imports = {item.upstream_id: item for item in source.imports}
        results: list[dict[str, Any]] = []
        now = float(self.clock())

        for entry in payload.entries:
            manifest = entry.manifest
            digest = self._digest(manifest)
            prior = imports.get(entry.upstream_id)
            provenance = SkillProvenance(
                source_type=source.source_type.value,
                source_ref=source.location,
                source_id=source.source_id,
                upstream_id=entry.upstream_id,
                source_revision=payload.source_revision,
                upstream_digest=digest,
                imported_at=now,
                origin=SkillOrigin.IMPORTED,
            )
            if prior is not None:
                if prior.skill_id != manifest.skill_id:
                    raise SkillSourceConflict(
                        f"upstream item {entry.upstream_id} cannot change canonical Skill ID"
                    )
                latest = self.skills.get(prior.skill_id, actor=actor)
                if latest["recordId"] != prior.record_id:
                    raise SkillSourceConflict(
                        f"Skill {prior.skill_id} has local changes; synchronize requires an explicit override or fork"
                    )
                if prior.upstream_digest == digest:
                    results.append({"upstreamId": entry.upstream_id, "skillId": prior.skill_id, "status": "unchanged", "revision": prior.revision})
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
