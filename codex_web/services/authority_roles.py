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
from codex_web.services.authority_publication import assess_authority_catalog_publication
from codex_web.definitions import (
    DefinitionContext,
    DefinitionDraftCreate,
    DefinitionScope,
    reference_for,
)
from codex_web.identity import AuthenticationActor
from codex_web.services.definitions import (
    DefinitionCompatibilityError,
    DefinitionConflictError,
    DefinitionError,
    DefinitionKindSchema,
    DefinitionRegistryService,
)
from codex_web.resources import ResourceLifecycle
from codex_web.services.resources import (
    ResourceCatalogError,
    ResourceCatalogService,
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

    def can_observe_reads(self) -> bool:
        if type(self) is not AuthorityRoleService:
            return False
        for name, original in _CANONICAL_AUTHORITY_BOUND_READS.items():
            method = getattr(self, name, None)
            if (getattr(method, "__self__", None) is not self
                    or getattr(method, "__func__", None) is not original):
                return False
        if any(getattr(self, name, None) is not original
               for name, original in _CANONICAL_AUTHORITY_STATIC_READS.items()):
            return False
        definition_check = getattr(self.registry, "can_observe_reads", None)
        resource_check = getattr(self.resources, "can_observe_reads", None)
        return (callable(definition_check) and definition_check()
                and (self.resources is None or callable(resource_check) and resource_check()))

    def _require_readers(self, definitions=None, resources=None):
        if definitions is None and resources is None:
            return
        if not _CANONICAL_AUTHORITY_CAN_OBSERVE(self):
            raise ValueError("Authority read observations require canonical owners")
        if definitions is not None:
            self.registry.require_read_observation(definitions)
        if resources is not None:
            if self.resources is None:
                raise ValueError("Authority Resource reader owner is unavailable")
            self.resources.require_read_observation(resources)

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
        _definitions=None,
    ):
        self._require_readers(_definitions)
        definitions = self.registry if _definitions is None else _definitions
        return definitions.resolve(
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
            if role.lifecycle == "disabled":
                return
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

    def role_ids_for_actor(
        self,
        actor: AuthenticationActor,
        *,
        project_id: str | None = None,
        now: float | None = None,
    ) -> tuple[str, ...]:
        """Return live operational Role IDs for deterministic eligibility checks."""

        evaluated_at = time.time() if now is None else float(now)
        record = self.catalog_record(
            actor=actor,
            project_id=project_id,
            now=evaluated_at,
        )
        catalog = AuthorityRoleCatalogDefinition.model_validate(record.payload)
        roots: list[tuple[str, str | None, float | None]] = []
        for binding in catalog.bindings:
            if self._binding_matches(binding, actor, project_id):
                roots.append((binding.role_id, None, None))
        for delegation in catalog.delegations:
            if self._delegation_matches(
                delegation,
                actor,
                project_id,
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
            return ()
        return tuple(
            sorted(
                {
                    path.role_id
                    for path in self._expand_role_paths(catalog, roots)
                }
            )
        )

    def _resource_findings(
        self,
        grant: AuthorityGrant,
        request: AuthorityEvaluationRequest,
        actor: AuthenticationActor,
        *,
        _resources=None,
    ) -> list[str]:
        self._require_readers(resources=_resources)
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
                resources_owner = self.resources if _resources is None else _resources
                resource = resources_owner.get(resource_id, actor)
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
        *,
        _resources=None,
    ) -> list[str]:
        self._require_readers(resources=_resources)
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

        options = {"_resources": _resources} if _resources is not None else {}
        findings.extend(self._resource_findings(grant, request, actor, **options))
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

    def _evaluate_record(
        self,
        record,
        request: AuthorityEvaluationRequest,
        *,
        actor: AuthenticationActor,
        evaluated_at: float,
        _resources=None,
    ) -> AuthorityDecision:
        self._require_readers(resources=_resources)
        catalog = AuthorityRoleCatalogDefinition.model_validate(record.payload)
        definition_ref = reference_for(record)

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
            options = {"_resources": _resources} if _resources is not None else {}
            candidates.append((path, self._grant_findings(path, request, actor, **options)))

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

    def evaluate_record(
        self,
        record_id: str,
        request: AuthorityEvaluationRequest,
        *,
        actor: AuthenticationActor,
        now: float | None = None,
    ) -> AuthorityDecision:
        """Evaluate one exact authority Definition revision with runtime semantics.

        This is intended for deterministic pre-publication simulation. It does
        not make the revision effective and enforces the candidate's canonical
        tenant/project slot before evaluating grants.
        """
        evaluated_at = time.time() if now is None else now
        try:
            record = self.registry.get_record(record_id)
            if (
                record.kind != AUTHORITY_ROLE_CATALOG_KIND
                or record.definition_id != AUTHORITY_ROLE_CATALOG_ID
            ):
                raise ValueError("record is not the canonical authority Role catalog")
            if (
                record.scope_type == DefinitionScope.ORGANIZATION
                and record.scope_id != actor.organization_id
            ):
                raise ValueError("authority candidate is outside actor organization")
            if (
                record.scope_type == DefinitionScope.WORKSPACE
                and record.scope_id != actor.workspace_id
            ):
                raise ValueError("authority candidate is outside actor workspace")
            if record.scope_type == DefinitionScope.PROJECT:
                if request.project_id is None or record.scope_id != request.project_id:
                    raise ValueError("authority candidate requires its exact project context")
            return self._evaluate_record(
                record,
                request,
                actor=actor,
                evaluated_at=evaluated_at,
            )
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

    def evaluate(
        self,
        request: AuthorityEvaluationRequest,
        *,
        actor: AuthenticationActor,
        now: float | None = None,
        _definitions=None,
        _resources=None,
    ) -> AuthorityDecision:
        self._require_readers(_definitions, _resources)
        evaluated_at = time.time() if now is None else now
        try:
            definition_options = {"_definitions": _definitions} if _definitions is not None else {}
            record = self.catalog_record(
                actor=actor,
                project_id=request.project_id,
                now=evaluated_at,
                **definition_options,
            )
            resource_options = {"_resources": _resources} if _resources is not None else {}
            return self._evaluate_record(
                record,
                request,
                actor=actor,
                evaluated_at=evaluated_at,
                **resource_options,
            )
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


_CANONICAL_AUTHORITY_BOUND_READS = {
    name: getattr(AuthorityRoleService, name)
    for name in ("catalog_record", "role_ids_for_actor", "_resource_findings", "_grant_findings",
                 "_evaluate_record", "evaluate_record", "evaluate", "_require_readers", "can_observe_reads")
}
_CANONICAL_AUTHORITY_STATIC_READS = {
    name: getattr(AuthorityRoleService, name)
    for name in ("_binding_matches", "_delegation_matches", "_expand_role_paths", "_deny")
}
_CANONICAL_AUTHORITY_CAN_OBSERVE = AuthorityRoleService.can_observe_reads


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
                assess_publish=assess_authority_catalog_publication,
            )
        )
    service = AuthorityRoleService(registry, resources)
    service.bootstrap()
    return service
