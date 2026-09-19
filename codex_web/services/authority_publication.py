from __future__ import annotations

from codex_web.authority import (
    AUTHORITY_AUTONOMY_RISK_RANK,
    AUTHORITY_LEVEL_RANK,
    AuthorityEnvironment,
    AuthorityGrant,
    AuthorityRoleCatalogDefinition,
)
from codex_web.definitions import DefinitionRecord
from codex_web.services.definitions import DefinitionPublicationAssessment


def _broadened_values(old_values, new_values) -> bool:
    old = set(old_values)
    new = set(new_values)
    if not old:
        return False
    if not new:
        return True
    return not new.issubset(old)


def _broadened_optional_scope(old: str | None, new: str | None) -> bool:
    if old is None:
        return False
    return new is None or new != old


def _limit_increased(old: float | int | None, new: float | int | None) -> bool:
    if old is None:
        return False
    if new is None:
        return True
    return new > old


def _grant_expansion_reasons(
    role_id: str,
    old: AuthorityGrant | None,
    new: AuthorityGrant,
) -> list[str]:
    prefix = f"role {role_id} grant {new.id}"
    if old is None:
        return [f"{prefix}: new authority grant"]

    reasons: list[str] = []
    if old.capability != new.capability:
        reasons.append(
            f"{prefix}: capability changed from {old.capability} to {new.capability}"
        )
    if (
        AUTHORITY_LEVEL_RANK[new.level]
        > AUTHORITY_LEVEL_RANK[old.level]
    ):
        reasons.append(
            f"{prefix}: authority level increased from {old.level.value} to {new.level.value}"
        )
    for field_name, label in (
        ("project_ids", "project scope"),
        ("resource_ids", "resource ID scope"),
        ("resource_types", "resource type scope"),
        ("resource_risks", "resource risk scope"),
        ("resource_sensitivities", "resource sensitivity scope"),
        ("environments", "environment scope"),
    ):
        if _broadened_values(
            getattr(old, field_name),
            getattr(new, field_name),
        ):
            reasons.append(f"{prefix}: {label} broadened")

    if (
        AuthorityEnvironment.PRODUCTION in new.environments
        and AuthorityEnvironment.PRODUCTION not in old.environments
        and bool(old.environments)
    ):
        reasons.append(f"{prefix}: production environment authority added")

    for field_name, label in (
        ("max_amount_usd", "monetary ceiling"),
        ("max_input_tokens", "input-token ceiling"),
        ("max_output_tokens", "output-token ceiling"),
        ("max_model_calls", "model-call ceiling"),
    ):
        if _limit_increased(
            getattr(old, field_name),
            getattr(new, field_name),
        ):
            reasons.append(f"{prefix}: {label} increased or became unbounded")

    if (
        AUTHORITY_AUTONOMY_RISK_RANK[new.max_autonomous_risk]
        > AUTHORITY_AUTONOMY_RISK_RANK[old.max_autonomous_risk]
    ):
        reasons.append(
            f"{prefix}: autonomous-risk ceiling increased from "
            f"{old.max_autonomous_risk.value} to {new.max_autonomous_risk.value}"
        )

    if new.approvals.count < old.approvals.count:
        reasons.append(f"{prefix}: required approval count decreased")
    if _broadened_values(old.approvals.role_ids, new.approvals.role_ids):
        reasons.append(f"{prefix}: qualifying approver Role scope broadened")
    return reasons


def assess_authority_catalog_publication(
    active: DefinitionRecord | None,
    candidate: DefinitionRecord,
) -> DefinitionPublicationAssessment:
    new_catalog = AuthorityRoleCatalogDefinition.model_validate(candidate.payload)
    old_catalog = (
        AuthorityRoleCatalogDefinition.model_validate(active.payload)
        if active is not None
        else AuthorityRoleCatalogDefinition(roles=())
    )

    reasons: list[str] = []
    old_roles = {item.id: item for item in old_catalog.roles}
    for role in new_catalog.roles:
        old_role = old_roles.get(role.id)
        if old_role is None:
            if role.grants:
                reasons.append(f"role {role.id}: new Role with authority grants")
            if role.inherits:
                reasons.append(f"role {role.id}: new inherited authority")
            continue

        if old_role.lifecycle == "disabled" and role.lifecycle != "disabled":
            reasons.append(
                f"role {role.id}: disabled authority Role reactivated"
            )
        elif old_role.lifecycle == "deprecated" and role.lifecycle == "active":
            reasons.append(
                f"role {role.id}: deprecated authority Role returned to active"
            )

        added_parents = set(role.inherits) - set(old_role.inherits)
        if added_parents:
            reasons.append(
                f"role {role.id}: inherited authority added from "
                + ", ".join(sorted(added_parents))
            )

        old_grants = {item.id: item for item in old_role.grants}
        for grant in role.grants:
            reasons.extend(
                _grant_expansion_reasons(
                    role.id,
                    old_grants.get(grant.id),
                    grant,
                )
            )

    old_bindings = {item.id: item for item in old_catalog.bindings}
    for binding in new_catalog.bindings:
        old = old_bindings.get(binding.id)
        if old is None:
            reasons.append(
                f"binding {binding.id}: new {binding.subject_kind} Role binding"
            )
            continue
        if (
            old.role_id != binding.role_id
            or old.subject_kind != binding.subject_kind
            or old.subject_id != binding.subject_id
        ):
            reasons.append(f"binding {binding.id}: authority target or Role changed")
        if _broadened_optional_scope(old.organization_id, binding.organization_id):
            reasons.append(f"binding {binding.id}: organization scope broadened")
        if _broadened_optional_scope(old.workspace_id, binding.workspace_id):
            reasons.append(f"binding {binding.id}: workspace scope broadened")
        if _broadened_values(old.project_ids, binding.project_ids):
            reasons.append(f"binding {binding.id}: project scope broadened")

    old_delegations = {item.id: item for item in old_catalog.delegations}
    for delegation in new_catalog.delegations:
        old = old_delegations.get(delegation.id)
        if old is None:
            reasons.append(
                f"delegation {delegation.id}: new delegated authority"
            )
            continue
        if (
            old.role_id != delegation.role_id
            or old.delegate_identity_id != delegation.delegate_identity_id
        ):
            reasons.append(
                f"delegation {delegation.id}: delegate or Role changed"
            )
        if _broadened_optional_scope(
            old.organization_id,
            delegation.organization_id,
        ):
            reasons.append(
                f"delegation {delegation.id}: organization scope broadened"
            )
        if _broadened_optional_scope(
            old.workspace_id,
            delegation.workspace_id,
        ):
            reasons.append(
                f"delegation {delegation.id}: workspace scope broadened"
            )
        if _broadened_values(old.project_ids, delegation.project_ids):
            reasons.append(f"delegation {delegation.id}: project scope broadened")
        if delegation.expires_at > old.expires_at:
            reasons.append(f"delegation {delegation.id}: expiry extended")

    normalized = tuple(sorted(dict.fromkeys(reasons)))
    return DefinitionPublicationAssessment(
        requires_independent_approval=bool(normalized),
        reasons=normalized,
    )
