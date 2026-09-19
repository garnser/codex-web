from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from codex_web.approval_requests import (
    ApprovalDecisionOutcome,
    ApprovalConsumeRequest,
    ApprovalDecisionSubmit,
    ApprovalRequestCreate,
    ApprovalRequestStatus,
    ApprovalRequirement,
    ApprovalTarget,
)
from codex_web.identity import (
    AuthenticationActor,
    AuthenticationAssurance,
    MembershipRole,
)
from codex_web.models import ApprovalSlackMessage
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.services.codex_worker_session import AssignmentBoundCodexSessionManager
from codex_web.storage.approval_requests import ApprovalRequestNotFoundError


class ApprovalService:
    """Compatibility transport for native Codex approvals.

    Native app-server pending maps are transport state only. When a canonical
    ApprovalRequest service is configured, every interactive native prompt is
    first projected into the durable approval domain and all decisions must
    pass that domain before the JSON-RPC response is sent.
    """

    def __init__(
        self,
        host: Any,
        *,
        assignment_sessions: AssignmentBoundCodexSessionManager | None = None,
        canonical: ApprovalRequestService | None = None,
        canonical_requester: AuthenticationActor | None = None,
        compatibility_actor: AuthenticationActor | None = None,
    ) -> None:
        self.host = host
        self.assignment_sessions = assignment_sessions
        self.canonical = canonical
        self.canonical_requester = canonical_requester
        self.compatibility_actor = compatibility_actor

        # ApprovalService is already composed by application.py. Rebind the
        # historical mutation seams here so compatibility surfaces share the
        # same transport and, when configured, the canonical approval gate.
        host._remember_approval_message = self.remember_message
        host._forget_approval_messages = self.forget_messages
        host._pending_codex_approvals = self.pending
        host._respond_codex_approval = (
            self.respond_compatibility if canonical is not None else self.respond
        )
        if canonical is not None:
            host._register_canonical_approval_request = self.register_native_request

    def _approval_runtimes(self):
        yield self.host.codex
        manager = self.assignment_sessions
        if manager is None:
            return
        for session in manager.sessions.values():
            if session.runtime is not None:
                yield session.runtime

    def pending(self) -> dict[int | str, dict[str, Any]]:
        result: dict[int | str, dict[str, Any]] = {}
        for runtime in self._approval_runtimes():
            for request_id, request in runtime.pending_approvals.items():
                if request_id in result:
                    raise RuntimeError(
                        f"duplicate canonical approval request id: {request_id}"
                    )
                result[request_id] = request
        return result

    async def respond(
        self,
        request_id: int | str,
        result: dict[str, Any],
    ) -> None:
        """Send a response to the owning native runtime without policy logic."""

        for runtime in self._approval_runtimes():
            if request_id in runtime.pending_approvals:
                await CodexAgentRuntimeAdapter(runtime).respond_approval(
                    request_id,
                    result,
                )
                return
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Approval request not found")

    def list(self) -> list[dict[str, Any]]:
        return list(self.pending().values())

    @staticmethod
    def _canonical_request_id(request_id: int | str) -> str:
        digest = hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()[:32]
        return f"approval-codex-{digest}"

    @staticmethod
    def _native_digest(request: dict[str, Any]) -> str:
        material = {
            "method": request.get("method"),
            "params": request.get("params") or {},
        }
        encoded = json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def register_native_request(
        self,
        request: dict[str, Any],
    ):
        canonical = self.canonical
        requester = self.canonical_requester
        if canonical is None or requester is None:
            return None

        request_id = request.get("id")
        if request_id is None:
            raise ValueError("native approval request requires a public id")
        canonical_id = self._canonical_request_id(request_id)
        method = str(request.get("method") or "approval")
        target = ApprovalTarget(
            operation=f"codex.{method}",
            object_type="codex_server_request",
            object_id=str(request_id),
            target_digest=self._native_digest(request),
        )

        try:
            existing = canonical.store.get(canonical_id)
        except ApprovalRequestNotFoundError:
            existing = None
        if existing is not None:
            if existing.target_fingerprint != target.fingerprint():
                raise RuntimeError(
                    "native approval id was reused for a different operation"
                )
            return existing

        summary = getattr(self.host, "_approval_summary", None)
        if callable(summary):
            reason = str(summary(request))
        else:
            reason = f"Native Codex approval requested for {method}"

        return await canonical.create(
            ApprovalRequestCreate(
                target=target,
                reason=reason,
                policy_source="codex:approval-policy",
                authority_source="codex:native-runtime",
                requirement=ApprovalRequirement(
                    quorum=1,
                    required_assurance=AuthenticationAssurance.MFA,
                    membership_roles=(
                        MembershipRole.OWNER,
                        MembershipRole.ADMIN,
                        MembershipRole.APPROVER,
                    ),
                    allow_self_approval=False,
                ),
            ),
            requester=requester,
            request_id=canonical_id,
        )

    @staticmethod
    def _outcome_for_native_decision(decision: str) -> ApprovalDecisionOutcome:
        if decision in {
            "accept",
            "acceptForSession",
            "approved",
            "approved_for_session",
        }:
            return ApprovalDecisionOutcome.APPROVE
        return ApprovalDecisionOutcome.REJECT

    @classmethod
    def _outcome_for_native_result(
        cls,
        result: dict[str, Any],
    ) -> ApprovalDecisionOutcome:
        decision = str(result.get("decision") or "")
        if decision:
            return cls._outcome_for_native_decision(decision)
        if "strictAutoReview" in result:
            return (
                ApprovalDecisionOutcome.REJECT
                if bool(result.get("strictAutoReview"))
                else ApprovalDecisionOutcome.APPROVE
            )
        return ApprovalDecisionOutcome.REJECT

    async def _consume_native_approval(
        self,
        canonical_request,
        *,
        actor: AuthenticationActor,
        request_id: int | str,
    ):
        assert self.canonical is not None
        return await self.canonical.consume(
            canonical_request.id,
            ApprovalConsumeRequest(
                target=canonical_request.target,
                idempotency_key=f"native-consume:{request_id}",
                resulting_operation_reference=f"codex-rpc:{request_id}",
            ),
            actor=actor,
        )

    async def respond_compatibility(
        self,
        request_id: int | str,
        result: dict[str, Any],
    ) -> None:
        """Gate historical Slack/native resolution through canonical approval."""

        canonical = self.canonical
        actor = self.compatibility_actor
        if canonical is None or actor is None:
            raise RuntimeError(
                "canonical approval actor is required for native compatibility"
            )
        request = self.pending().get(request_id)
        if request is None:
            await self.respond(request_id, result)
            return
        canonical_request = await self.register_native_request(request)
        assert canonical_request is not None

        outcome = self._outcome_for_native_result(result)
        digest = hashlib.sha256(
            json.dumps(
                result,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:24]
        if canonical_request.status == ApprovalRequestStatus.APPROVED:
            updated = canonical_request
        else:
            updated = await canonical.decide(
                canonical_request.id,
                ApprovalDecisionSubmit(
                    outcome=outcome,
                    reason="Native compatibility approval decision",
                    idempotency_key=f"native-compat:{request_id}:{digest}",
                ),
                actor=actor,
            )
        if updated.status == ApprovalRequestStatus.APPROVED:
            await self._consume_native_approval(
                updated,
                actor=actor,
                request_id=request_id,
            )
            await self.respond(request_id, result)
            return
        if updated.status == ApprovalRequestStatus.REJECTED:
            await self.respond(request_id, result)

    async def decide(
        self,
        request_id: str,
        decision: str,
        *,
        actor: AuthenticationActor | None = None,
    ) -> dict[str, bool]:
        normalized_id = self.host._request_id_value(request_id)
        canonical = self.canonical
        if canonical is None:
            return await self.host._resolve_approval_request(
                normalized_id,
                decision,
                actor="Codex Web",
            )

        current_actor = actor or self.compatibility_actor
        if current_actor is None:
            raise RuntimeError("authenticated approval actor is required")
        native_request = self.pending().get(normalized_id)
        if native_request is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Approval request not found")

        canonical_request = await self.register_native_request(native_request)
        assert canonical_request is not None
        outcome = self._outcome_for_native_decision(decision)
        if canonical_request.status == ApprovalRequestStatus.APPROVED:
            updated = canonical_request
        else:
            updated = await canonical.decide(
                canonical_request.id,
                ApprovalDecisionSubmit(
                    outcome=outcome,
                    reason=f"Native Codex decision: {decision}",
                    idempotency_key=(
                        f"native-ui:{normalized_id}:{current_actor.identity_id}:{decision}"
                    ),
                ),
                actor=current_actor,
            )
        if updated.status == ApprovalRequestStatus.APPROVED:
            updated = await self._consume_native_approval(
                updated,
                actor=current_actor,
                request_id=normalized_id,
            )
        if updated.status in {
            ApprovalRequestStatus.CONSUMED,
            ApprovalRequestStatus.REJECTED,
        }:
            result = self.host._approval_result(
                str(native_request.get("method") or "approval"),
                decision,
            )
            await self.respond(normalized_id, result)
            self.forget_messages(normalized_id)
        return {"ok": True}

    def remember_message(
        self,
        request_id: int | str,
        *,
        connection_id: str,
        channel: str,
        message_ts: str,
        context: str,
        thread_id: str | None = None,
    ) -> None:
        messages = self.host._load_approval_messages()
        key = str(request_id)
        current = messages.setdefault(key, [])
        if any(
            item.connection_id == connection_id
            and item.channel == channel
            and item.message_ts == message_ts
            for item in current
        ):
            return
        current.append(
            ApprovalSlackMessage(
                request_id=key,
                connection_id=connection_id,
                channel=channel,
                message_ts=message_ts,
                context=context,
                thread_id=thread_id,
                created_at=time.time(),
            )
        )
        self.host._save_approval_messages(messages)

    def forget_messages(self, request_id: int | str) -> None:
        messages = self.host._load_approval_messages()
        if messages.pop(str(request_id), None) is not None:
            self.host._save_approval_messages(messages)
