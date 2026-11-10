"""Canonical Project policy for governed Artifact delivery (#2008)."""
from codex_web.artifact_sharing_policy import KIND, POLICY_ID, VERSION, ArtifactSharingPolicy
from codex_web.definitions import DefinitionContext, reference_for
from codex_web.services.definitions import DefinitionKindSchema, DefinitionPublicationAssessment


def validate_policy(payload):
    return ArtifactSharingPolicy.model_validate(payload).model_dump(mode='json')


def assess_publication(active, candidate):
    """Any new/broader directional rule uses existing independent approval."""
    old_rules = ArtifactSharingPolicy.model_validate(active.payload).rules if active else ()
    new = ArtifactSharingPolicy.model_validate(candidate.payload)
    reasons = []
    for rule in new.rules:
        prior = next((r for r in old_rules if r.id == rule.id), None)
        if (prior is None or not set(rule.source_resource_ids).issubset(prior.source_resource_ids)
                or not set(rule.target_resource_ids).issubset(prior.target_resource_ids)
                or rule.max_content_bytes > prior.max_content_bytes):
            reasons.append('New or broadened governed Artifact sharing rule: ' + rule.id)
    return DefinitionPublicationAssessment(bool(reasons), tuple(reasons))


class ArtifactSharingPolicyService:
    def __init__(self, registry, resources):
        self.registry, self.resources = registry, resources
        registry.register_schema(DefinitionKindSchema(KIND, VERSION, validate_policy,
            assess_publish=assess_publication, publication_guard=self._publication_guard))

    def _publication_guard(self, record, publisher):
        policy = ArtifactSharingPolicy.model_validate(record.payload)
        if record.scope_type != 'project' or record.scope_id != policy.project_id:
            raise ValueError('Artifact sharing policy requires explicit Project scope')
        # Definition owner authenticates/authorizes publication; policy cannot
        # contain an identity, grant, approval assertion or execution instruction.

    def resolve(self, assignment, actor, *, now):
        record = self.registry.resolve(definition_id=POLICY_ID, kind=KIND,
            context=DefinitionContext(organization_id=assignment.organization_id,
                workspace_id=assignment.workspace_id, project_id=assignment.project_id), now=now)
        policy = ArtifactSharingPolicy.model_validate(record.payload)
        if (record.scope_type != 'project' or record.scope_id != assignment.project_id
                or (policy.organization_id,policy.workspace_id,policy.project_id) !=
                    (assignment.organization_id,assignment.workspace_id,assignment.project_id)
                or (actor.organization_id,actor.workspace_id) != (policy.organization_id,policy.workspace_id)):
            raise ValueError('No exact Project sharing policy is published')
        return policy, reference_for(record)


def scoped_work(broker, assignment, ref):
    from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
    try:
        state = broker.work_items.state_machine._work_item_state(ref)
    except KeyError:
        state = None
    if (state is None or (state.organization_id,state.workspace_id,state.project_id) !=
            (assignment.organization_id,assignment.workspace_id,assignment.project_id)):
        raise ControlPlaneBrokerDeniedError('Canonical sharing Work is missing or outside tenant/Project')
    return state
