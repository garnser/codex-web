from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException

from codex_web.agent_profiles import AgentProfileUpdate
from codex_web.definitions import (
    DefinitionDraftCreate,
    DefinitionLifecycle,
    DefinitionPublishRequest,
    DefinitionRecord,
    DefinitionReference,
    DefinitionRollbackRequest,
    DefinitionScope,
    definition_is_effective,
    reference_for,
)
from codex_web.identity import AuthenticationActor, AuthenticationAssurance
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.definitions import (
    DefinitionKindSchema,
    DefinitionRegistryService,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.skills import (
    MAX_SKILL_BODY_CHARACTERS,
    SKILL_DEFINITION_KIND,
    SKILL_DEFINITION_SCHEMA_VERSION,
    SkillAssetKind,
    SkillBundle,
    SkillContextSelection,
    SkillDefinition,
    SkillSource,
    SkillSourceKind,
    validate_skill_definition,
)


class SkillError(ValueError):
    pass


class SkillNotFoundError(LookupError):
    pass


class SkillConflictError(RuntimeError):
    pass


class SkillService:
    """Skill product facade over the canonical Definition Registry."""

    DEFAULT_CONTEXT_CHARACTERS = 24_000
    MAX_CONTEXT_CHARACTERS = 64_000

    def __init__(
        self,
        registry: DefinitionRegistryService,
        *,
        profiles: AgentProfileService | None = None,
        projects: ProjectService | None = None,
    ) -> None:
        self.registry = registry
        self.profiles = profiles
        self.projects = projects

    @staticmethod
    def _is_admin(actor: AuthenticationActor) -> bool:
        try:
            IdentityService.require_admin(actor)
        except Exception:
            return False
        return True

    @staticmethod
    def _require_publish_authority(actor: AuthenticationActor) -> None:
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )

    def _project_visible(
        self,
        project_id: str | None,
        actor: AuthenticationActor,
    ) -> bool:
        if not project_id or self.projects is None:
            return False
        try:
            self.projects.get(project_id, actor.tenant)
        except ProjectNotFoundError:
            return False
        return True

    def _visible(
        self,
        record: DefinitionRecord,
        actor: AuthenticationActor,
    ) -> bool:
        if record.kind != SKILL_DEFINITION_KIND:
            return False
        if record.scope_type == DefinitionScope.GLOBAL:
            return True
        if record.scope_type == DefinitionScope.ORGANIZATION:
            return record.scope_id == actor.organization_id
        if record.scope_type == DefinitionScope.WORKSPACE:
            return record.scope_id == actor.workspace_id
        if record.scope_type == DefinitionScope.PROJECT:
            return self._project_visible(record.scope_id, actor)
        return False

    def _require_visible(
        self,
        record: DefinitionRecord,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        if not self._visible(record, actor):
            raise SkillNotFoundError("skill revision not found")
        return record

    def _record(
        self,
        record_id: str,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        try:
            record = self.registry.get_record(record_id)
        except Exception as exc:
            raise SkillNotFoundError("skill revision not found") from exc
        if record.kind != SKILL_DEFINITION_KIND:
            raise SkillNotFoundError("skill revision not found")
        return self._require_visible(record, actor)

    def _latest_visible(
        self,
        skill_id: str,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        records = [
            item
            for item in self.registry.list_records(
                kind=SKILL_DEFINITION_KIND,
                definition_id=skill_id,
            )
            if self._visible(item, actor)
        ]
        if not records:
            raise SkillNotFoundError("skill not found")
        return max(records, key=lambda item: (item.revision, item.created_at))

    @staticmethod
    def definition(record: DefinitionRecord) -> SkillDefinition:
        if record.kind != SKILL_DEFINITION_KIND:
            raise SkillConflictError("definition is not a Skill")
        return SkillDefinition.model_validate(record.payload)

    def list(
        self,
        *,
        actor: AuthenticationActor,
        query: str | None = None,
        tag: str | None = None,
        capability: str | None = None,
        owner_identity_id: str | None = None,
        lifecycle: DefinitionLifecycle | None = None,
        include_revisions: bool = False,
    ) -> list[DefinitionRecord]:
        records = [
            item
            for item in self.registry.list_records(
                kind=SKILL_DEFINITION_KIND
            )
            if self._visible(item, actor)
        ]
        if not include_revisions:
            latest: dict[
                tuple[str, DefinitionScope, str | None],
                DefinitionRecord,
            ] = {}
            for item in records:
                key = (
                    item.definition_id,
                    item.scope_type,
                    item.scope_id,
                )
                current = latest.get(key)
                if current is None or item.revision > current.revision:
                    latest[key] = item
            records = list(latest.values())

        normalized_query = str(query or "").strip().casefold()
        normalized_tag = str(tag or "").strip().casefold()
        normalized_capability = str(capability or "").strip().casefold()
        values: list[DefinitionRecord] = []
        for record in records:
            if lifecycle is not None and record.lifecycle != lifecycle:
                continue
            skill = self.definition(record)
            if (
                owner_identity_id is not None
                and skill.owner_identity_id != owner_identity_id
            ):
                continue
            if normalized_tag and normalized_tag not in set(skill.tags):
                continue
            capabilities = {
                *skill.required_provider_capabilities,
                *skill.required_worker_capabilities,
            }
            if normalized_capability and normalized_capability not in capabilities:
                continue
            if normalized_query:
                haystack = " ".join(
                    (
                        record.definition_id,
                        skill.name,
                        skill.description,
                        " ".join(skill.tags),
                        " ".join(skill.applicability),
                    )
                ).casefold()
                if normalized_query not in haystack:
                    continue
            values.append(record)
        return sorted(
            values,
            key=lambda item: (
                self.definition(item).name.casefold(),
                item.definition_id,
                -item.revision,
            ),
        )

    def get(
        self,
        record_id: str,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        return self._record(record_id, actor)

    def revisions(
        self,
        skill_id: str,
        *,
        actor: AuthenticationActor,
    ) -> list[DefinitionRecord]:
        values = [
            item
            for item in self.registry.list_records(
                kind=SKILL_DEFINITION_KIND,
                definition_id=skill_id,
            )
            if self._visible(item, actor)
        ]
        if not values:
            raise SkillNotFoundError("skill not found")
        return sorted(values, key=lambda item: item.revision)

    def _scope(
        self,
        actor: AuthenticationActor,
        scope_type: DefinitionScope,
        scope_id: str | None,
    ) -> str | None:
        if scope_type == DefinitionScope.GLOBAL:
            if actor.assurance != AuthenticationAssurance.LOCAL_TRUSTED:
                raise AuthorizationError(
                    "global Skill mutation requires local-trusted context"
                )
            return None
        if scope_type == DefinitionScope.ORGANIZATION:
            if scope_id not in {None, actor.organization_id}:
                raise AuthorizationError(
                    "cross-tenant Skill organization scope denied"
                )
            return actor.organization_id
        if scope_type == DefinitionScope.WORKSPACE:
            if scope_id not in {None, actor.workspace_id}:
                raise AuthorizationError(
                    "cross-tenant Skill workspace scope denied"
                )
            return actor.workspace_id
        if scope_type == DefinitionScope.PROJECT:
            if not self._project_visible(scope_id, actor):
                raise AuthorizationError(
                    "cross-tenant or unknown Skill project scope denied"
                )
            return scope_id
        raise AuthorizationError("unsupported Skill scope")

    def create_draft(
        self,
        *,
        skill_id: str,
        skill: SkillDefinition,
        actor: AuthenticationActor,
        scope_type: DefinitionScope = DefinitionScope.WORKSPACE,
        scope_id: str | None = None,
        reason: str | None = None,
        derived_from_record_id: str | None = None,
    ) -> DefinitionRecord:
        effective_scope_id = self._scope(actor, scope_type, scope_id)
        owner = skill.owner_identity_id or actor.identity_id
        if owner != actor.identity_id and not self._is_admin(actor):
            raise AuthorizationError(
                "only administrators may create a Skill for another owner"
            )
        normalized = skill.model_copy(
            update={"owner_identity_id": owner}
        )
        if derived_from_record_id is not None:
            source = self._record(derived_from_record_id, actor)
            if source.definition_id != skill_id:
                raise SkillConflictError(
                    "Skill revision source belongs to a different Skill"
                )
            source_skill = self.definition(source)
            if (
                source_skill.owner_identity_id != actor.identity_id
                and not self._is_admin(actor)
            ):
                raise AuthorizationError(
                    "only Skill owner/admin may revise this Skill"
                )
        return self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=skill_id,
                kind=SKILL_DEFINITION_KIND,
                definition_schema_version=(
                    SKILL_DEFINITION_SCHEMA_VERSION
                ),
                scope_type=scope_type,
                scope_id=effective_scope_id,
                payload=normalized.model_dump(mode="json"),
                actor=actor.identity_id,
                reason=reason,
                derived_from_record_id=derived_from_record_id,
            )
        )

    def revise(
        self,
        record_id: str,
        *,
        skill: SkillDefinition,
        actor: AuthenticationActor,
        reason: str,
    ) -> DefinitionRecord:
        source = self._record(record_id, actor)
        source_skill = self.definition(source)
        if (
            source_skill.owner_identity_id != actor.identity_id
            and not self._is_admin(actor)
        ):
            raise AuthorizationError(
                "only Skill owner/admin may revise this Skill"
            )
        owner = (
            skill.owner_identity_id
            or source_skill.owner_identity_id
            or actor.identity_id
        )
        if (
            owner != source_skill.owner_identity_id
            and not self._is_admin(actor)
        ):
            raise AuthorizationError(
                "only administrators may transfer Skill ownership"
            )
        return self.create_draft(
            skill_id=source.definition_id,
            skill=skill.model_copy(
                update={"owner_identity_id": owner}
            ),
            actor=actor,
            scope_type=source.scope_type,
            scope_id=source.scope_id,
            reason=reason,
            derived_from_record_id=source.record_id,
        )

    def publish(
        self,
        record_id: str,
        *,
        actor: AuthenticationActor,
        reason: str | None = None,
        expected_active_revision: int | None = None,
    ) -> DefinitionRecord:
        self._require_publish_authority(actor)
        record = self._record(record_id, actor)
        return self.registry.publish(
            record.record_id,
            DefinitionPublishRequest(
                actor=actor.identity_id,
                reason=reason,
                expected_active_revision=expected_active_revision,
            ),
        )

    def archive(
        self,
        record_id: str,
        *,
        actor: AuthenticationActor,
        reason: str,
    ) -> DefinitionRecord:
        self._require_publish_authority(actor)
        record = self._record(record_id, actor)
        return self.registry.deprecate(
            record.record_id,
            actor=actor.identity_id,
            reason=reason,
        )

    def rollback(
        self,
        *,
        skill_id: str,
        target_revision: int,
        actor: AuthenticationActor,
        reason: str,
    ) -> DefinitionRecord:
        self._require_publish_authority(actor)
        latest = self._latest_visible(skill_id, actor)
        active = [
            item
            for item in self.revisions(skill_id, actor=actor)
            if item.lifecycle == DefinitionLifecycle.PUBLISHED
        ]
        expected_active = (
            max(active, key=lambda item: item.revision).revision
            if active
            else None
        )
        return self.registry.rollback(
            DefinitionRollbackRequest(
                definition_id=skill_id,
                kind=SKILL_DEFINITION_KIND,
                scope_type=latest.scope_type,
                scope_id=latest.scope_id,
                target_revision=target_revision,
                actor=actor.identity_id,
                reason=reason,
                expected_active_revision=expected_active,
            )
        )

    def export_bundle(
        self,
        record_id: str,
        *,
        actor: AuthenticationActor,
    ) -> SkillBundle:
        record = self._record(record_id, actor)
        skill = self.definition(record)
        metadata = skill.model_dump(
            mode="json",
            exclude={"body", "assets"},
        )
        metadata.update(
            {
                "definition_revision": record.revision,
                "definition_record_id": record.record_id,
                "definition_checksum": record.checksum,
            }
        )
        return SkillBundle(
            skill_id=record.definition_id,
            skill_md=skill.body,
            metadata=metadata,
            assets=skill.assets,
        )

    def import_bundle(
        self,
        bundle: SkillBundle,
        *,
        actor: AuthenticationActor,
        scope_type: DefinitionScope = DefinitionScope.WORKSPACE,
        scope_id: str | None = None,
        source_reference: str | None = None,
    ) -> DefinitionRecord:
        metadata = {
            key: value
            for key, value in bundle.metadata.items()
            if key
            not in {
                "definition_revision",
                "definition_record_id",
                "definition_checksum",
                "owner_identity_id",
                "source",
            }
        }
        skill = SkillDefinition.model_validate(
            {
                **metadata,
                "name": metadata.get("name") or bundle.skill_id,
                "body": bundle.skill_md,
                "assets": [
                    item.model_dump(mode="json")
                    for item in bundle.assets
                ],
                "owner_identity_id": actor.identity_id,
                "source": SkillSource(
                    kind=SkillSourceKind.IMPORTED,
                    reference=source_reference,
                ).model_dump(mode="json"),
            }
        )
        return self.create_draft(
            skill_id=bundle.skill_id,
            skill=skill,
            actor=actor,
            scope_type=scope_type,
            scope_id=scope_id,
            reason="imported Skill bundle",
        )

    def promote_verified_procedure(
        self,
        *,
        skill_id: str,
        name: str,
        body: str,
        evidence_refs: tuple[str, ...],
        actor: AuthenticationActor,
        description: str = "",
        tags: tuple[str, ...] = (),
        source_reference: str | None = None,
    ) -> DefinitionRecord:
        if not evidence_refs:
            raise SkillError(
                "verified procedure promotion requires Evidence references"
            )
        skill = SkillDefinition(
            name=name,
            description=description,
            body=body,
            tags=tags,
            owner_identity_id=actor.identity_id,
            source=SkillSource(
                kind=SkillSourceKind.VERIFIED_PROCEDURE,
                reference=source_reference,
                verified_evidence_refs=evidence_refs,
            ),
        )
        # Deliberately creates a draft. Human review/publication remains
        # explicit and independent from the source execution/model output.
        return self.create_draft(
            skill_id=skill_id,
            skill=skill,
            actor=actor,
            reason="promoted from verified procedure for human review",
        )

    def usage(
        self,
        record_id: str,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        record = self._record(record_id, actor)
        result = self.registry.usage(record.record_id)
        visible: list[dict[str, Any]] = []
        for item in result.get("items", []):
            organization_id = item.get("organization_id")
            workspace_id = item.get("workspace_id")
            if (
                organization_id not in {None, actor.organization_id}
                or workspace_id not in {None, actor.workspace_id}
            ):
                continue
            visible.append(item)
        return {
            "reference": result["reference"],
            "items": visible,
            "count": len(visible),
        }

    def attach(
        self,
        profile_id: str,
        record_id: str,
        *,
        actor: AuthenticationActor,
        reason: str,
    ):
        if self.profiles is None:
            raise SkillConflictError("Agent Profile service is unavailable")
        record = self._record(record_id, actor)
        if (
            record.lifecycle != DefinitionLifecycle.PUBLISHED
            or not definition_is_effective(record)
        ):
            raise SkillConflictError(
                "only an effective published Skill revision may be attached"
            )
        profile = self.profiles.get(profile_id, actor=actor)
        reference = reference_for(record)
        refs = tuple(
            (
                *(
                    item
                    for item in profile.skill_refs
                    if item.definition_id != record.definition_id
                ),
                reference,
            )
        )
        return self.profiles.update(
            profile_id,
            AgentProfileUpdate(
                skill_refs=refs,
                reason=reason,
            ),
            actor=actor,
        )

    def detach(
        self,
        profile_id: str,
        skill_id: str,
        *,
        actor: AuthenticationActor,
        reason: str,
    ):
        if self.profiles is None:
            raise SkillConflictError("Agent Profile service is unavailable")
        profile = self.profiles.get(profile_id, actor=actor)
        refs = tuple(
            item
            for item in profile.skill_refs
            if item.definition_id != skill_id
        )
        if refs == profile.skill_refs:
            return profile
        return self.profiles.update(
            profile_id,
            AgentProfileUpdate(
                skill_refs=refs,
                reason=reason,
            ),
            actor=actor,
        )

    def requirements(
        self,
        references: Iterable[DefinitionReference],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        provider: list[str] = []
        worker: list[str] = []
        for reference in references:
            record = self.registry.get_record(reference.record_id)
            if reference_for(record) != reference:
                raise SkillConflictError(
                    "Skill reference does not match canonical revision"
                )
            if record.kind != SKILL_DEFINITION_KIND:
                raise SkillConflictError(
                    "Agent Profile skill reference is not an agent.skill"
                )
            if record.lifecycle in {
                DefinitionLifecycle.QUARANTINED,
                DefinitionLifecycle.DISABLED,
                DefinitionLifecycle.DRAFT,
                DefinitionLifecycle.VALIDATED,
            }:
                raise SkillConflictError(
                    "Skill revision is not eligible for execution"
                )
            skill = self.definition(record)
            provider.extend(skill.required_provider_capabilities)
            worker.extend(skill.required_worker_capabilities)
        return (
            tuple(dict.fromkeys(provider)),
            tuple(dict.fromkeys(worker)),
        )

    def context_for(
        self,
        references: Iterable[DefinitionReference],
        *,
        relevance_tags: Iterable[str] = (),
        asset_paths: Iterable[str] = (),
        max_characters: int = DEFAULT_CONTEXT_CHARACTERS,
    ) -> tuple[SkillContextSelection, ...]:
        budget = max(
            1_000,
            min(int(max_characters), self.MAX_CONTEXT_CHARACTERS),
        )
        tags = {
            str(item).strip().casefold()
            for item in relevance_tags
            if str(item).strip()
        }
        explicit_paths = {
            str(item).strip()
            for item in asset_paths
            if str(item).strip()
        }
        selections: list[SkillContextSelection] = []
        remaining = budget

        for reference in references:
            record = self.registry.get_record(reference.record_id)
            if reference_for(record) != reference:
                raise SkillConflictError(
                    "Skill reference does not match canonical revision"
                )
            if record.kind != SKILL_DEFINITION_KIND:
                raise SkillConflictError(
                    "execution Skill reference is not an agent.skill"
                )
            if record.lifecycle in {
                DefinitionLifecycle.QUARANTINED,
                DefinitionLifecycle.DISABLED,
                DefinitionLifecycle.DRAFT,
                DefinitionLifecycle.VALIDATED,
            }:
                raise SkillConflictError(
                    "Skill revision is not eligible for execution"
                )
            skill = self.definition(record)
            if remaining <= 0:
                break

            body = skill.body[:remaining]
            used = len(body)
            truncated = len(body) < len(skill.body)
            remaining -= used
            selected_assets = []
            omitted = []

            for asset in skill.assets:
                eligible_kind = asset.kind in {
                    SkillAssetKind.REFERENCE,
                    SkillAssetKind.TEMPLATE,
                }
                tag_match = bool(
                    tags
                    and tags.intersection(
                        {
                            item.casefold()
                            for item in asset.capability_tags
                        }
                    )
                )
                explicitly_selected = asset.path in explicit_paths
                if (
                    not eligible_kind
                    or asset.executable
                    or not (tag_match or explicitly_selected)
                ):
                    omitted.append(asset.path)
                    continue
                if len(asset.content) > remaining:
                    omitted.append(asset.path)
                    truncated = True
                    continue
                selected_assets.append(asset)
                remaining -= len(asset.content)
                used += len(asset.content)

            selections.append(
                SkillContextSelection(
                    definition_id=record.definition_id,
                    record_id=record.record_id,
                    revision=record.revision,
                    name=skill.name,
                    body=body,
                    selected_assets=tuple(selected_assets),
                    omitted_asset_paths=tuple(omitted),
                    required_provider_capabilities=(
                        skill.required_provider_capabilities
                    ),
                    required_worker_capabilities=(
                        skill.required_worker_capabilities
                    ),
                    characters=used,
                    truncated=truncated,
                )
            )
        return tuple(selections)


def install_skill_definitions(
    registry: DefinitionRegistryService,
    *,
    profiles: AgentProfileService | None = None,
    projects: ProjectService | None = None,
) -> SkillService:
    if not any(
        item["kind"] == SKILL_DEFINITION_KIND
        and item["schema_version"] == SKILL_DEFINITION_SCHEMA_VERSION
        for item in registry.schemas.metadata()
    ):
        registry.register_schema(
            DefinitionKindSchema(
                kind=SKILL_DEFINITION_KIND,
                schema_version=SKILL_DEFINITION_SCHEMA_VERSION,
                validate=validate_skill_definition,
            )
        )
    return SkillService(
        registry,
        profiles=profiles,
        projects=projects,
    )
