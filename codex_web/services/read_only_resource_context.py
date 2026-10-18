"""Use existing identity/authority/catalog/WorkItem/lease boundaries only."""
from codex_web.authority import AuthorityEvaluationRequest, AuthorityLevel, AuthorityDecisionOutcome
from codex_web.identity import TenantScope
from codex_web.resources import ResourceLifecycle, ResourceType
from codex_web.services.identity import AuthorizationError


class ReadOnlyResourceContextAdmission:
    def __init__(self, identity, authority, resources, work_item, profiles):
        self.identity,self.authority,self.resources,self.work_item=identity,authority,resources,work_item
        self.profiles=profiles

    def __call__(self, context, project, profile):
        if context is None:
            return
        actor=self.identity.actor_for_identity(context.requester_identity_id,scope=TenantScope(organization_id=project.organization_id,workspace_id=project.workspace_id))
        # The original current requester authorizes READ, not the privileged
        # internal execution controller. Project association adds no authority.
        item=self.work_item(context.work_item_ref)
        if (item is None or profile is None or item.current_owner!=profile.profile_id
                or item.project_id!=project.id
                or (item.organization_id,item.workspace_id)!=(actor.organization_id,actor.workspace_id)):
            raise AuthorizationError('Current Work Item owner/recipient and pinned profile required for resource context')
        current,_=self.profiles.resolve_for_execution(profile.profile_id,actor=actor,project_id=project.id)
        if (current.lifecycle!='active' or current.revision!=getattr(profile,'profile_revision',getattr(profile,'revision',None))
                or current.record_id!=getattr(profile,'profile_record_id',getattr(profile,'record_id',None))):
            raise AuthorizationError('Pinned current Agent Profile required for Resource context')
        associated=set(self.resources.resource_ids_for_project(project))
        for resource_id in context.read_only_resource_ids:
            resource=self.resources.get(resource_id,actor)
            if (resource.resource_type==ResourceType.REPOSITORY or resource.lifecycle!=ResourceLifecycle.ACTIVE
                    or resource_id not in associated):
                raise AuthorizationError('Active nonrepository Resource must belong to current Project')
            decision=self.authority.evaluate(AuthorityEvaluationRequest(capability='resource.read',
                level=AuthorityLevel.READ,project_id=project.id,resource_ids=(resource_id,)),actor=actor)
            if decision.outcome!=AuthorityDecisionOutcome.ALLOW:
                raise AuthorizationError('Current original requester Resource READ authority required')
