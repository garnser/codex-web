from __future__ import annotations

import time
from dataclasses import dataclass

from codex_web.authority import (
    AUTHORITY_AUTONOMY_RISK_RANK,
    AUTHORITY_LEVEL_RANK,
    AUTHORITY_ROLE_CATALOG_ID,
    AUTHORITY_ROLE_CATALOG_KIND,
    AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
    AuthorityDecision,
    AuthorityDecisionOutcome,
    AuthorityEvaluationRequest,
    AuthorityGrant,
    AuthorityRoleCatalogDefinition,
    validate_authority_role_catalog,
)
from codex_web.authority_seed import authority_role_catalog_seed_payload
from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionRecord,
    reference_for,
)
from codex_web.identity import AuthenticationActor
from codex_web.services.definitions import (
    DefinitionCompatibilityError,
    DefinitionConflictError,
    DefinitionError,
    DefinitionKindSchema,
    DefinitionPublicationGuardResult,
    DefinitionRegistryService,
)
from codex_web.resources import ResourceLifecycle
from codex_web.services.resources import (
    ResourceCatalogError,
    ResourceCatalogService,
)


def _set_scope_expands(before: tuple, after: tuple) -> bool:
    """Return True when an allow-list becomes less restrictive."""

    if not before:
        return False
    if not after:
        return True
    return not set(after).issubset(set(before))


def _limit_expands(before: float | int | None, after: float | int | None) -> bool:
    if before is None:
        return False
    if after is None:
        return True
    return after > before


def _approval_expands(before, after) -> bool:
    if after.count < before.count:
        return True
    if before.count == 0:
        return False
    if not before.role_ids:
        return False
    if not after.role_ids:
        return True
    return not set(after.role_ids).issubset(set(before.role_ids))


