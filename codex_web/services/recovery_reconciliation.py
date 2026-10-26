"""Explicit Evidence-bound settlement; never an executor, retry or timeout takeover."""
from __future__ import annotations
import hashlib
import re
from codex_web.canonical_json import canonical_json_bytes
from codex_web.artifact_evidence import EvidenceLifecycle, EvidenceResult, EvidenceType, VerificationResult
from codex_web.identity import PrincipalKind, AuthenticationAssurance
from codex_web.recovery import RecoveryReconcileRequest, RecoveryFiringReconciliation
from codex_web.services.identity import AuthorizationError, IdentityService
from codex_web.services.recovery import RecoveryConflictError
from codex_web.services.recovery_policy import RecoveryPolicyControl

PROOF_CONTRACT = "recovery-owner-disposition.v2"


def fingerprint(value):
    return hashlib.sha256(canonical_json_bytes(value.model_dump(mode="json"))).hexdigest()


def require_operator(actor):
    if actor.principal_kind == PrincipalKind.SERVICE:
        if "recovery:admin" not in actor.service_scopes:
            raise AuthorizationError("recovery:admin service scope required")
    else:
        IdentityService.require_admin(actor)
        IdentityService.require_assurance(actor, AuthenticationAssurance.MFA)


def validate_proof(evidence_state, request, firing, actor, now):
    evidence = next((e for e in evidence_state.evidence if e.id == request.evidence_id), None)
    verification = next((v for v in evidence_state.verifications if v.id == request.verification_id), None)
    if (evidence is None or verification is None
            or (evidence.organization_id, evidence.workspace_id) != (actor.organization_id, actor.workspace_id)
            or (verification.organization_id, verification.workspace_id) != (actor.organization_id, actor.workspace_id)
            or evidence.lifecycle != EvidenceLifecycle.VALID or evidence.result != EvidenceResult.PASS
            or evidence.evidence_type != EvidenceType.RUNTIME_RESULT
            or evidence.observed_at < firing.admitted_at or evidence.observed_at > now
            or evidence.retention_expires_at is not None and evidence.retention_expires_at <= now
            or verification.result != VerificationResult.VERIFIED or not verification.independent
            or verification.verifier_identity_id == evidence.producer_identity_id
            or request.evidence_id not in verification.evidence_ids
            or verification.verified_at < max(evidence.observed_at, evidence.created_at)
            or verification.verified_at > now or verification.method != PROOF_CONTRACT):
        raise RecoveryConflictError("Current independent owner/effect proof is unavailable")
    metadata = evidence.metadata
    required = {"contract": PROOF_CONTRACT, "event_id": firing.event_id,
        "owner_id": firing.owner_id, "effect_id": firing.effect_id,
        "firing_sha256": request.expected_firing_sha256,
        "schedule_sha256": request.expected_schedule_sha256,
        "policy_fingerprint": request.expected_policy_fingerprint,
        "writers_quiescent": True, "effects_quiescent": True, "retention_settled": True,
        "all_writer_incarnations_accounted": True, "original_owner_fenced": True,
        "state_sha256": request.expected_state_sha256}
    # Booleans must be actual booleans: 1/"true" do not establish quiescence.
    if any(type(metadata.get(k)) is not type(v) or metadata.get(k) != v for k, v in required.items()):
        raise RecoveryConflictError("Owner/effect proof does not bind this exact snapshot")
    outcome = metadata.get("effect_outcome")
    if outcome not in ("succeeded", "failed", "unknown"):
        raise RecoveryConflictError("Invalid original effect outcome")
    for name in ("writer_inventory_sha256", "writer_fence_proof_sha256"):
        if not isinstance(metadata.get(name), str) or not re.fullmatch(r"[0-9a-f]{64}", metadata[name]):
            raise RecoveryConflictError("Complete writer inventory/fence proof unavailable")
    if request.activate_state_1_3 and (metadata.get("state_activation_qualified") is not True
            or metadata.get("migration_backup_restore_verified") is not True):
        raise RecoveryConflictError("State1.3 activation is not independently qualified")
    return outcome


