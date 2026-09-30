"""Bounded read-only consumer projection over canonical provider dependencies."""
from __future__ import annotations

from codex_web.agent_providers import AgentProviderUpsert
from codex_web.services.identity import AuthorizationError


class AgentProviderAdministration:
    SCAN_LIMIT = 5000
    DISPLAY_LIMIT = 100

    def __init__(self, service, *, runtimes=None, sessions=None, profiles=None,
                 configuration=None, definitions=None, projects=None, resources=None):
        self.service = service
        self.runtimes = runtimes
        self.sessions = sessions
        self.profiles = profiles
        self.configuration = configuration
        self.definitions = definitions
        self.projects = projects
        self.resources = resources

    def permissions(self, actor):
        try:
            self.service._require_admin(actor)
            return True, None
        except AuthorizationError as exc:
            return False, str(exc)

    def catalog(self, actor):
        allowed, reason = self.permissions(actor)
        return {
            "schema_version": "1.0", "can_manage": allowed, "denial_reason": reason,
            "organization_id": actor.organization_id, "workspace_id": actor.workspace_id,
            "schema": AgentProviderUpsert.model_json_schema(),
            "runtime_ownership": "Runtime adapter registrations are code-owned deployment state. Change the adapter deployment to add or remove registrations; this editor changes canonical provider bindings only.",
            "items": [item.model_dump(mode="json") for item in self.service.list(actor)],
        }

    def impact(self, provider_id, actor):
        provider = self.service.get(provider_id, actor)
        allowed, reason = self.permissions(actor)
        result = {
            "schema_version": "1.0", "available": True, "provider": provider.model_dump(mode="json"),
            "can_manage": allowed and not provider.synthesized_from_model_gateway,
            "denial_reason": reason, "expected_revision": provider.revision,
            "owner": "model_gateway" if provider.synthesized_from_model_gateway else "agent_provider",
            "organization_id": actor.organization_id, "workspace_id": actor.workspace_id,
            "consumers": [], "total": 0, "truncated": False, "blockers": [],
            "coverage": ["runtime registrations", "retained sessions", "profile revisions", "published routing definitions", "published routing preferences", "linked models and model policies"],
            "effect": "Disabling the binding removes it from subsequent agent discovery/routing. It does not cancel running sessions, disable a linked ModelGateway provider, remove a runtime deployment or change a remote provider account.",
            "limitations": "Explicit retained references are an impact projection, not a lock on future consumers. Unconstrained routing can also select this provider. Only active/disabled binding transitions exist; archive, delete and remote-account retirement are not supported.",
        }
        def add(kind, identifier, *, revision=None, project_id=None, status=None):
            result['total'] += 1
            if len(result['consumers']) < self.DISPLAY_LIMIT:
                result['consumers'].append(dict(kind=kind, id=identifier, revision=revision, project_id=project_id, status=status))
            else:
                result['truncated'] = True

        def bounded(values):
            if len(values) > self.SCAN_LIMIT:
                raise ValueError('consumer scan exceeds supported bound')
            return values

        try:
            if any(value is None for value in (self.runtimes, self.sessions, self.profiles, self.configuration, self.definitions, self.projects, self.resources, self.service.model_gateway)):
                raise ValueError('consumer inventory is not attached')
            project_ids = {item.id for item in bounded(self.projects.list(actor.tenant))}
            # Resource service performs canonical tenant visibility filtering.
            resource_ids = {item.id for item in bounded(self.resources.list(actor=actor))}
            def visible(item):
                kind = item.scope_type.value
                return kind in {'global', 'deployment'} or (
                    kind == 'organization' and item.scope_id == actor.organization_id
                    or kind == 'workspace' and item.scope_id == actor.workspace_id
                    or kind == 'project' and item.scope_id in project_ids
                    or kind == 'resource' and item.scope_id in resource_ids
                )
            registrations = [item for item in bounded(self.runtimes.list_registrations()) if item.provider_id == provider_id]
            runtime_ids = {item.runtime_id for item in registrations}
            for item in registrations:
                add('runtime', item.runtime_id, revision=item.capability_revision, status='deployment_owned')
            for item in bounded(self.sessions.list()):
                if self.service._same_scope(item, actor) and item.provider_id == provider_id:
                    add('session', item.id, project_id=item.project_id, status=item.status.value)
            def references(policy):
                return (provider_id in (*policy.allowed_provider_ids, *policy.preferred_provider_ids)
                        or bool(runtime_ids.intersection((*policy.allowed_runtime_ids, *policy.preferred_runtime_ids))))
            for item in bounded(self.profiles.load().revisions):
                if self.service._same_scope(item, actor) and references(item.runtime_policy):
                    add('profile', item.profile_id, revision=item.revision, status=item.lifecycle.value)
            from codex_web.agent_routing_definitions import AGENT_ROUTING_POLICY_KIND, AgentRoutingPolicyDefinition
            for item in bounded(self.definitions.load()):
                if item.kind == AGENT_ROUTING_POLICY_KIND and item.lifecycle.value == 'published' and visible(item):
                    if any(references(role) for role in AgentRoutingPolicyDefinition.model_validate(item.payload).roles):
                        add('definition', item.definition_id, revision=item.revision, project_id=item.scope_id if item.scope_type.value == 'project' else None, status='published')
            for item in bounded(self.configuration.load()):
                if item.state.value != 'published' or not visible(item):
                    continue
                if ((item.key == 'agent.routing.preferred_provider_ids' and provider_id in item.value)
                        or (item.key == 'agent.routing.preferred_runtime_ids' and runtime_ids.intersection(item.value))):
                    add('configuration', item.key, revision=item.revision, project_id=item.scope_id if item.scope_type.value == 'project' else None, status='published')
            model_state = self.service.model_gateway.load()
            linked = set(provider.model_provider_ids)
            for item in bounded(model_state.models):
                if self.service._same_scope(item, actor) and item.provider_id in linked:
                    add('model', item.id, status=item.lifecycle.value)
            for item in bounded(model_state.policies):
                if self.service._same_scope(item, actor) and linked.intersection(item.allowed_provider_ids):
                    add('model_policy', actor.workspace_id, status='effective_constraint')
        except Exception:
            # Never turn corrupt/incomplete dependency state into "zero users".
            result.update(available=False, can_manage=False, consumers=[], total=None,
                          blockers=['Canonical dependency inventory is incomplete, invalid or exceeds its scan bound. Refresh or repair the owning inventory before changing this binding.'])
        return result