def assess_authority_role_publication(
    selected: DefinitionRecord,
    active: DefinitionRecord | None,
) -> DefinitionPublicationGuardResult:
    """Classify whether an authority catalog revision expands operational power."""

    classes: list[str] = []
    reasons: list[str] = []
    sensitive = False

    def mark(change_class: str, reason: str, *, expansion: bool = False) -> None:
        nonlocal sensitive
        classes.append(change_class)
        reasons.append(reason)
        sensitive = sensitive or expansion

    try:
        candidate = AuthorityRoleCatalogDefinition.model_validate(selected.payload)
        previous = (
            AuthorityRoleCatalogDefinition.model_validate(active.payload)
            if active is not None
            else None
        )
    except Exception as exc:
        return DefinitionPublicationGuardResult(
            change_classes=("authority.comparison_ambiguous",),
            reasons=(
                "authority catalog comparison could not be proven safe: "
                f"{type(exc).__name__}: {exc}",
            ),
            requires_approval=True,
        )

    if previous is None:
        return DefinitionPublicationGuardResult(
            change_classes=("authority.catalog_created",),
            reasons=(
                "new authority catalog slot can introduce operational authority",
            ),
            requires_approval=True,
        )

    before_roles = {item.id: item for item in previous.roles}
    after_roles = {item.id: item for item in candidate.roles}
    for role_id in sorted(set(after_roles) - set(before_roles)):
        mark(
            "authority.role_added",
            f"role added: {role_id}",
            expansion=True,
        )
    for role_id in sorted(set(before_roles) - set(after_roles)):
        mark(
            "authority.role_removed",
            f"role removed: {role_id}",
        )

    for role_id in sorted(set(before_roles) & set(after_roles)):
        before_role = before_roles[role_id]
        after_role = after_roles[role_id]
        added_inherits = sorted(set(after_role.inherits) - set(before_role.inherits))
        removed_inherits = sorted(set(before_role.inherits) - set(after_role.inherits))
        if added_inherits:
            mark(
                "authority.inheritance_added",
                f"role {role_id} inherits additional roles: "
                + ", ".join(added_inherits),
                expansion=True,
            )
        if removed_inherits:
            mark(
                "authority.inheritance_removed",
                f"role {role_id} removed inherited roles: "
                + ", ".join(removed_inherits),
            )

        before_grants = {item.id: item for item in before_role.grants}
        after_grants = {item.id: item for item in after_role.grants}
        for grant_id in sorted(set(after_grants) - set(before_grants)):
            mark(
                "authority.grant_added",
                f"role {role_id} grant added: {grant_id}",
                expansion=True,
            )
        for grant_id in sorted(set(before_grants) - set(after_grants)):
            mark(
                "authority.grant_removed",
                f"role {role_id} grant removed: {grant_id}",
            )

        for grant_id in sorted(set(before_grants) & set(after_grants)):
            before = before_grants[grant_id]
            after = after_grants[grant_id]
            prefix = f"role {role_id} grant {grant_id}"

            if before.capability != after.capability:
                mark(
                    "authority.capability_changed",
                    f"{prefix} capability changed from {before.capability} "
                    f"to {after.capability}",
                    expansion=True,
                )
            if AUTHORITY_LEVEL_RANK[after.level] > AUTHORITY_LEVEL_RANK[before.level]:
                mark(
                    "authority.level_increased",
                    f"{prefix} level increased from {before.level.value} "
                    f"to {after.level.value}",
                    expansion=True,
                )
            elif AUTHORITY_LEVEL_RANK[after.level] < AUTHORITY_LEVEL_RANK[before.level]:
                mark(
                    "authority.level_reduced",
                    f"{prefix} level reduced from {before.level.value} "
                    f"to {after.level.value}",
                )

            for field_name, label in (
                ("project_ids", "project"),
                ("resource_ids", "resource"),
                ("resource_types", "resource type"),
                ("resource_risks", "resource risk"),
                ("resource_sensitivities", "resource sensitivity"),
                ("environments", "environment"),
            ):
                before_values = tuple(getattr(before, field_name))
                after_values = tuple(getattr(after, field_name))
                if _set_scope_expands(before_values, after_values):
                    mark(
                        f"authority.{field_name}_expanded",
                        f"{prefix} {label} scope expanded",
                        expansion=True,
                    )
                elif before_values != after_values:
                    mark(
                        f"authority.{field_name}_changed",
                        f"{prefix} {label} scope changed without broadening",
                    )

            if (
                "production" not in {item.value for item in before.environments}
                and "production" in {item.value for item in after.environments}
            ):
                mark(
                    "authority.production_scope_added",
                    f"{prefix} added production environment authority",
                    expansion=True,
                )

            for field_name, label in (
                ("max_amount_usd", "monetary"),
                ("max_input_tokens", "input-token"),
                ("max_output_tokens", "output-token"),
                ("max_model_calls", "model-call"),
            ):
                before_limit = getattr(before, field_name)
                after_limit = getattr(after, field_name)
                if _limit_expands(before_limit, after_limit):
                    mark(
                        f"authority.{field_name}_increased",
                        f"{prefix} {label} ceiling increased or removed",
                        expansion=True,
                    )
                elif before_limit != after_limit:
                    mark(
                        f"authority.{field_name}_reduced",
                        f"{prefix} {label} ceiling reduced",
                    )

            if (
                AUTHORITY_AUTONOMY_RISK_RANK[after.max_autonomous_risk]
                > AUTHORITY_AUTONOMY_RISK_RANK[before.max_autonomous_risk]
            ):
                mark(
                    "authority.autonomy_risk_increased",
                    f"{prefix} autonomous-risk ceiling increased from "
                    f"{before.max_autonomous_risk.value} to "
                    f"{after.max_autonomous_risk.value}",
                    expansion=True,
                )
            elif (
                AUTHORITY_AUTONOMY_RISK_RANK[after.max_autonomous_risk]
                < AUTHORITY_AUTONOMY_RISK_RANK[before.max_autonomous_risk]
            ):
                mark(
                    "authority.autonomy_risk_reduced",
                    f"{prefix} autonomous-risk ceiling reduced",
                )

            if _approval_expands(before.approvals, after.approvals):
                mark(
                    "authority.approval_requirement_reduced",
                    f"{prefix} approval requirement became less restrictive",
                    expansion=True,
                )
            elif before.approvals != after.approvals:
                mark(
                    "authority.approval_requirement_tightened",
                    f"{prefix} approval requirement became more restrictive",
                )

    def binding_key(item):
        return item.id

    before_bindings = {binding_key(item): item for item in previous.bindings}
    after_bindings = {binding_key(item): item for item in candidate.bindings}
    for binding_id in sorted(set(after_bindings) - set(before_bindings)):
        mark(
            "authority.binding_added",
            f"role binding added: {binding_id}",
            expansion=True,
        )
    for binding_id in sorted(set(before_bindings) - set(after_bindings)):
        mark(
            "authority.binding_removed",
            f"role binding removed: {binding_id}",
        )
    for binding_id in sorted(set(before_bindings) & set(after_bindings)):
        before = before_bindings[binding_id]
        after = after_bindings[binding_id]
        if (
            before.role_id != after.role_id
            or before.subject_kind != after.subject_kind
            or before.subject_id != after.subject_id
            or before.organization_id != after.organization_id
            or before.workspace_id != after.workspace_id
        ):
            mark(
                "authority.binding_target_changed",
                f"role binding {binding_id} target/role changed",
                expansion=True,
            )
        elif _set_scope_expands(before.project_ids, after.project_ids):
            mark(
                "authority.binding_project_scope_expanded",
                f"role binding {binding_id} project scope expanded",
                expansion=True,
            )
        elif before.project_ids != after.project_ids:
            mark(
                "authority.binding_project_scope_reduced",
                f"role binding {binding_id} project scope reduced",
            )

    before_delegations = {item.id: item for item in previous.delegations}
    after_delegations = {item.id: item for item in candidate.delegations}
    for delegation_id in sorted(set(after_delegations) - set(before_delegations)):
        mark(
            "authority.delegation_added",
            f"delegation added: {delegation_id}",
            expansion=True,
        )
    for delegation_id in sorted(set(before_delegations) - set(after_delegations)):
        mark(
            "authority.delegation_removed",
            f"delegation removed: {delegation_id}",
        )
    for delegation_id in sorted(set(before_delegations) & set(after_delegations)):
        before = before_delegations[delegation_id]
        after = after_delegations[delegation_id]
        if (
            before.role_id != after.role_id
            or before.delegate_identity_id != after.delegate_identity_id
            or before.delegated_by_identity_id != after.delegated_by_identity_id
            or before.organization_id != after.organization_id
            or before.workspace_id != after.workspace_id
        ):
            mark(
                "authority.delegation_target_changed",
                f"delegation {delegation_id} target/role changed",
                expansion=True,
            )
        if _set_scope_expands(before.project_ids, after.project_ids):
            mark(
                "authority.delegation_project_scope_expanded",
                f"delegation {delegation_id} project scope expanded",
                expansion=True,
            )
        elif before.project_ids != after.project_ids:
            mark(
                "authority.delegation_project_scope_reduced",
                f"delegation {delegation_id} project scope reduced",
            )
        if after.expires_at > before.expires_at:
            mark(
                "authority.delegation_expiry_extended",
                f"delegation {delegation_id} expiry extended",
                expansion=True,
            )
        elif after.expires_at < before.expires_at:
            mark(
                "authority.delegation_expiry_reduced",
                f"delegation {delegation_id} expiry reduced",
            )

    if not classes:
        classes.append("authority.equivalent")
        reasons.append("authority catalog has no effective privilege-shape changes")

    return DefinitionPublicationGuardResult(
        change_classes=tuple(classes),
        reasons=tuple(reasons),
        requires_approval=sensitive,
    )


