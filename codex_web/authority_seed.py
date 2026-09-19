from __future__ import annotations

from codex_web.authority import (
    AuthorityApprovalRequirement,
    AuthorityAutonomyRisk,
    AuthorityGrant,
    AuthorityLevel,
    AuthorityRoleBinding,
    AuthorityRoleCatalogDefinition,
    AuthorityRoleDefinition,
)
from codex_web.identity import DEFAULT_HUMAN_IDENTITY_ID


def authority_role_catalog_seed_payload() -> dict:
    """Minimal compatibility seed for the trusted local administrator.

    This is bootstrap data only. Once the Definition Registry contains a
    canonical authority-role catalog, bootstrap never overwrites it.
    """

    return AuthorityRoleCatalogDefinition(
        roles=(
            AuthorityRoleDefinition(
                id="local-admin",
                name="Local administrator",
                description=(
                    "Compatibility authority for the trusted single-user local "
                    "administrator. Hosted/operator roles should be explicitly "
                    "defined and bound rather than inheriting this role."
                ),
                grants=(
                    AuthorityGrant(
                        id="local-admin.all",
                        capability="*",
                        level=AuthorityLevel.APPROVE,
                        max_autonomous_risk=AuthorityAutonomyRisk.CRITICAL,
                        approvals=AuthorityApprovalRequirement(count=0),
                    ),
                ),
            ),
        ),
        bindings=(
            AuthorityRoleBinding(
                id="local-admin-binding",
                role_id="local-admin",
                subject_kind="identity",
                subject_id=DEFAULT_HUMAN_IDENTITY_ID,
                organization_id="local",
                workspace_id="default",
            ),
        ),
    ).model_dump(mode="json")
