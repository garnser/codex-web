from __future__ import annotations

import time
import uuid
from typing import Any

from codex_web.action_providers import (
    ACTION_PROVIDER_CONTRACT,
    ActionCapability,
    ActionDefinition,
    ActionEvidence,
    ActionProviderBinding,
    ActionRequest,
    ActionResult,
    ActionRiskClass,
    ActionVerification,
)
from codex_web.resources import ResourceType


class ReferenceActionProvider:
    """In-memory conformance provider with no external side effects."""

    contract_version = ACTION_PROVIDER_CONTRACT.current
    provider_type = "reference"
    provider_instance = "local-reference"

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self._idempotent_results: dict[str, ActionResult] = {}
        self._rollback_snapshots: dict[str, tuple[str, bool, Any]] = {}

    def actions(self) -> tuple[ActionDefinition, ...]:
        return (
            ActionDefinition(
                action_id="reference.set",
                title="Set reference value",
                description="Set a value in the in-memory reference provider.",
                capabilities=ActionCapability(
                    read=True,
                    prepare=True,
                    execute=True,
                    dry_run=True,
                    idempotency=True,
                    rollback=True,
                    verification=True,
                    progress=False,
                    evidence=True,
                ),
                risk_class=ActionRiskClass.LOW,
                required_resource_types=(ResourceType.OTHER,),
                required_authority=("action.reference.set",),
                credential_required=False,
                expected_evidence=("reference-state",),
                timeout_seconds=10.0,
                retry_max_attempts=2,
                reversible=True,
            ),
        )

    async def prepare(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
    ) -> dict[str, Any]:
        key = str(request.parameters.get("key") or "").strip()
        if not key:
            raise ValueError("reference.set requires parameter 'key'")
        return {
            "operation": "set",
            "key": key,
            "dry_run": request.dry_run,
            "resource_ids": list(request.resource_ids),
        }

    async def execute(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        if request.idempotency_key and request.idempotency_key in self._idempotent_results:
            return self._idempotent_results[request.idempotency_key]

        started = time.time()
        key = str(request.parameters.get("key") or "").strip()
        if not key:
            raise ValueError("reference.set requires parameter 'key'")
        value = request.parameters.get("value")
        existed = key in self.values
        previous = self.values.get(key)
        rollback_token = f"rollback-{uuid.uuid4().hex}"
        if not request.dry_run:
            self.values[key] = value
            self._rollback_snapshots[rollback_token] = (key, existed, previous)

        result = ActionResult(
            provider_binding_id=binding.id,
            action_id=request.action_id,
            status="dry_run" if request.dry_run else "succeeded",
            started_at=started,
            completed_at=time.time(),
            idempotency_key=request.idempotency_key,
            output={"key": key, "value": value},
            evidence=(
                ActionEvidence(
                    evidence_type="reference-state",
                    reference=key,
                    summary="Reference value prepared." if request.dry_run else "Reference value updated.",
                ),
            ),
            rollback_token=None if request.dry_run else rollback_token,
        )
        if request.idempotency_key:
            self._idempotent_results[request.idempotency_key] = result
        return result

    async def verify(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
    ) -> ActionVerification:
        key = str(result.output.get("key") or "")
        if result.status == "dry_run":
            return ActionVerification(
                verified=True,
                evidence=result.evidence,
                findings=("dry-run result has no persistent state to verify",),
            )
        expected = result.output.get("value")
        verified = key in self.values and self.values.get(key) == expected
        return ActionVerification(
            verified=verified,
            evidence=(
                ActionEvidence(
                    evidence_type="reference-state",
                    reference=key,
                    summary="Reference provider state matches result." if verified else "Reference provider state differs.",
                ),
            ),
            findings=() if verified else ("reference state mismatch",),
        )

    async def rollback(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        if not result.rollback_token or result.rollback_token not in self._rollback_snapshots:
            raise ValueError("rollback token is missing or unknown")
        started = time.time()
        key, existed, previous = self._rollback_snapshots.pop(result.rollback_token)
        if existed:
            self.values[key] = previous
        else:
            self.values.pop(key, None)
        return ActionResult(
            provider_binding_id=binding.id,
            action_id=result.action_id,
            status="rolled_back",
            started_at=started,
            completed_at=time.time(),
            idempotency_key=result.idempotency_key,
            output={"key": key, "restored": existed, "value": previous},
            evidence=(
                ActionEvidence(
                    evidence_type="reference-state",
                    reference=key,
                    summary="Reference provider rollback completed.",
                ),
            ),
        )