@dataclass(frozen=True, slots=True)
class _GrantPath:
    role_id: str
    grant: AuthorityGrant
    delegation_id: str | None = None
    expires_at: float | None = None


class AuthorityRoleService:
    """Deterministic operational Role evaluator over Definition Registry data."""

    def __init__(
        self,
        registry: DefinitionRegistryService,
        resources: ResourceCatalogService | None = None,
    ) -> None:
        self.registry = registry
        self.resources = resources

    def bootstrap(self) -> None:
        self.registry.bootstrap(
            [
                DefinitionDraftCreate(
                    definition_id=AUTHORITY_ROLE_CATALOG_ID,
                    kind=AUTHORITY_ROLE_CATALOG_KIND,
                    definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                    payload=authority_role_catalog_seed_payload(),
                    actor="bootstrap",
                    reason="bootstrap minimal canonical operational Role authority",
                )
            ]
        )

    def catalog_record(
        self,
        *,
        actor: AuthenticationActor,
        project_id: str | None = None,
        now: float | None = None,
    ):
        return self.registry.resolve(
            definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,
            context=DefinitionContext(
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                project_id=project_id,
            ),
            now=now,
        )

    @staticmethod
    def _binding_matches(binding, actor: AuthenticationActor, project_id: str | None) -> bool:
        if (
            binding.organization_id is not None
            and binding.organization_id != actor.organization_id
        ):
            return False
        if (
            binding.workspace_id is not None
            and binding.workspace_id != actor.workspace_id
        ):
            return False
        if binding.project_ids:
            if project_id is None or project_id not in binding.project_ids:
                return False
        if binding.subject_kind == "identity":
            return binding.subject_id == actor.identity_id
        return binding.subject_id in actor.team_ids

    @staticmethod
    def _delegation_matches(
        delegation,
        actor: AuthenticationActor,
        project_id: str | None,
        now: float,
    ) -> bool:
        if delegation.delegate_identity_id != actor.identity_id:
            return False
        if (
            delegation.organization_id is not None
            and delegation.organization_id != actor.organization_id
        ):
            return False
        if (
            delegation.workspace_id is not None
            and delegation.workspace_id != actor.workspace_id
        ):
            return False
        if delegation.expires_at <= now:
            return False
        if delegation.project_ids:
            if project_id is None or project_id not in delegation.project_ids:
                return False
        return True

    @staticmethod
    def _expand_role_paths(
        catalog: AuthorityRoleCatalogDefinition,
        roots: list[tuple[str, str | None, float | None]],
    ) -> tuple[_GrantPath, ...]:
        by_id = {item.id: item for item in catalog.roles}
        rows: list[_GrantPath] = []

        def walk(
            role_id: str,
            delegation_id: str | None,
            expires_at: float | None,
            seen: set[str],
        ) -> None:
            if role_id in seen:
                # Schema validation already rejects cycles. Keep evaluation
                # fail-closed even if an invalid payload somehow reaches here.
                raise ValueError("authority role inheritance cycle detected at runtime")
            role = by_id[role_id]
            next_seen = {*seen, role_id}
            for grant in role.grants:
                rows.append(
                    _GrantPath(
                        role_id=role_id,
                        grant=grant,
                        delegation_id=delegation_id,
                        expires_at=expires_at,
                    )
                )
            for inherited in sorted(role.inherits):
                walk(inherited, delegation_id, expires_at, next_seen)

        for role_id, delegation_id, expires_at in roots:
            walk(role_id, delegation_id, expires_at, set())

        unique: dict[tuple[str, str, str | None], _GrantPath] = {}
        for row in rows:
            unique[(row.role_id, row.grant.id, row.delegation_id)] = row
        return tuple(
            unique[key]
            for key in sorted(
                unique,
                key=lambda item: (item[0], item[1], item[2] or ""),
            )
        )

    def _resource_findings(
        self,
        grant: AuthorityGrant,
        request: AuthorityEvaluationRequest,
        actor: AuthenticationActor,
    ) -> list[str]:
        restrictions = bool(
            grant.resource_ids
            or grant.resource_types
            or grant.resource_risks
            or grant.resource_sensitivities
        )
        if restrictions and not request.resource_ids:
            return ["grant requires explicit resource targets"]
        if not request.resource_ids:
            return []
        if self.resources is None:
            return ["canonical Resource Catalog is unavailable"]

        resources = []
        for resource_id in request.resource_ids:
            try:
                resource = self.resources.get(resource_id, actor)
            except ResourceCatalogError:
                return [f"resource is unavailable or outside tenant scope: {resource_id}"]
            if resource.lifecycle in {
                ResourceLifecycle.DISABLED,
                ResourceLifecycle.DELETED,
            }:
                return [f"resource is not active for privileged use: {resource_id}"]
            resources.append(resource)

        findings: list[str] = []
        if grant.resource_ids:
            outside = [
                item.id for item in resources if item.id not in grant.resource_ids
            ]
            if outside:
                findings.append(
                    "resource targets outside grant: " + ", ".join(sorted(outside))
                )
        if grant.resource_types:
            outside = [
                item.id
                for item in resources
                if item.resource_type not in grant.resource_types
            ]
            if outside:
                findings.append(
                    "resource types outside grant: " + ", ".join(sorted(outside))
                )
        if grant.resource_risks:
            outside = [
                item.id for item in resources if item.risk not in grant.resource_risks
            ]
            if outside:
                findings.append(
                    "resource risk outside grant: " + ", ".join(sorted(outside))
                )
        if grant.resource_sensitivities:
            outside = [
                item.id
                for item in resources
                if item.sensitivity not in grant.resource_sensitivities
            ]
            if outside:
                findings.append(
                    "resource sensitivity outside grant: "
                    + ", ".join(sorted(outside))
                )
        return findings

    def _grant_findings(
        self,
        path: _GrantPath,
        request: AuthorityEvaluationRequest,
        actor: AuthenticationActor,
    ) -> list[str]:
        grant = path.grant
        findings: list[str] = []
        if grant.capability not in {"*", request.capability}:
            return ["capability does not match"]
        if AUTHORITY_LEVEL_RANK[grant.level] < AUTHORITY_LEVEL_RANK[request.level]:
            findings.append(
                f"grant level {grant.level.value} is below requested "
                f"{request.level.value}"
            )
        if grant.project_ids:
            if request.project_id is None:
                findings.append("grant requires explicit project scope")
            elif request.project_id not in grant.project_ids:
                findings.append("project is outside grant")
        if grant.environments:
            if request.environment is None:
                findings.append("grant requires explicit environment class")
            elif request.environment not in grant.environments:
                findings.append("environment is outside grant")
        for limit, observed, missing_reason, exceeded_reason in (
            (
                grant.max_amount_usd,
                request.amount_usd,
                "grant requires explicit monetary amount",
                "monetary amount exceeds grant",
            ),
            (
                grant.max_input_tokens,
                request.input_tokens,
                "grant requires explicit input-token budget",
                "input-token budget exceeds grant",
            ),
            (
                grant.max_output_tokens,
                request.output_tokens,
                "grant requires explicit output-token budget",
                "output-token budget exceeds grant",
            ),
            (
                grant.max_model_calls,
                request.model_calls,
                "grant requires explicit model-call budget",
                "model-call budget exceeds grant",
            ),
        ):
            if limit is None:
                continue
            if observed is None:
                findings.append(missing_reason)
            elif observed > limit:
                findings.append(exceeded_reason)
        if (
            AUTHORITY_AUTONOMY_RISK_RANK[request.autonomous_risk]
            > AUTHORITY_AUTONOMY_RISK_RANK[grant.max_autonomous_risk]
        ):
            findings.append("autonomous risk exceeds grant")

        requirement = grant.approvals
        if requirement.count:
            observed = list(request.approval_role_ids)
            if requirement.role_ids:
                observed = [
                    item for item in observed if item in requirement.role_ids
                ]
            if len(observed) < requirement.count:
                findings.append(
                    f"grant requires {requirement.count} qualifying approval(s)"
                )

        findings.extend(self._resource_findings(grant, request, actor))
        return findings

    @staticmethod
    def _deny(
        actor: AuthenticationActor,
        request: AuthorityEvaluationRequest,
        *,
        reasons: tuple[str, ...],
        definition_ref=None,
        matched_role_ids: tuple[str, ...] = (),
        delegation_ids: tuple[str, ...] = (),
        evaluated_at: float | None = None,
    ) -> AuthorityDecision:
        return AuthorityDecision(
            outcome=AuthorityDecisionOutcome.DENY,
            actor_identity_id=actor.identity_id,
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            request=request,
            definition_ref=definition_ref,
            matched_role_ids=matched_role_ids,
            delegation_ids=delegation_ids,
            reasons=reasons,
            evaluated_at=time.time() if evaluated_at is None else evaluated_at,
        )

    def evaluate(
        self,
        request: AuthorityEvaluationRequest,
        *,
        actor: AuthenticationActor,
        now: float | None = None,
    ) -> AuthorityDecision:
        evaluated_at = time.time() if now is None else now
        try:
            record = self.catalog_record(
                actor=actor,
                project_id=request.project_id,
                now=evaluated_at,
            )
            catalog = AuthorityRoleCatalogDefinition.model_validate(record.payload)
            definition_ref = reference_for(record)
        except (
            DefinitionCompatibilityError,
            DefinitionConflictError,
            DefinitionError,
            LookupError,
            ValueError,
        ) as exc:
            return self._deny(
                actor,
                request,
                reasons=(f"canonical authority definition unavailable: {exc}",),
                evaluated_at=evaluated_at,
            )

        roots: list[tuple[str, str | None, float | None]] = []
        for binding in catalog.bindings:
            if self._binding_matches(binding, actor, request.project_id):
                roots.append((binding.role_id, None, None))
        for delegation in catalog.delegations:
            if self._delegation_matches(
                delegation,
                actor,
                request.project_id,
                evaluated_at,
            ):
                roots.append(
                    (
                        delegation.role_id,
                        delegation.id,
                        delegation.expires_at,
                    )
                )

        if not roots:
            return self._deny(
                actor,
                request,
                definition_ref=definition_ref,
                reasons=("actor has no matching operational Role binding or delegation",),
                evaluated_at=evaluated_at,
            )

        paths = self._expand_role_paths(catalog, roots)
        matched_roles = tuple(sorted({item.role_id for item in paths}))
        delegation_ids = tuple(
            sorted(
                {
                    item.delegation_id
                    for item in paths
                    if item.delegation_id is not None
                }
            )
        )
        candidates: list[tuple[_GrantPath, list[str]]] = []
        for path in paths:
            if path.grant.capability not in {"*", request.capability}:
                continue
            candidates.append(
                (path, self._grant_findings(path, request, actor))
            )

        allowed = [
            item for item in candidates if not item[1]
        ]
        if allowed:
            path = sorted(
                (item[0] for item in allowed),
                key=lambda item: (
                    item.role_id,
                    item.grant.id,
                    item.delegation_id or "",
                ),
            )[0]
            return AuthorityDecision(
                outcome=AuthorityDecisionOutcome.ALLOW,
                actor_identity_id=actor.identity_id,
                organization_id=actor.organization_id,
                workspace_id=actor.workspace_id,
                request=request,
                definition_ref=definition_ref,
                matched_role_ids=(path.role_id,),
                matched_grant_ids=(path.grant.id,),
                delegation_ids=(
                    (path.delegation_id,)
                    if path.delegation_id is not None
                    else ()
                ),
                expires_at=path.expires_at,
                reasons=(
                    f"allowed by role {path.role_id} grant {path.grant.id}",
                ),
                evaluated_at=evaluated_at,
            )

        if not candidates:
            reasons = (
                f"no assigned Role grants capability {request.capability}",
            )
        else:
            reasons = tuple(
                f"{path.role_id}/{path.grant.id}: " + "; ".join(findings)
                for path, findings in candidates
            )
        return self._deny(
            actor,
            request,
            definition_ref=definition_ref,
            matched_role_ids=matched_roles,
            delegation_ids=delegation_ids,
            reasons=reasons,
            evaluated_at=evaluated_at,
        )


def install_authority_roles(
    registry: DefinitionRegistryService,
    resources: ResourceCatalogService | None = None,
) -> AuthorityRoleService:
    if not any(
        item["kind"] == AUTHORITY_ROLE_CATALOG_KIND
        and item["schema_version"] == AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION
        for item in registry.schemas.metadata()
    ):
        registry.register_schema(
            DefinitionKindSchema(
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                validate=validate_authority_role_catalog,
            )
        )
    service = AuthorityRoleService(registry, resources)
    service.bootstrap()
    registry.register_publication_guard(
        AUTHORITY_ROLE_CATALOG_KIND,
        assess_authority_role_publication,
    )
    return service
