from __future__ import annotations

import time
from typing import Any

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
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
    PrincipalKind,
)
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.definitions import (
    DefinitionKindSchema,
    DefinitionPublicationAssessment,
    DefinitionRegistryService,
)
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.skill_definitions import (
    SKILL_BUNDLE_FORMAT,
    SKILL_BUNDLE_VERSION,
    SKILL_DEFINITION_KIND,
    SKILL_SCHEMA_VERSION,
    SkillAsset,
    SkillAssetKind,
    SkillAttachmentRequest,
    SkillBundleAsset,
    SkillBundleImport,
    SkillBundleManifest,
    SkillDefinition,
    SkillDraftRequest,
    SkillEditRequest,
    SkillExecutionMaterial,
    SkillExecutionMaterialRequest,
    SkillPublishRequest,
    SkillRollbackRequest,
    SkillSecurityClassification,
    SkillSourceMetadata,
    SkillSourceType,
    SkillUpdatePolicy,
    VerifiedProcedurePromotion,
    skill_definition_from_bundle,
    validate_skill_definition,
)


class SkillDefinitionError(RuntimeError):
    pass


class SkillDefinitionNotFound(SkillDefinitionError):
    pass


class SkillDefinitionConflict(SkillDefinitionError):
    pass


class SkillDefinitionService:
    def __init__(
        self,
        registry: DefinitionRegistryService,
        *,
        profiles: AgentProfileService | None = None,
        clock=time.time,
    ) -> None:
        self.registry = registry
        self.profiles = profiles
        self.clock = clock

    @staticmethod
    def _is_admin(actor: AuthenticationActor) -> bool:
        if actor.principal_kind == PrincipalKind.SERVICE:
            return "skills:admin" in actor.service_scopes
        return actor.has_role(
            MembershipRole.OWNER,
            MembershipRole.ADMIN,
        )

    @classmethod
    def _require_publish_actor(cls, actor: AuthenticationActor) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if "skills:admin" not in actor.service_scopes:
                raise AuthorizationError(
                    "skills:admin service scope required"
                )
            return
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(
            actor,
            AuthenticationAssurance.MFA,
        )

    @classmethod
    def _require_draft_actor(
        cls,
        actor: AuthenticationActor,
        *,
        record: DefinitionRecord | None = None,
    ) -> None:
        if actor.principal_kind == PrincipalKind.SERVICE:
            if not (
                {"skills:write", "skills:admin"}
                & set(actor.service_scopes)
            ):
                raise AuthorizationError(
                    "skills:write service scope required"
                )
            return
        if cls._is_admin(actor):
            return
        if record is not None and record.created_by != actor.identity_id:
            raise AuthorizationError(
                "skill creator or administrator required"
            )

    @staticmethod
    def _scope(
        actor: AuthenticationActor,
        scope_type: DefinitionScope,
    ) -> tuple[DefinitionScope, str | None]:
        if scope_type == DefinitionScope.WORKSPACE:
            return scope_type, actor.workspace_id
        if scope_type == DefinitionScope.ORGANIZATION:
            return scope_type, actor.organization_id
        raise SkillDefinitionConflict(
            "Skill drafts may only use workspace or organization scope"
        )

    @staticmethod
    def _visible(
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
        return False

    def _record(
        self,
        record_id: str,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        try:
            record = self.registry.get_record(record_id)
        except Exception as exc:
            raise SkillDefinitionNotFound(
                "Skill Definition not found"
            ) from exc
        if not self._visible(record, actor):
            raise SkillDefinitionNotFound(
                "Skill Definition not found"
            )
        return record

    @staticmethod
    def definition(record: DefinitionRecord) -> SkillDefinition:
        if record.kind != SKILL_DEFINITION_KIND:
            raise SkillDefinitionConflict(
                "definition record is not an agent.skill"
            )
        return SkillDefinition.model_validate(record.payload)

    def create_draft(
        self,
        payload: SkillDraftRequest,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        self._require_draft_actor(actor)
        scope_type, scope_id = self._scope(
            actor,
            payload.scope_type,
        )
        skill = payload.skill
        if skill.owner_identity_id is None:
            skill = skill.model_copy(
                update={"owner_identity_id": actor.identity_id}
            )
        elif (
            skill.owner_identity_id != actor.identity_id
            and not self._is_admin(actor)
        ):
            raise AuthorizationError(
                "only administrators may create a Skill for another owner"
            )
        return self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=payload.skill_id,
                kind=SKILL_DEFINITION_KIND,
                definition_schema_version=SKILL_SCHEMA_VERSION,
                scope_type=scope_type,
                scope_id=scope_id,
                payload=skill.model_dump(mode="json"),
                actor=actor.identity_id,
                reason=payload.reason,
            )
        )

    def edit(
        self,
        record_id: str,
        payload: SkillEditRequest,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        current = self._record(record_id, actor=actor)
        self._require_draft_actor(actor, record=current)
        owner = self.definition(current).owner_identity_id
        if (
            owner
            and owner != actor.identity_id
            and not self._is_admin(actor)
        ):
            raise AuthorizationError(
                "Skill owner or administrator required"
            )
        return self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=current.definition_id,
                kind=SKILL_DEFINITION_KIND,
                definition_schema_version=SKILL_SCHEMA_VERSION,
                scope_type=current.scope_type,
                scope_id=current.scope_id,
                payload=payload.skill.model_dump(mode="json"),
                actor=actor.identity_id,
                reason=payload.reason,
                derived_from_record_id=current.record_id,
                min_engine_version=current.min_engine_version,
                max_engine_version=current.max_engine_version,
            )
        )

    def publish(
        self,
        record_id: str,
        payload: SkillPublishRequest,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        self._require_publish_actor(actor)
        record = self._record(record_id, actor=actor)
        return self.registry.publish(
            record.record_id,
            DefinitionPublishRequest(
                actor=actor.identity_id,
                reason=payload.reason,
                expected_active_revision=(
                    payload.expected_active_revision
                ),
                approval_metadata=payload.approval_metadata,
            ),
        )

    def archive(
        self,
        record_id: str,
        payload: SkillAttachmentRequest,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        self._require_publish_actor(actor)
        record = self._record(record_id, actor=actor)
        return self.registry.disable(
            record.record_id,
            actor=actor.identity_id,
            reason=payload.reason,
        )

    def rollback(
        self,
        record_id: str,
        payload: SkillRollbackRequest,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        self._require_publish_actor(actor)
        record = self._record(record_id, actor=actor)
        return self.registry.rollback(
            DefinitionRollbackRequest(
                definition_id=record.definition_id,
                kind=SKILL_DEFINITION_KIND,
                scope_type=record.scope_type,
                scope_id=record.scope_id,
                target_revision=payload.target_revision,
                actor=actor.identity_id,
                reason=payload.reason,
                expected_active_revision=(
                    payload.expected_active_revision
                ),
                approval_metadata=payload.approval_metadata,
            )
        )

    def get(
        self,
        record_id: str,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        return self._record(record_id, actor=actor)

    def revision(
        self,
        skill_id: str,
        revision: int,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        candidates = [
            item
            for item in self.registry.list_records(
                kind=SKILL_DEFINITION_KIND,
                definition_id=skill_id,
            )
            if item.revision == revision
            and self._visible(item, actor)
        ]
        if not candidates:
            raise SkillDefinitionNotFound(
                "Skill Definition revision not found"
            )
        if len(candidates) > 1:
            # One tenant actor should never resolve two same-id/revision
            # records at the same effective scope.
            candidates.sort(
                key=lambda item: (
                    item.scope_type.value,
                    item.scope_id or "",
                )
            )
        return candidates[-1]

    def list(
        self,
        *,
        actor: AuthenticationActor,
        tag: str | None = None,
        owner_identity_id: str | None = None,
        provider_capability: str | None = None,
        worker_capability: str | None = None,
        lifecycle: DefinitionLifecycle | None = None,
        include_revisions: bool = False,
    ) -> list[DefinitionRecord]:
        values = [
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
            for item in values:
                key = (
                    item.definition_id,
                    item.scope_type,
                    item.scope_id,
                )
                existing = latest.get(key)
                if (
                    existing is None
                    or item.revision > existing.revision
                ):
                    latest[key] = item
            values = list(latest.values())

        filtered: list[DefinitionRecord] = []
        for item in values:
            skill = self.definition(item)
            if lifecycle is not None and item.lifecycle != lifecycle:
                continue
            if tag and tag not in skill.tags:
                continue
            if (
                owner_identity_id
                and skill.owner_identity_id != owner_identity_id
            ):
                continue
            if (
                provider_capability
                and provider_capability
                not in skill.required_provider_capabilities
            ):
                continue
            if (
                worker_capability
                and worker_capability
                not in skill.required_worker_capabilities
            ):
                continue
            filtered.append(item)
        return sorted(
            filtered,
            key=lambda item: (
                self.definition(item).name.casefold(),
                item.definition_id,
                item.revision,
            ),
        )

    def import_bundle(
        self,
        bundle: SkillBundleImport,
        *,
        actor: AuthenticationActor,
        reason: str = "import Skill bundle",
    ) -> DefinitionRecord:
        skill = skill_definition_from_bundle(
            bundle,
            imported_at=float(self.clock()),
        )
        return self.create_draft(
            SkillDraftRequest(
                skill_id=bundle.manifest.skill_id,
                skill=skill,
                reason=reason,
                scope_type=DefinitionScope.WORKSPACE,
            ),
            actor=actor,
        )

    def export_bundle(
        self,
        record_id: str,
        *,
        actor: AuthenticationActor,
    ) -> dict[str, Any]:
        record = self._record(record_id, actor=actor)
        skill = self.definition(record)
        assets = tuple(
            SkillBundleAsset(
                path=(
                    item.source_path
                    or f"assets/{item.id}"
                ),
                content=item.content,
                media_type=item.media_type,
                kind=item.kind,
                security_classification=(
                    item.security_classification
                ),
                include_by_default=item.include_by_default,
            )
            for item in skill.assets
        )
        return {
            "format": SKILL_BUNDLE_FORMAT,
            "version": SKILL_BUNDLE_VERSION,
            "manifest": SkillBundleManifest(
                skill_id=record.definition_id,
                name=skill.name,
                description=skill.description,
                owner_identity_id=skill.owner_identity_id,
                tags=skill.tags,
                applicability=skill.applicability,
                required_provider_capabilities=(
                    skill.required_provider_capabilities
                ),
                required_worker_capabilities=(
                    skill.required_worker_capabilities
                ),
                inputs=skill.inputs,
                outputs=skill.outputs,
                compatibility_tags=skill.compatibility_tags,
                source_uri=skill.source.source_uri,
                external_revision=(
                    skill.source.external_revision
                ),
            ).model_dump(mode="json"),
            "skill_md": skill.body,
            "assets": [
                item.model_dump(mode="json")
                for item in assets
            ],
            "record": reference_for(record).model_dump(
                mode="json"
            ),
        }

    def promote_verified_procedure(
        self,
        payload: VerifiedProcedurePromotion,
        *,
        actor: AuthenticationActor,
    ) -> DefinitionRecord:
        body = payload.procedure_summary.strip()
        if payload.checklist:
            body += "\n\n## Checklist\n" + "\n".join(
                f"{index}. {item.strip()}"
                for index, item in enumerate(
                    payload.checklist,
                    start=1,
                )
            )
        skill = SkillDefinition(
            name=payload.name,
            description=payload.description,
            body=body,
            owner_identity_id=actor.identity_id,
            tags=payload.tags,
            source=SkillSourceMetadata(
                source_type=SkillSourceType.VERIFIED_PROCEDURE,
                source_execution_id=payload.source_execution_id,
                update_policy=SkillUpdatePolicy.MANUAL,
                imported_at=float(self.clock()),
            ),
        )
        return self.create_draft(
            SkillDraftRequest(
                skill_id=payload.skill_id,
                skill=skill,
                reason=(
                    "promoted from verified procedure; human review "
                    "required before publication"
                ),
            ),
            actor=actor,
        )

    def usage(
        self,
        record_id: str,
        *,
        actor: AuthenticationActor,
    ) -> list[dict[str, Any]]:
        record = self._record(record_id, actor=actor)
        if self.profiles is None:
            return []
        reference = reference_for(record)
        result: list[dict[str, Any]] = []
        for profile in self.profiles.list(
            actor=actor,
            include_archived=True,
        ):
            if not any(
                item.record_id == reference.record_id
                for item in profile.skill_refs
            ):
                continue
            result.append(
                {
                    "profile_id": profile.profile_id,
                    "profile_revision": profile.revision,
                    "profile_name": profile.name,
                    "profile_lifecycle": profile.lifecycle.value,
                    "organization_id": profile.organization_id,
                    "workspace_id": profile.workspace_id,
                }
            )
        return result

    def attach(
        self,
        record_id: str,
        profile_id: str,
        payload: SkillAttachmentRequest,
        *,
        actor: AuthenticationActor,
    ):
        if self.profiles is None:
            raise SkillDefinitionConflict(
                "Agent Profile service is unavailable"
            )
        record = self._record(record_id, actor=actor)
        if not definition_is_effective(record):
            raise SkillDefinitionConflict(
                "only an effective published Skill revision may be attached"
            )
        reference = reference_for(record)
        profile = self.profiles.get(
            profile_id,
            actor=actor,
        )
        refs = tuple(
            dict.fromkeys(
                (*profile.skill_refs, reference)
            )
        )
        return self.profiles.update(
            profile_id,
            AgentProfileUpdate(
                skill_refs=refs,
                reason=payload.reason,
            ),
            actor=actor,
        )

    def detach(
        self,
        record_id: str,
        profile_id: str,
        payload: SkillAttachmentRequest,
        *,
        actor: AuthenticationActor,
    ):
        if self.profiles is None:
            raise SkillDefinitionConflict(
                "Agent Profile service is unavailable"
            )
        record = self._record(record_id, actor=actor)
        profile = self.profiles.get(
            profile_id,
            actor=actor,
        )
        refs = tuple(
            item
            for item in profile.skill_refs
            if item.record_id != record.record_id
        )
        return self.profiles.update(
            profile_id,
            AgentProfileUpdate(
                skill_refs=refs,
                reason=payload.reason,
            ),
            actor=actor,
        )

    def material(
        self,
        reference: DefinitionReference,
        request: SkillExecutionMaterialRequest | None = None,
    ) -> SkillExecutionMaterial:
        record = self.registry.get_record(reference.record_id)
        if reference_for(record) != reference:
            raise SkillDefinitionConflict(
                "Skill reference does not match canonical record"
            )
        if record.kind != SKILL_DEFINITION_KIND:
            raise SkillDefinitionConflict(
                "execution reference is not an agent.skill"
            )
        if not definition_is_effective(record):
            raise SkillDefinitionConflict(
                "Skill revision is not effective for execution"
            )
        skill = self.definition(record)
        selection = request or SkillExecutionMaterialRequest()
        if not skill.applicability.matches(
            purpose=selection.purpose,
            model_class=selection.model_class,
            capability_tags=selection.capability_tags,
        ):
            raise SkillDefinitionConflict(
                "Skill revision is not applicable to execution context"
            )
        explicit = set(selection.include_asset_ids)
        known_ids = {item.id for item in skill.assets}
        missing = sorted(explicit - known_ids)
        if missing:
            raise SkillDefinitionConflict(
                "requested Skill assets are unavailable: "
                + ", ".join(missing)
            )

        passive: list[SkillAsset] = []
        helpers: list[SkillAsset] = []
        for asset in skill.assets:
            selected = (
                asset.id in explicit
                or (
                    asset.include_by_default
                    and asset.applicability.matches(
                        purpose=selection.purpose,
                        model_class=selection.model_class,
                        capability_tags=selection.capability_tags,
                    )
                )
            )
            if not selected:
                continue
            if asset.kind == SkillAssetKind.HELPER:
                # Helper content is never automatically inserted into model
                # context or executed. An explicit caller may inspect it and
                # route execution through the canonical worker/ActionIntent
                # boundary.
                if asset.id in explicit:
                    helpers.append(asset)
                continue
            passive.append(asset)

        return SkillExecutionMaterial(
            reference=reference,
            name=skill.name,
            description=skill.description,
            body=skill.body,
            passive_assets=tuple(passive),
            helper_assets=tuple(helpers),
            required_provider_capabilities=(
                skill.required_provider_capabilities
            ),
            required_worker_capabilities=(
                skill.required_worker_capabilities
            ),
        )


def assess_skill_publication(
    active: DefinitionRecord | None,
    candidate: DefinitionRecord,
) -> DefinitionPublicationAssessment:
    del active
    skill = SkillDefinition.model_validate(candidate.payload)
    reasons: list[str] = []
    for asset in skill.assets:
        if (
            asset.security_classification
            == SkillSecurityClassification.EXECUTABLE_HELPER
        ):
            reasons.append("skill_executable_helper")
        elif (
            asset.security_classification
            == SkillSecurityClassification.SIDE_EFFECTING_HELPER
        ):
            reasons.append("skill_side_effecting_helper")
    if skill.required_worker_capabilities:
        reasons.append("skill_requires_worker_capabilities")
    return DefinitionPublicationAssessment(
        requires_independent_approval=bool(reasons),
        reasons=tuple(sorted(set(reasons))),
    )


def install_skill_definitions(
    registry: DefinitionRegistryService,
    *,
    profiles: AgentProfileService | None = None,
) -> SkillDefinitionService:
    if not any(
        item["kind"] == SKILL_DEFINITION_KIND
        and item["schema_version"] == SKILL_SCHEMA_VERSION
        for item in registry.schemas.metadata()
    ):
        registry.register_schema(
            DefinitionKindSchema(
                kind=SKILL_DEFINITION_KIND,
                schema_version=SKILL_SCHEMA_VERSION,
                validate=validate_skill_definition,
                assess_publish=assess_skill_publication,
            )
        )
    return SkillDefinitionService(
        registry,
        profiles=profiles,
    )