class RecoveryReconciliationService:
    def __init__(self, recovery, resolve_actor):
        self.service = recovery
        self.resolve_actor = resolve_actor

    def _current(self, actor):
        current = self.resolve_actor(actor.identity_id)
        if (current.identity_id, current.organization_id, current.workspace_id, current.principal_kind) != (actor.identity_id, actor.organization_id, actor.workspace_id, actor.principal_kind):
            raise AuthorizationError("Current operator identity or scope changed")
        require_operator(current)
        return current

    def reconcile(self, request: RecoveryReconcileRequest, *, actor, mutation_admission=None):
        actor = self._current(actor)
        service = self.service
        if service.scheduler is None or not hasattr(service.evidence, "store"):
            raise RecoveryConflictError("Canonical Scheduler and Evidence stores are required")
        if service.evidence.store.store is not service.store.store:
            raise RecoveryConflictError("Reconciliation requires one canonical transactional store")
        request_sha = fingerprint(request)
        # Read-only same-key reconciliation after a lost response. Current
        # authorization/scope still apply; historical receipt is not eligibility.
        existing_state = service.store.load()
        RecoveryPolicyControl(service).require_scope(existing_state, actor)
        for prior in existing_state.reconciliations.values():
            if prior.idempotency_key == request.idempotency_key:
                if prior.request_sha256 != request_sha:
                    raise RecoveryConflictError("Reconciliation key has another request")
                return prior
        repeated = False
        result = None
        locked_evidence = None
        def evidence_update(state):
            nonlocal locked_evidence
            locked_evidence = state
            return state
        def apply(state, keys):
            nonlocal result, repeated
            self._current(actor)
            RecoveryPolicyControl(service).require_scope(state, actor, key_records=keys.keys)
            # Durable same-key receipt survives response loss. It never replays
            # backup, retention or Evidence production, even after revocation.
            previous = next((v for v in state.reconciliations.values()
                if v.idempotency_key == request.idempotency_key), None)
            if previous is not None:
                if previous.request_sha256 != request_sha or (
                        previous.original.organization_id, previous.original.workspace_id) != (actor.organization_id, actor.workspace_id):
                    raise RecoveryConflictError("Reconciliation key has another request/scope")
                repeated = True
                result = previous
                return state
            firing = state.scheduled_firings.get(request.event_id)
            if (firing is None or firing.status not in ("running", "unknown")
                    or (firing.organization_id, firing.workspace_id) != (actor.organization_id, actor.workspace_id)
                    or fingerprint(firing) != request.expected_firing_sha256
                    or state.policy is None or state.policy.fingerprint() != request.expected_policy_fingerprint
                    or request.event_id in state.reconciliations):
                raise RecoveryConflictError("Recovery firing/policy snapshot changed")
            if fingerprint(state) != request.expected_state_sha256:
                raise RecoveryConflictError("Recovery activation/state snapshot changed")
            if state.schema_version == "1.2" and not request.activate_state_1_3:
                raise RecoveryConflictError("Explicit qualified state1.3 activation required")
            local = service._scheduled_executor.status()
            if firing.owner_id == service._scheduled_admission.owner_id and (
                    local["accepting"] or local["waiting_or_running"] or local["inflight_work"]):
                raise RecoveryConflictError("Original local owner is not drained and closed")
            outcome = validate_proof(locked_evidence, request, firing, actor, float(service.clock()))
            if outcome == "succeeded":
                # A manifest alone was insufficient above. Proof must also bind
                # terminal effect/retention; require its matching canonical fact.
                fact = (state.backups if firing.trigger_type == service.BACKUP_TRIGGER else state.verifications).get(firing.effect_id)
                if fact is None or (fact.organization_id, fact.workspace_id) != (actor.organization_id, actor.workspace_id):
                    raise RecoveryConflictError("Successful effect has no scoped canonical result")
                if firing.trigger_type == service.BACKUP_TRIGGER:
                    matches = fact.policy_fingerprint == firing.policy_fingerprint and fact.created_at >= firing.admitted_at
                else:
                    matches = fact.backup_id == firing.backup_id and fact.verified_at >= firing.admitted_at
                if not matches:
                    raise RecoveryConflictError("Canonical result does not bind the reserved firing")
            result = RecoveryFiringReconciliation(original=firing, outcome=outcome,
                evidence_id=request.evidence_id, verification_id=request.verification_id,
                actor_id=actor.identity_id, reconciled_at=float(service.clock()),
                request=request, request_sha256=request_sha, idempotency_key=request.idempotency_key)
            # This is the only first-write activation. Proof, actor, complete
            # state/policy/schedule CAS and keys share the enclosing transaction.
            state.schema_version = "1.3"
            state.reconciliations[firing.event_id] = result
            state.scheduled_firings[firing.event_id] = firing.model_copy(update={
                "status": "succeeded" if outcome == "succeeded" else "unknown",
                "finished_at": result.reconciled_at,
                "error_code": None if outcome == "succeeded" else ("operator_verified_terminal_failure" if outcome == "failed" else "operator_released_custody_effect_unknown")})
            return state
        def schedule(state, schedules):
            # Called in the same transaction; rejection rolls back all updates.
            if repeated:
                return
            original = result.original
            record = schedules.schedules.get(original.schedule_id)
            if record is None or fingerprint(record) != request.expected_schedule_sha256:
                raise RecoveryConflictError("Recovery schedule snapshot changed")
        # Independent writer proof remains mandatory. This additional local
        # gate prevents manual/scheduled submission behind the shared CAS.
        with service._scheduled_executor.quiescence():
            service._write_key_bound(apply, actor=actor, requirements=(),
                evidence_update=evidence_update, scheduler_update=schedule, _activate_reconciliation=True,
                mutation_admission=mutation_admission)
        return result

    def preview(self, event_id, *, actor):
        actor = self._current(actor)
        state = self.service.store.load()
        RecoveryPolicyControl(self.service).require_scope(state, actor)
        firing = state.scheduled_firings.get(event_id)
        if firing is None or (firing.organization_id,firing.workspace_id) != (actor.organization_id,actor.workspace_id):
            raise RecoveryConflictError("Scoped firing unavailable")
        if self.service.scheduler is None or state.policy is None:
            raise RecoveryConflictError("Canonical policy/schedule unavailable")
        schedule = self.service.scheduler.store.load().schedules.get(firing.schedule_id)
        if schedule is None:
            raise RecoveryConflictError("Canonical schedule unavailable")
        return {"event_id":event_id,"owner_id":firing.owner_id,"effect_id":firing.effect_id,
            "expected_state_sha256":fingerprint(state),"activation_required":state.schema_version == "1.2",
            "expected_firing_sha256":fingerprint(firing),"expected_schedule_sha256":fingerprint(schedule),
            "expected_policy_fingerprint":state.policy.fingerprint(),"proof_contract":PROOF_CONTRACT,
            "action":"recovery.scheduled-firing.reconcile", "provenance":"canonical Recovery/Scheduler read; rechecked transactionally at execution",
            "next_action":"Qualified independent owner/effect Evidence and canonical ActionIntent; no effect replay"}

    def inspect(self, event_id, *, actor):
        actor = self._current(actor)
        state = self.service.store.load()
        RecoveryPolicyControl(self.service).require_scope(state, actor)
        return state.reconciliations.get(event_id)


