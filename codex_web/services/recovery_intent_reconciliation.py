"""Read-only Recovery observation through existing guarded ActionIntent writes.

No provider execution, Recovery operation, retry queue or new store is used here.
"""
import asyncio
from codex_web.action_intents import ActionIntentStatus, ActionDecisionOutcome
from codex_web.action_providers import ActionResult
from codex_web.security import SecurityDecisionOutcome
from codex_web.services.action_providers import ActionRequirementError
from codex_web.services.recovery_reconciliation import validate_proof

ACTION = "recovery.scheduled-firing.reconcile"
PENDING = {ActionIntentStatus.UNCERTAIN, ActionIntentStatus.REQUIRES_RECONCILIATION}


async def reconcile_recovery_intent(service, intent, *, actor):
    """Observe the existing domain receipt and verify under current admission.

    A lost receipt or missing domain record never permits an execute retry.
    No Work Item success is supported by this custody-only action.
    """
    try:
        if service.identity is None or service.authority is None or intent.work_item_success is not None:
            raise ActionRequirementError("Recovery settlement requires canonical identity/authority and no Work Item advance")
        binding, provider, definition, request = service.execution.resolve_contract(
            intent.binding_id, intent.request, actor=actor)
        from codex_web.services.recovery_action_provider import RecoveryActionProvider
        if not isinstance(provider, RecoveryActionProvider) or definition != intent.action_definition:
            raise ActionRequirementError("Recovery provider/Definition changed")
        recovery = provider.recovery
        repositories = [service.execution.registry.store, service.execution.resources.store,
            service.identity.store, service.authority.registry.store, recovery.store,
            recovery.crypto.store, recovery.evidence.store, recovery.scheduler.store]
        if service.artifact_evidence is not None:
            repositories.append(service.artifact_evidence.store)
        if service.execution_profiles is not None:
            repositories.append(service.execution_profiles.registry.store)
            if service.execution_profiles.agent_profiles is not None:
                repositories.append(service.execution_profiles.agent_profiles.store)
        if service.procedure_discovery is not None:
            repositories.append(service.procedure_discovery.skills.definitions.store)
        if service.procedure_execution is not None:
            repositories.append(service.procedure_execution.approvals.store)
        if any(r.store is not service.store.store for r in repositories):
            raise ActionRequirementError("Recovery settlement requires the shared canonical transaction store")
        domains = {r.namespace: (None,) for r in repositories}
        if service.procedure_discovery is not None:
            security = service.procedure_discovery.skills.security
            if security is not None:
                store = security.store
                if store.state is not service.store.store:
                    raise ActionRequirementError("Procedure security requires canonical store")
                names = (store.SCANS, store.OVERRIDES, store.BASELINES)
                existing = service.store.store.record_collections_exist(names)
                domains.update({n: () if existing[n] else (None,) for n in names})
        revisions = lambda: {n: service.store.store.namespace_revision(n) for n in domains}
        pins = await asyncio.to_thread(revisions)
        requester = await asyncio.to_thread(service._requester_actor, intent)
        authority = await asyncio.to_thread(service._canonical_authority_snapshot,
            definition=definition, request=request, actor=requester, execution_id=intent.execution_id)
        if authority.outcome != ActionDecisionOutcome.ALLOW:
            raise ActionRequirementError("Current Recovery action authority denied")
        admitted, error = await asyncio.to_thread(service._refresh_security, intent,
            actor=requester, authority_decision=authority)
        if admitted is None:
            raise ActionRequirementError(error or "Current security admission denied")
        preparation = await service.execution.prepare(intent.binding_id, request, actor=actor)
        plan = preparation.provider_plan
        if plan.get("operation") != "recovery-scheduled-firing-reconciliation" or plan.get("observed_result") is None:
            raise ActionRequirementError("Original Recovery outcome is not observed; never replay")
        result = ActionResult.model_validate(plan["observed_result"])
        if (result.action_id != ACTION or result.provider_binding_id != intent.binding_id
                or result.idempotency_key != intent.idempotency_key
                or result.external_id != request.parameters.get("event_id")):
            raise ActionRequirementError("Observed Recovery receipt does not bind this intent")
        if service.execution.procedure_admission is not None:
            from codex_web.procedure_discovery import procedure_action_intent
            with procedure_action_intent(intent.id):
                await service.execution.procedure_admission(binding, provider, definition, result, actor, "verify")
        if await asyncio.to_thread(revisions) != pins:
            raise ActionRequirementError("Recovery admission changed during observation")

        def guard(documents):
            if revisions() != pins:
                raise ActionRequirementError("Recovery admission changed before settlement")
            current = service._intent(intent.id, actor)
            exclude = {"last_receipt_id", "last_verification_id", "updated_at", "procedure_discoveries"}
            if current.status not in PENDING or current.model_dump(exclude=exclude) != admitted.model_dump(exclude=exclude):
                raise ActionRequirementError("Recovery intent snapshot changed")
            current_requester = service._requester_actor(current)
            service.identity.actor_for_identity(actor.identity_id, scope=actor.tenant)
            current_binding, current_provider, current_definition, current_request = service.execution.resolve_contract(
                intent.binding_id, request, actor=actor)
            if current_binding != binding or current_provider is not provider or current_definition != definition:
                raise ActionRequirementError("Recovery provider admission changed")
            decision = service._canonical_authority_snapshot(definition=definition,
                request=current_request, actor=current_requester, execution_id=intent.execution_id)
            if decision.outcome != ActionDecisionOutcome.ALLOW or decision.definition_refs != authority.definition_refs:
                raise ActionRequirementError("Recovery authority expired or changed")
            if current.security_decision.outcome != SecurityDecisionOutcome.ALLOW or current.security_policy != binding.security_policy:
                raise ActionRequirementError("Recovery security observation changed")
            if service.security_boundary is not None:
                decision = service.security_boundary.evaluate_action(binding=binding,definition=definition,
                    request=current_request,authority_outcome=decision.outcome.value,
                    authority_source=decision.source,policy_outcome=current.policy_decision.outcome.value,
                    policy_source=current.policy_decision.source,actor=current_requester,
                    action_intent_id=intent.id,work_item_ref=intent.work_item_ref,execution_id=intent.execution_id)
                if decision.outcome != SecurityDecisionOutcome.ALLOW:
                    raise ActionRequirementError("Current Recovery security denied settlement")
            record = provider.reconcile.inspect(result.external_id, actor=provider.resolve_actor(request.requested_by))
            if record is None or record.model_dump(mode="json") != result.output.get("record"):
                raise ActionRequirementError("Recovery disposition changed")
            validate_proof(recovery.evidence.store.load(),record.request,record.original,
                current_requester,float(recovery.clock()))
        guarded = {"guard_rows": domains, "guard": guard}
        # Persist the actual observation first. If later admission is revoked,
        # it remains available; no successful status or effect is manufactured.
        await asyncio.to_thread(service._append_receipt,intent,result=result,outcome="completed",
            details={"operation":"recovery-state-reconciliation","provider_mutation_replayed":False})
        if not await service._verify_completion(admitted, result, actor=actor, settlement_guard=guarded):
            raise ActionRequirementError("Recovery result verification denied")
        return await asyncio.to_thread(service._set_status,intent.id,ActionIntentStatus.SUCCEEDED,guarded=guarded)
    except Exception as exc:
        # Retain unknown outcome and any already committed observation. Error
        # class only: no provider payload, identity detail or secret is logged.
        return await asyncio.to_thread(service._ready_reconciliation,intent.id,
            "Recovery read-only reconciliation blocked: " + type(exc).__name__)
