"""Administration of the existing entitlement state; provenance is never authority."""
from __future__ import annotations

import hashlib
import json
import time

from codex_web.entitlements import (
    CapabilityEntitlement, EntitlementChangePreview, EntitlementControl,
    EntitlementControlUpdate, QuotaPolicy,
)
from codex_web.identity import PrincipalKind
from codex_web.services.entitlements import EntitlementConflictError
from codex_web.services.identity import AuthorizationError


class EntitlementAdministration:
    def __init__(self, service):
        self.service = service

    def control(self, state, actor):
        return next((item for item in state.controls if self.service._same_scope(item, actor)), None)

    def revision(self, state, actor):
        document = {
            name: sorted((item.model_dump(mode="json") for item in getattr(state, name)
                          if self.service._same_scope(item, actor)),
                         key=lambda item: json.dumps(item, sort_keys=True))
            for name in ("settings", "capabilities", "quotas", "controls")
        }
        return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def require_control(self, state, actor, expected_revision=None):
        self.service._require_admin(actor)
        control = self.control(state, actor)
        if control and control.kind == "external" and not (
            actor.principal_kind == PrincipalKind.SERVICE
            and actor.identity_id == control.controller_identity_id
        ):
            raise AuthorizationError("entitlements are externally managed; contact the registered controller")
        if expected_revision is not None and expected_revision != self.revision(state, actor):
            raise EntitlementConflictError("entitlement configuration changed; reload and preview again")

    def set_control(self, payload: EntitlementControlUpdate, *, actor, expected_revision: str):
        # A human, provenance string, or generic entitlement-admin service cannot
        # claim commercial control. The credential boundary grants this scope.
        if actor.principal_kind != PrincipalKind.SERVICE or "entitlements:control" not in actor.service_scopes:
            raise AuthorizationError("entitlements:control service scope required")
        updated = []

        def apply(state):
            current = self.control(state, actor)
            if current and current.kind == "external" and current.controller_identity_id != actor.identity_id:
                raise AuthorizationError("only the registered controller can release or update external control")
            if expected_revision != self.revision(state, actor):
                raise EntitlementConflictError("entitlement configuration changed; reload before changing control")
            item = EntitlementControl(
                **payload.model_dump(), organization_id=actor.organization_id, workspace_id=actor.workspace_id,
                controller_identity_id=actor.identity_id if payload.kind == "external" else None,
                updated_by=actor.identity_id,
            )
            state.controls = [value for value in state.controls if not self.service._same_scope(value, actor)]
            state.controls.append(item)
            updated.append(item)
            return state

        self.service.store.update(apply)
        return updated[0]

    def snapshot(self, actor):
        state = self.service.store.load()
        control = self.control(state, actor)
        try:
            self.require_control(state, actor)
            can_manage, reason = True, None
        except AuthorizationError as exc:
            can_manage, reason = False, str(exc)
        now = time.time()
        capabilities = [item for item in state.capabilities if self.service._same_scope(item, actor)]
        quotas = [item for item in state.quotas if self.service._same_scope(item, actor)]
        return {
            "schema_version": "1.0", "revision": self.revision(state, actor),
            "organization_id": actor.organization_id, "workspace_id": actor.workspace_id,
            "scope": "workspace", "inheritance": "All Projects inherit this workspace configuration; no Project override exists.",
            "control": control.model_dump(mode="json") if control else {"kind": "local", "controller_identity_id": None, "guidance": "Managed by workspace administrators."},
            "can_manage": can_manage, "denial_reason": reason,
            "mode": self.service._mode(state, actor).value,
            "mode_source": "explicit" if self.service._settings(state, actor) else "self-hosted default",
            "capabilities": [{**item.model_dump(mode="json"), "decision": self.service._decision(state, actor, item.capability, at=now).model_dump(mode="json")} for item in capabilities],
            "quotas": [{**item.model_dump(mode="json"), "current_usage": self.service._usage_in_window(state, actor, item.metric, at=now, window=item.window)[0]} for item in quotas],
            "quota_schema": QuotaPolicy.model_json_schema(),
        }

    def preview(self, payload: EntitlementChangePreview, *, actor):
        state = self.service.store.load()
        self.require_control(state, actor)
        now = time.time()
        key = payload.key
        effect = "Applies to subsequent entitlement decisions. Authorization is always checked separately."
        previous = None
        current_usage = None
        if payload.kind == "mode":
            previous = {"mode": self.service._mode(state, actor).value}
            proposed = payload.mode.model_dump(mode="json")
            effect += " Enforced mode denies absent/disabled/expired capabilities; unlimited mode bypasses entitlement and quota enforcement."
        elif payload.kind == "capability":
            old = next((item for item in state.capabilities if self.service._same_scope(item, actor) and item.capability == key), None)
            previous = old.model_dump(mode="json") if old else None
            # Validate the final domain record, including activation/expiry order.
            proposed = CapabilityEntitlement(capability=key, organization_id=actor.organization_id, workspace_id=actor.workspace_id, updated_by=actor.identity_id, **payload.capability.model_dump()).model_dump(mode="json")
            effect += " Disabling or expiring this capability blocks its subsequent entitled operations in enforced mode; running operations are not cancelled."
        else:
            old = self.service._quota(state, actor, key)
            previous = old.model_dump(mode="json") if old else None
            if payload.kind == "retire_quota":
                if old is None:
                    raise ValueError("quota policy not found")
                proposed = None
                effect += " Retiring this limit removes its quota enforcement; capability and authority checks remain. Usage history is retained."
            else:
                proposed = payload.quota.model_dump(mode="json")
                current_usage = self.service._usage_in_window(state, actor, key, at=now, window=payload.quota.window)[0]
                effect += " A hard-stop limit below current usage blocks subsequent consumption in enforced mode. Other behaviors report exceedance without hard denial."
        return {
            "available": True, "expected_revision": self.revision(state, actor),
            "organization_id": actor.organization_id, "workspace_id": actor.workspace_id,
            "kind": payload.kind, "key": key, "previous": previous, "proposed": proposed,
            "current_usage": current_usage, "effect": effect,
            "usage_caveat": "Usage is live and may change after this preview; it is never reset by configuration changes.",
        }

    def retire_quota(self, metric, *, actor, expected_revision):
        def apply(state):
            self.require_control(state, actor, expected_revision)
            if self.service._quota(state, actor, metric) is None:
                raise ValueError("quota policy not found")
            state.quotas = [item for item in state.quotas if not (self.service._same_scope(item, actor) and item.metric == metric)]
            return state
        self.service.store.update(apply)
