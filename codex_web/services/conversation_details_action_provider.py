from __future__ import annotations

import time

from codex_web.action_providers import (
    ACTION_PROVIDER_CONTRACT, ActionCapability, ActionDefinition, ActionEvidence,
    ActionResult, ActionVerification, UnsupportedActionCapabilityError,
)


class ConversationDetailsActionProvider:
    """Protocol adapter over existing bot delivery, not a new message store.

    Slack cannot prove idempotent sends following an unknown HTTP outcome.
    Canonical ActionIntent owns admission, the single attempt and reconciliation.
    Registration declares capability; an operator binding/policy grants use.
    """

    contract_version = ACTION_PROVIDER_CONTRACT.current
    provider_type = "conversation-channel"
    provider_instance = "slack"

    def __init__(self, delivery, connections, projects):
        self.delivery = delivery
        self.connections = connections
        self.projects = projects

    def actions(self):
        return (ActionDefinition(action_id="conversation.details.reply", title="Reply with canonical thread details",
            capabilities=ActionCapability(idempotency=False, verification=True, evidence=True),
            credential_required=True, credential_purpose="bot_token", network_access=True,
            retry_max_attempts=1, required_authority=("conversation.reply",),
            expected_evidence=("provider-message-receipt",)),)

    def _target(self, request, provider_binding):
        binding_id = request.parameters.get("bot_binding_id")
        target = next((row for row in self.connections.load_bindings() if row.id == binding_id), None)
        if (target is None or target.provider != "slack" or target.project_id != request.project_id
                or target.connection_id != request.parameters.get("connection_id")
                or target.thread_id != request.parameters.get("thread_id")):
            raise ValueError("canonical details target is unavailable")
        project = self.projects.get(target.project_id)
        if (project.organization_id, project.workspace_id) != (request.organization_id, request.workspace_id):
            raise ValueError("canonical details target tenant changed")
        connection = self.connections.get(target.connection_id)
        if connection.project_id != project.id or provider_binding.project_id != project.id or provider_binding.credential_ref != connection.bot_token_secret_id:
            raise ValueError("canonical details connection/binding credential scope changed")
        return target

    async def prepare(self, request, *, binding):
        target = self._target(request, binding)
        return {"bot_binding_id": target.id, "thread_id": target.thread_id,
            "connection_id": target.connection_id, "project_id": target.project_id}

    async def execute(self, request, *, binding, credential=None):
        target = self._target(request, binding)
        if credential is None:
            raise ValueError("details credential boundary unavailable")
        started = time.time()
        response = await self.delivery.send_details(target, credential=credential)
        receipt = response.get("providerResponse") or {}
        if not response.get("sent") or not receipt.get("ts"):
            # A failed/missing response is not evidence of non-send. The
            # ActionIntent retains the unknown outcome without automatic retry.
            raise RuntimeError("details provider outcome requires reconciliation")
        return ActionResult(provider_binding_id=binding.id, action_id=request.action_id, status="succeeded",
            started_at=started, completed_at=time.time(), idempotency_key=request.idempotency_key,
            external_id=str(receipt["ts"]), output={"message_id": str(receipt["ts"]), "bot_binding_id": target.id},
            evidence=(ActionEvidence(evidence_type="provider-message-receipt", reference=str(receipt["ts"]),
                summary="Slack acknowledged the details reply."),))

    async def verify(self, result, *, binding, credential=None):
        return ActionVerification(verified=result.status == "succeeded" and bool(result.external_id),
            evidence=result.evidence, findings=() if result.external_id else ("provider receipt unavailable",))

    async def rollback(self, result, *, binding, credential=None):
        raise UnsupportedActionCapabilityError("rollback")
