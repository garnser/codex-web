"""Revalidate selected canonical Memory at native turn admission, without models."""
from __future__ import annotations

import hashlib

from fastapi import HTTPException

from codex_web.identity import PrincipalKind, TenantScope
from codex_web.ux_briefs import UxBriefSelection, UxBriefTurnBinding


class UxBriefTurnService:
    def __init__(self, briefs, identity):
        self.briefs = briefs
        self.identity = identity

    @staticmethod
    def _deny():
        raise HTTPException(status_code=409, detail={
            "code": "memory_context_denied",
            "message": "Selected context changed or is unavailable; inspect the brief again.",
        })

    def _preview(self, selection, project, actor):
        if actor is None or (actor.organization_id, actor.workspace_id) != (
            project.organization_id, project.workspace_id
        ):
            self._deny()
        if actor.principal_kind == PrincipalKind.SERVICE and not set(actor.service_scopes).intersection(
            {"memory:read", "memory:write", "memory:admin"}
        ):
            self._deny()
        try:
            preview = self.briefs.preview(selection, project_id=project.id, actor=actor)
        except (RuntimeError, ValueError, KeyError):
            self._deny()
        if not preview["context"] or preview["omitted"]:
            self._deny()
        return preview

    def bind(self, selection, *, project, actor):
        preview = self._preview(selection, project, actor)
        exact = UxBriefSelection.model_validate(preview["selection"])
        return UxBriefTurnBinding(selection=exact, project_id=project.id,
            organization_id=actor.organization_id, workspace_id=actor.workspace_id,
            identity_id=actor.identity_id, principal_kind=actor.principal_kind,
            service_token_id=actor.service_token_id,
            context_sha256=hashlib.sha256(preview["context"].encode()).hexdigest())

    def prepare(self, binding, *, project):
        if (binding.project_id, binding.organization_id, binding.workspace_id) != (
            project.id, project.organization_id, project.workspace_id
        ):
            self._deny()
        try:
            actor = self.identity.actor_for_identity(binding.identity_id, scope=TenantScope(
                organization_id=binding.organization_id, workspace_id=binding.workspace_id))
        except (RuntimeError, ValueError, KeyError):
            self._deny()
        if actor.identity_id != binding.identity_id or actor.principal_kind != binding.principal_kind:
            self._deny()
        if actor.principal_kind == PrincipalKind.SERVICE:
            token = next((row for row in self.identity.store.load().service_tokens
                if row.id == binding.service_token_id), None)
            if (token is None or token.service_identity_id != actor.identity_id
                or (token.organization_id, token.workspace_id) != (actor.organization_id, actor.workspace_id)
                or token.revoked_at is not None
                or (token.expires_at is not None and token.expires_at <= self.briefs.memory.clock())):
                self._deny()
            actor = actor.model_copy(update={"service_token_id": token.id,
                "service_scopes": tuple(token.scopes)})
        preview = self._preview(binding.selection, project, actor)
        if (hashlib.sha256(preview["context"].encode()).hexdigest() != binding.context_sha256
            or UxBriefSelection.model_validate(preview["selection"]) != binding.selection):
            self._deny()
        return preview, actor

    def delivered(self, preview, actor, *, thread_id, execution_id, native_turn_id,
                  provider_id, runtime_id, message):
        """Record an accepted native start in the existing retrieval audit only."""
        if not native_turn_id:
            return
        receipt = {"delivery_proven": True, "thread_id": thread_id,
            "execution_id": execution_id, "native_turn_id": native_turn_id,
            "provider_id": str(provider_id), "runtime_id": str(runtime_id),
            "context_sha256": hashlib.sha256(preview["context"].encode()).hexdigest(),
            "message_sha256": hashlib.sha256(message.encode()).hexdigest()}
        def update(state):
            for index, row in enumerate(state.retrieval_runs):
                if (row.id == preview["retrieval_id"] and row.organization_id == actor.organization_id
                    and row.workspace_id == actor.workspace_id):
                    state.retrieval_runs[index] = row.model_copy(update={
                        "filters": {**row.filters, "native_delivery": receipt}})
                    return state
            raise ValueError("retrieval audit unavailable")
        self.briefs.memory.store.update_state(update)
