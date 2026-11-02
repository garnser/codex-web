"""Current governed MR review basis, without inventing principal eligibility."""
from __future__ import annotations

from fastapi import HTTPException

from codex_web.artifact_evidence import EvidenceRequirement
from codex_web.services.artifact_evidence import ArtifactNotFoundError
from codex_web.mr_closure_acceptance import MRClosureAcceptanceError, MRClosureScope


class MRPolicyAcceptanceError(MRClosureAcceptanceError):
    """A closed policy/provenance refusal, not an approval or merge decision."""


class CurrentPolicyMRAcceptanceOwner:
    """Compose existing owners for the closure gate's finite acceptance callback.

    Principal eligibility is deliberately not inferred from labels, an Artifact
    producer, membership, or a caller's list of identities. Until a canonical
    owner supplies that provenance, principal review remains unavailable here.
    """

    def __init__(self, *, execution_roles, assignment_reviews, evidence=None, work_items=None,
                 require_principal_independence: bool = False):
        if type(require_principal_independence) is not bool:
            raise TypeError('principal independence requirement must be a server-owned boolean')
        self.roles = execution_roles
        self.evidence = evidence
        self.work_items = work_items
        self.assignment_reviews = assignment_reviews
        self.require_principal_independence = require_principal_independence

    def ci_requirement(self, scope: MRClosureScope, *, actor):
        """Current optional CI readiness contract, never a missing-owner fallback."""
        if (scope.organization_id, scope.workspace_id) != (actor.organization_id, actor.workspace_id):
            raise MRPolicyAcceptanceError('mr_acceptance_actor_tenant_mismatch')
        if self.roles is None:
            raise MRPolicyAcceptanceError('mr_acceptance_current_ci_policy_owner_unavailable')
        return self.roles.resolve_mr_ci_readiness_policy(
            organization_id=scope.organization_id, workspace_id=scope.workspace_id,
            project_id=scope.project_id)

    def _requirements(self, scope, actor):
        if (self.evidence is None or self.work_items is None
                or self.evidence.work_item_host is None):
            raise MRPolicyAcceptanceError('mr_acceptance_work_requirements_owner_unavailable')
        try:
            state = self.work_items.state_machine._work_item_state(scope.work_item_ref)
        except HTTPException as error:
            if error.status_code not in {403, 404}:
                raise
            raise MRPolicyAcceptanceError('mr_acceptance_work_requirements_scope_unavailable') from error
        identity = (state.ref, state.organization_id, state.workspace_id, state.project_id)
        if (identity != (scope.work_item_ref, scope.organization_id, scope.workspace_id, scope.project_id)
                or scope.repository_resource_id not in state.resource_ids):
            raise MRPolicyAcceptanceError('mr_acceptance_work_requirements_scope_mismatch')
        try:
            requirements = self.evidence.work_item_requirements(scope.work_item_ref, actor=actor)
        except ArtifactNotFoundError as error:
            raise MRPolicyAcceptanceError('mr_acceptance_work_requirements_scope_unavailable') from error
        if (any(type(item) is not EvidenceRequirement for item in requirements)
                or requirements != tuple(state.execution.evidence_requirements)):
            raise MRPolicyAcceptanceError('mr_acceptance_work_requirements_changed')
        return identity, tuple(state.resource_ids), requirements

    def _review_records(self, artifact_id, verification_id, actor):
        artifact = self.evidence.get_artifact(artifact_id, actor=actor)
        verification = self.evidence.get_verification(verification_id, actor=actor)
        if len(verification.evidence_ids) != 1:
            raise MRPolicyAcceptanceError('mr_acceptance_review_evidence_unavailable')
        review = self.evidence.get_evidence(verification.evidence_ids[0], actor=actor)
        return artifact, verification, review

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
        work_snapshot = self._requirements(scope, actor)
        records = self._review_records(artifact_id, verification_id, actor)
        artifact, _, review = records
        # Independence belongs to each applicable evidence requirement. An
        # independent CI requirement is not an independent QA requirement.
        strict_review = any(item.independent_verification and item.evidence_type == 'review'
            and review.evidence_type == 'review' and review.result in item.accepted_results
            and (item.artifact_type is None or item.artifact_type == artifact.artifact_type)
            and (item.provider is None or item.provider == review.provider)
            for item in work_snapshot[2])
        # This owner re-reads the managed manifest, receipt, actual STARTs,
        # historical publication, current catalog, and all canonical metadata.
        # It keeps Verification.independent unchanged and applies stricter
        # principal-independence requirements even under assignment-role policy.
        manifest = self.assignment_reviews.validate_canonical_receipt(
            scope, artifact_id, verification_id, actor=actor,
            require_principal_independence=self.require_principal_independence or strict_review)
        evaluation = self.evidence.evaluate(scope.work_item_ref, work_snapshot[2], actor=actor)
        if not evaluation.satisfied:
            raise MRPolicyAcceptanceError('mr_acceptance_work_evidence_unsatisfied')
        # Both receipt validation and canonical evidence evaluation may read
        # managed content. Repeat the actual scoped Work contract afterwards.
        if (self._requirements(scope, actor) != work_snapshot
                or self._review_records(artifact_id, verification_id, actor) != records):
            raise MRPolicyAcceptanceError('mr_acceptance_work_requirements_or_review_changed')
        # The delegated check includes content I/O. A publication during that
        # observation cannot silently replace the policy selected for this gate.
        current, current_reference = self.roles.resolve_catalog(**context)
        if current_reference != reference or current != catalog:
            raise MRPolicyAcceptanceError('mr_acceptance_current_policy_changed')
        return manifest

    __call__ = acceptance
