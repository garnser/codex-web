"""Current governed MR review basis, without inventing principal eligibility."""
from __future__ import annotations

from codex_web.mr_closure_acceptance import MRClosureAcceptanceError, MRClosureScope


class MRPolicyAcceptanceError(MRClosureAcceptanceError):
    """A closed policy/provenance refusal, not an approval or merge decision."""


class CurrentPolicyMRAcceptanceOwner:
    """Compose existing owners for the closure gate's finite acceptance callback.

    Principal eligibility is deliberately not inferred from labels, an Artifact
    producer, membership, or a caller's list of identities. Until a canonical
    owner supplies that provenance, principal review remains unavailable here.
    """

    def __init__(self, *, execution_roles, assignment_reviews,
                 require_principal_independence: bool = False):
        if type(require_principal_independence) is not bool:
            raise TypeError('principal independence requirement must be a server-owned boolean')
        self.roles = execution_roles
        self.assignment_reviews = assignment_reviews
        self.require_principal_independence = require_principal_independence

    def acceptance(self, scope: MRClosureScope, artifact_id: str, verification_id: str, *, actor):
        if (scope.organization_id, scope.workspace_id) != (actor.organization_id, actor.workspace_id):
            raise MRPolicyAcceptanceError('mr_acceptance_actor_tenant_mismatch')
        if self.roles is None:
            raise MRPolicyAcceptanceError('mr_acceptance_current_policy_owner_unavailable')
        context = dict(organization_id=scope.organization_id, workspace_id=scope.workspace_id,
                       project_id=scope.project_id)
        catalog, reference = self.roles.resolve_catalog(**context)
        policy = catalog.mr_review_policy
        if policy is None or policy.basis == 'principal':
            raise MRPolicyAcceptanceError('mr_acceptance_principal_eligibility_owner_unavailable')
        if (reference.definition_schema_version != '1.1' or policy.basis != 'assignment_role'
                or self.assignment_reviews is None):
            raise MRPolicyAcceptanceError('mr_acceptance_assignment_policy_owner_unavailable')
        # This owner re-reads the managed manifest, receipt, actual STARTs,
        # historical publication, current catalog, and all canonical metadata.
        # It keeps Verification.independent unchanged and applies stricter
        # principal-independence requirements even under assignment-role policy.
        manifest = self.assignment_reviews.validate_canonical_receipt(
            scope, artifact_id, verification_id, actor=actor,
            require_principal_independence=self.require_principal_independence)
        # The delegated check includes content I/O. A publication during that
        # observation cannot silently replace the policy selected for this gate.
        current, current_reference = self.roles.resolve_catalog(**context)
        if current_reference != reference or current != catalog:
            raise MRPolicyAcceptanceError('mr_acceptance_current_policy_changed')
        return manifest

    __call__ = acceptance
