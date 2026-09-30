"""ModelGateway binding administration; remote provider accounts remain external."""
from __future__ import annotations

import hashlib
import json

from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.model_gateway import ModelProviderUpsert
from codex_web.services.identity import AuthorizationError, IdentityService


def provider_fingerprint(provider):
    if provider is None:
        return 'none'
    return hashlib.sha256(json.dumps(provider.model_dump(mode='json'), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class ModelProviderAdministration:
    def __init__(self, service, *, agents=None, profiles=None):
        self.service = service
        self.agents = agents
        self.profiles = profiles

    def permissions(self, actor):
        try:
            self.service._require_admin(actor)
            if actor.principal_kind != PrincipalKind.SERVICE:
                IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)
            return True, None
        except AuthorizationError as exc:
            return False, str(exc)

    def catalog(self, actor):
        allowed, reason = self.permissions(actor)
        return {'schema_version': '1.0', 'can_manage': allowed, 'denial_reason': reason,
                'schema': ModelProviderUpsert.model_json_schema(),
                'items': [item.model_dump(mode='json') for item in self.service.list_providers(actor)]}

    def impact(self, provider_id, actor):
        state = self.service.store.load()
        provider = self.service._provider(state, provider_id, actor)
        allowed, reason = self.permissions(actor)
        result = dict(schema_version='1.0', available=True, can_manage=allowed, denial_reason=reason,
            provider=provider.model_dump(mode='json'), owner='model_gateway_binding', expected_revision=provider_fingerprint(provider),
            organization_id=actor.organization_id, workspace_id=actor.workspace_id,
            consumers=[], total=0, truncated=False, blockers=[],
            effect='Disabling this ModelGateway binding removes its models from subsequent inference routing and affects linked AgentProvider discovery. It does not cancel accepted invocations, remove model definitions or change remote provider accounts, subscriptions or credentials.',
            limitations='Explicit retained model/profile/provider/policy and invocation references are shown. Future unconstrained calls can also select this binding; this preview does not lock consumers. Active, degraded and disabled are supported; archive/delete/remote-account retirement are not.',
            coverage=['models', 'model policies', 'AgentProvider bindings', 'profile revisions', 'retained invocations'])
        def bounded(values):
            if len(values) > 5000:
                raise ValueError('model provider consumer scan exceeds supported bound')
            return values
        def add(kind, identifier, status=None, revision=None, project_id=None):
            result['total'] += 1
            if len(result['consumers']) < 100:
                result['consumers'].append(dict(kind=kind, id=identifier, status=status, revision=revision, project_id=project_id))
            else:
                result['truncated'] = True
        try:
            if self.agents is None or self.profiles is None:
                raise ValueError('consumer inventory unavailable')
            for item in bounded(state.models):
                if self.service._same_scope(item, actor) and item.provider_id == provider_id:
                    add('model', item.id, item.lifecycle.value)
            for item in bounded(state.policies):
                if self.service._same_scope(item, actor) and provider_id in item.allowed_provider_ids:
                    add('model_policy', actor.workspace_id, 'effective_constraint')
            for item in bounded(self.agents.list()):
                if self.service._same_scope(item, actor) and provider_id in item.model_provider_ids:
                    add('agent_provider', item.id, item.lifecycle.value, item.revision)
            for item in bounded(self.profiles.load().revisions):
                if self.service._same_scope(item, actor) and provider_id in item.model_policy.preferred_provider_ids:
                    add('profile', item.profile_id, item.lifecycle.value, item.revision)
            for item in bounded(state.invocations):
                if self.service._same_scope(item, actor) and item.selected_provider_id == provider_id:
                    add('invocation', item.id, item.status)
        except Exception:
            result.update(available=False, can_manage=False, consumers=[], total=None,
                blockers=['Canonical model dependency inventory is incomplete, invalid or exceeds its scan bound. Repair it before changing the binding.'])
        return result