def _validated_custody_records(service, state, actor):
    """Return scoped proof-validated settlements, or None for unresolved custody."""
    scoped = [f for f in state.scheduled_firings.values()
        if (f.organization_id, f.workspace_id) == (actor.organization_id, actor.workspace_id)]
    pending = [f for f in scoped if f.status == "unknown"]
    reconciled = [r for r in state.reconciliations.values()
        if (r.original.organization_id, r.original.workspace_id) == (actor.organization_id, actor.workspace_id)]
    if not pending and not reconciled:
        return []
    if not hasattr(service.evidence, "store"):
        return None
    evidence = service.evidence.store.load()
    for firing in pending:
        record = state.reconciliations.get(firing.event_id)
        if record is None or not record.custody_released or record.outcome not in ("failed", "unknown"):
            return None
        try:
            validate_proof(evidence, record.request, record.original, actor, float(service.clock()))
        except RecoveryConflictError:
            return None
    for record in reconciled:
        # Even a proven successful historical effect is not fresh qualification.
        # Evidence revocation or expiration is not ignored by the consumer.
        try:
            validate_proof(evidence, record.request, record.original, actor, float(service.clock()))
        except RecoveryConflictError:
            return None
    return reconciled


def capture_custody_released(service, *, actor):
    """Capture-only: current settled custody may precede fresh backup verification.

    Never infers a remote owner is idle from an empty local executor. Every
    historical UNKNOWN must retain its independently verified fencing proof.
    Ordinary Recovery health still requires a newer matching backup/PASS.
    """
    if not hasattr(service, "store"):
        return False
    state = service.store.load()
    RecoveryPolicyControl(service).require_scope(state, actor)
    scope = (actor.organization_id, actor.workspace_id)
    scoped = [f for f in state.scheduled_firings.values()
              if (f.organization_id, f.workspace_id) == scope]
    if any(f.status == "running" for f in scoped):
        return False
    for firing in scoped:
        record = state.reconciliations.get(firing.event_id)
        if firing.status == "unknown" and record is not None and (
                record.original.organization_id, record.original.workspace_id) != scope:
            return False
    records = _validated_custody_records(service, state, actor)
    # No records cannot explain this blocker. Require explicit release even for
    # settlements no longer referenced by a pending UNKNOWN firing.
    return bool(records) and all(record.custody_released is True for record in records)


def unresolved_unknown(service, state, actor, latest_backup, latest_verify):
    """History remains unknown; explicit proof never replaces fresh recovery."""
    from codex_web.recovery import RestoreVerificationStatus
    reconciled = _validated_custody_records(service, state, actor)
    if reconciled is None:
        return True
    for record in reconciled:
        if (latest_backup is None or latest_verify is None
                or latest_backup.created_at <= record.reconciled_at
                or latest_verify.verified_at <= record.reconciled_at
                or latest_verify.backup_id != latest_backup.id
                or latest_verify.status != RestoreVerificationStatus.PASS
                or state.policy is None or latest_backup.policy_fingerprint != state.policy.fingerprint()):
            return True
    return False
