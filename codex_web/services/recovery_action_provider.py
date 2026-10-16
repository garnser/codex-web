"""Opt-in canonical provider. Registration/activation is not an authority grant."""
import asyncio
from codex_web.action_providers import (ACTION_PROVIDER_CONTRACT, ActionCapability,
    ActionDefinition, ActionEvidence, ActionResult, ActionVerification, ActionRiskClass)
from codex_web.recovery import RecoveryReconcileRequest
from codex_web.services.recovery_reconciliation import RecoveryReconciliationService, validate_proof, fingerprint
from codex_web.services.recovery import RecoveryConflictError


class RecoveryActionProvider:
    contract_version = ACTION_PROVIDER_CONTRACT.current
    provider_type = "recovery"
    provider_instance = "canonical-recovery"

    def __init__(self, service, resolve_actor):
        # resolve_actor is existing canonical identity/session admission, not a
        # caller-supplied actor/role from parameters or an executable artifact.
        self.recovery = service
        self.reconcile = RecoveryReconciliationService(service, resolve_actor)
        self.resolve_actor = resolve_actor

    def actions(self):
        return (ActionDefinition(action_id="recovery.scheduled-firing.reconcile",
            title="Reconcile terminal scheduled Recovery owner",
            description="Evidence-bound original firing settlement without effect replay",
            capabilities=ActionCapability(prepare=True,execute=True,idempotency=True,verification=True,evidence=True),
            retry_max_attempts=1,risk_class=ActionRiskClass.HIGH,required_authority=("recovery.scheduled-firing.reconcile",),
            credential_required=False,network_access=False,expected_evidence=("recovery-reconciliation",)),)

    def _input(self, request, binding):
        if request.action_id != self.actions()[0].action_id or request.dry_run:
            raise RecoveryConflictError("Unsupported Recovery operation")
        actor = self.resolve_actor(request.requested_by)
        if (actor.identity_id != request.requested_by
                or (actor.organization_id,actor.workspace_id) != (request.organization_id,request.workspace_id)
                or (binding.organization_id,binding.workspace_id) != (request.organization_id,request.workspace_id)
                or binding.provider_type != self.provider_type or binding.provider_instance != self.provider_instance):
            raise RecoveryConflictError("Recovery action identity/scope changed")
        if request.resource_ids or request.credential_ref is not None:
            raise RecoveryConflictError("Recovery workspace action does not accept foreign resources/credentials")
        payload = RecoveryReconcileRequest.model_validate(request.parameters)
        if payload.idempotency_key != request.idempotency_key:
            raise RecoveryConflictError("Recovery request idempotency does not match intent")
        return actor, payload

    async def prepare(self, request, *, binding):
        actor, payload = self._input(request,binding)
        from codex_web.services.recovery_reconciliation import require_operator
        require_operator(actor)
        observed = await self.readback(request, binding=binding)
        return {"operation":"recovery-scheduled-firing-reconciliation",
            "event_id":payload.event_id,
            "observed_result": observed.model_dump(mode="json") if observed is not None else None,
            "next_action":"Read-only existing receipt reconciliation; otherwise execute only through current canonical intent gates"}

    async def execute(self, request, *, binding, credential=None):
        actor, payload = self._input(request,binding)
        from codex_web.services.recovery_mutation_admission import mutation_guard, mutation_intent_id
        def mutate():
            admitted = mutation_guard(self, binding, request)
            return self.reconcile.reconcile(payload, actor=actor, mutation_admission=admitted)
        record = await self.recovery._scheduled_executor.run('reconcile:' + mutation_intent_id(), mutate)
        return self._result(record, binding=binding)

    @staticmethod
    def _result(record, *, binding):
        # Stable identity/timestamps bind every reconstruction to the ONE durable
        # operation, including after receipt serialization/response loss.
        return ActionResult(execution_id="recovery-action-" + record.request_sha256,
            provider_binding_id=binding.id, action_id="recovery.scheduled-firing.reconcile",
            status="succeeded",started_at=record.reconciled_at,completed_at=record.reconciled_at,
            idempotency_key=record.idempotency_key,external_id=record.original.event_id,
            output={"record":record.model_dump(mode="json")},
            evidence=(ActionEvidence(evidence_type="recovery-reconciliation",
                reference=record.evidence_id, observed_at=record.reconciled_at,
                summary="Canonical Evidence-bound custody receipt; not fresh Recovery qualification",
                metadata={"event_id":record.original.event_id,"owner_id":record.original.owner_id,
                    "effect_id":record.original.effect_id,"verification_id":record.verification_id,
                    "request_sha256":record.request_sha256,"effect_outcome":record.outcome}),))

    async def readback(self, request, *, binding):
        """Observe only; no engine call, Evidence creation or original effect.

        None means no durable domain receipt, NEVER proof of no execution. This
        is not itself an ActionIntent settlement route or replay authorization.
        """
        actor, payload = self._input(request, binding)
        record = await asyncio.to_thread(self.reconcile.inspect,payload.event_id,actor=actor)
        if record is None:
            return None
        if record.request_sha256 != fingerprint(payload) or record.idempotency_key != request.idempotency_key:
            raise RecoveryConflictError("Observed receipt belongs to another immutable request")
        validate_proof(self.recovery.evidence.store.load(),record.request,record.original,
            actor,float(self.recovery.clock()))
        return self._result(record,binding=binding)

    async def verify(self, result, *, binding, credential=None):
        # Read-only settlement reconciliation: never call execute again.
        record = self.recovery.store.load().reconciliations.get(result.external_id)
        if (record is None or result.provider_binding_id != binding.id
                or (binding.organization_id,binding.workspace_id) != (record.original.organization_id,record.original.workspace_id)
                or binding.provider_type != self.provider_type or binding.provider_instance != self.provider_instance):
            return ActionVerification(verified=False,findings=("Canonical receipt unavailable",))
        actor = self.resolve_actor(record.actor_id)
        inspected = self.reconcile.inspect(record.original.event_id,actor=actor)
        try:
            validate_proof(self.recovery.evidence.store.load(),record.request,record.original,
                actor,float(self.recovery.clock()))
        except RecoveryConflictError:
            return ActionVerification(verified=False,findings=("Current disposition proof unavailable",))
        valid = (inspected == record and result == self._result(record,binding=binding))
        return ActionVerification(verified=valid,evidence=result.evidence if valid else ())

    async def rollback(self, result, *, binding, credential=None):
        from codex_web.action_providers import UnsupportedActionCapabilityError
        raise UnsupportedActionCapabilityError("rollback")
