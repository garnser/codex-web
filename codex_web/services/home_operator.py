from __future__ import annotations

import time
from urllib.parse import quote

from codex_web.identity import AuthenticationActor
from codex_web.models import HomeOperatorBinding, HomeOperatorUpdate
from codex_web.services.identity import IdentityError, IdentityService


class HomeOperatorConflict(ValueError):
    pass


class HomeOperatorService:
    """One durable Home navigation binding, never an execution grant or dispatcher."""

    def __init__(self, *, projects, thread_scope, thread_index, settings, clock=time.time):
        self.projects = projects
        self.thread_scope = thread_scope
        self.thread_index = thread_index
        self.settings = settings
        self.clock = clock

    def status(self, actor: AuthenticationActor) -> dict:
        project = self.projects.get('home', actor.tenant)
        binding = project.home_operator
        try:
            IdentityService.require_admin(actor)
            can_configure = True
        except IdentityError:
            can_configure = False
        result = {'can_configure': can_configure, 'binding': binding.model_dump() if binding else None,
                  'status': 'unconfigured', 'thread': None}
        if not binding or not binding.thread_id:
            return result
        # Revalidate scope on every read. A retained pointer never grants access
        # to a moved, foreign, archived or otherwise unavailable conversation.
        try:
            self.thread_scope.for_thread(binding.thread_id, actor, 'home')
            if self.thread_index.replacement_thread_id(binding.thread_id, project_id='home',
                    organization_id=actor.organization_id, workspace_id=actor.workspace_id):
                raise HomeOperatorConflict('Operator conversation was replaced; explicit reselection required')
            row = self.thread_index.get(binding.thread_id)
            if row is None or row.archived:
                raise HomeOperatorConflict('Operator conversation is unavailable')
            settings = self.settings.get(binding.thread_id)
            result['thread'] = {
                'id': row.id, 'name': row.name,
                'execution_profile_id': settings.execution_profile_id,
                'sandbox': settings.sandbox,
                'approval_policy': settings.approval_policy,
                'href': f'/projects/home?thread={quote(row.id, safe="")}#workspace/threads',
            }
            result['status'] = 'configured' if settings.execution_profile_id else 'profile_required'
        except Exception:
            # Do not leak foreign thread metadata or silently select a replacement.
            result['status'] = 'unavailable'
        return result

    def configure(self, payload: HomeOperatorUpdate, actor: AuthenticationActor) -> dict:
        IdentityService.require_admin(actor)
        self.projects.get('home', actor.tenant)
        if payload.thread_id:
            self.thread_scope.for_thread(payload.thread_id, actor, 'home')
            if self.thread_index.replacement_thread_id(payload.thread_id, project_id='home',
                    organization_id=actor.organization_id, workspace_id=actor.workspace_id):
                raise HomeOperatorConflict('Select the current conversation explicitly after replacement')
            row = self.thread_index.get(payload.thread_id)
            if row is None or row.archived or not row.name.strip():
                raise HomeOperatorConflict('Select an existing named, unarchived Home conversation')
            if not self.settings.get(payload.thread_id).execution_profile_id:
                raise HomeOperatorConflict('Explicitly select the conversation execution profile first')
        binding = HomeOperatorBinding(thread_id=payload.thread_id,
            selected_by=actor.identity_id, revision=payload.expected_revision + 1, updated_at=self.clock())
        try:
            self.projects.repository.update_home_operator(
                organization_id=actor.organization_id, workspace_id=actor.workspace_id,
                expected_revision=payload.expected_revision, binding=binding)
        except ValueError as exc:
            raise HomeOperatorConflict(str(exc)) from exc
        return self.status(actor)
