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


class ExampleFeatureFlagProvider:
    """Synthetic reversible ActionProvider with no external side effects."""

    contract_version = ACTION_PROVIDER_CONTRACT.current
    provider_type = "example-feature-flags"
    provider_instance = "demo"

    def __init__(self) -> None:
        self.flags: dict[str, bool] = {}
        self._idempotent_results: dict[str, ActionResult] = {}
        self._rollback: dict[str, tuple[str, bool, bool | None]] = {}

    def actions(self) -> tuple[ActionDefinition, ...]:
        return (
            ActionDefinition(
                action_id="example.feature-flag.set",
                title="Set example feature flag",
                description="Set a synthetic feature flag in the example provider.",
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
                required_authority=("action.example.feature-flag.set",),
                credential_required=False,
                expected_evidence=("feature-flag-state",),
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
        del binding
        name = self._flag_name(request)
        enabled = self._enabled(request)
        return {
            "operation": "set-feature-flag",
            "name": name,
            "enabled": enabled,
            "dry_run": request.dry_run,
        }

    async def execute(
        self,
        request: ActionRequest,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        del credential
        if (
            request.idempotency_key
            and request.idempotency_key in self._idempotent_results
        ):
            return self._idempotent_results[request.idempotency_key]

        started = time.time()
        name = self._flag_name(request)
        enabled = self._enabled(request)
        existed = name in self.flags
        previous = self.flags.get(name)
        rollback_token = f"example-rollback-{uuid.uuid4().hex}"

        if not request.dry_run:
            self.flags[name] = enabled
            self._rollback[rollback_token] = (name, existed, previous)

        result = ActionResult(
            provider_binding_id=binding.id,
            action_id=request.action_id,
            status="dry_run" if request.dry_run else "succeeded",
            started_at=started,
            completed_at=time.time(),
            idempotency_key=request.idempotency_key,
            output={"name": name, "enabled": enabled},
            evidence=(
                ActionEvidence(
                    evidence_type="feature-flag-state",
                    reference=name,
                    summary=(
                        "Feature flag change prepared."
                        if request.dry_run
                        else "Feature flag state updated."
                    ),
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
        del binding
        if result.status == "dry_run":
            return ActionVerification(
                verified=True,
                evidence=result.evidence,
                findings=("dry-run result intentionally has no persistent state",),
            )
        name = str(result.output["name"])
        expected = bool(result.output["enabled"])
        verified = self.flags.get(name) is expected
        return ActionVerification(
            verified=verified,
            evidence=(
                ActionEvidence(
                    evidence_type="feature-flag-state",
                    reference=name,
                    summary=(
                        "Observed state matches the requested state."
                        if verified
                        else "Observed state differs from the requested state."
                    ),
                ),
            ),
            findings=() if verified else ("feature flag state mismatch",),
        )

    async def rollback(
        self,
        result: ActionResult,
        *,
        binding: ActionProviderBinding,
        credential: str | None = None,
    ) -> ActionResult:
        del credential
        token = result.rollback_token
        if not token or token not in self._rollback:
            raise ValueError("rollback token is missing or unknown")

        started = time.time()
        name, existed, previous = self._rollback.pop(token)
        if existed:
            self.flags[name] = bool(previous)
        else:
            self.flags.pop(name, None)

        return ActionResult(
            provider_binding_id=binding.id,
            action_id=result.action_id,
            status="rolled_back",
            started_at=started,
            completed_at=time.time(),
            idempotency_key=result.idempotency_key,
            output={
                "name": name,
                "restored": existed,
                "enabled": previous,
            },
            evidence=(
                ActionEvidence(
                    evidence_type="feature-flag-state",
                    reference=name,
                    summary="Feature flag rollback completed.",
                ),
            ),
        )

    @staticmethod
    def _flag_name(request: ActionRequest) -> str:
        name = str(request.parameters.get("name") or "").strip()
        if not name:
            raise ValueError("feature-flag action requires parameter 'name'")
        return name

    @staticmethod
    def _enabled(request: ActionRequest) -> bool:
        enabled = request.parameters.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("feature-flag action requires boolean parameter 'enabled'")
        return enabled
