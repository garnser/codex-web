from __future__ import annotations

from codex_web.action_providers import (
    ACTION_PROVIDER_CONTRACT,
    ActionProvider,
    ActionProviderBinding,
    ActionRequest,
    ActionResult,
    ActionVerification,
)


class ActionProviderConformanceError(RuntimeError):
    pass


class ActionProviderConformanceSuite:
    """Shared provider-neutral contract checks for ActionProvider implementations."""

    def validate_contract(self, provider: ActionProvider) -> None:
        ACTION_PROVIDER_CONTRACT.require(provider.contract_version)
        if not str(provider.provider_type).strip():
            raise ActionProviderConformanceError("provider_type must not be empty")
        if not str(provider.provider_instance).strip():
            raise ActionProviderConformanceError("provider_instance must not be empty")
        actions = provider.actions()
        ids = [item.action_id for item in actions]
        if len(ids) != len(set(ids)):
            raise ActionProviderConformanceError("action ids must be unique")
        for action in actions:
            if action.capabilities.rollback and not action.reversible:
                raise ActionProviderConformanceError(
                    f"{action.action_id}: rollback capability must be declared reversible"
                )
            if action.reversible and not action.capabilities.rollback:
                raise ActionProviderConformanceError(
                    f"{action.action_id}: reversible action requires rollback capability"
                )
            if action.credential_required and not action.credential_purpose:
                raise ActionProviderConformanceError(
                    f"{action.action_id}: credential requirement needs purpose"
                )

    async def exercise(
        self,
        provider: ActionProvider,
        *,
        binding: ActionProviderBinding,
        request: ActionRequest,
    ) -> tuple[ActionResult, ActionVerification | None, ActionResult | None]:
        self.validate_contract(provider)
        definitions = {item.action_id: item for item in provider.actions()}
        definition = definitions.get(request.action_id)
        if definition is None:
            raise ActionProviderConformanceError("request action is not declared")
        if definition.capabilities.prepare:
            plan = await provider.prepare(request, binding=binding)
            if not isinstance(plan, dict):
                raise ActionProviderConformanceError("prepare must return provider-neutral mapping")
        result = await provider.execute(request, binding=binding, credential=None)
        if not isinstance(result, ActionResult):
            raise ActionProviderConformanceError("execute must return ActionResult")
        verification = None
        if definition.capabilities.verification:
            verification = await provider.verify(result, binding=binding)
            if not isinstance(verification, ActionVerification):
                raise ActionProviderConformanceError("verify must return ActionVerification")
        rollback = None
        if (
            definition.capabilities.rollback
            and result.status == "succeeded"
            and result.rollback_token
        ):
            rollback = await provider.rollback(result, binding=binding, credential=None)
            if not isinstance(rollback, ActionResult) or rollback.status != "rolled_back":
                raise ActionProviderConformanceError("rollback must return rolled_back ActionResult")
        return result, verification, rollback
