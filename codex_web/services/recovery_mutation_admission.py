"""Internal ActionIntent context and final transactional Recovery admission.

No serialized actor, callback, admission flag or request parameter can install
this context. Domains use the SAME sorted canonical locks as all normal writers.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import time

from codex_web.action_intents import ActionDecisionOutcome, ActionIntentStatus
from codex_web.security import SecurityDecisionOutcome
from codex_web.services.action_providers import ActionRequirementError
from codex_web.services.recovery_reconciliation import require_operator

_current = ContextVar('canonical_recovery_mutation_executor', default=None)


@contextmanager
def recovery_mutation_executor(service, intent_id, worker):
    token = _current.set((service, intent_id, worker))
    try:
        yield
    finally:
        _current.reset(token)


def mutation_intent_id():
    origin = _current.get()
    if origin is None:
        raise ActionRequirementError('Canonical Recovery mutation executor unavailable')
    return origin[1]


def mutation_guard(provider, binding, request):
    origin = _current.get()
    if origin is None:
        raise ActionRequirementError('Canonical Recovery mutation executor unavailable')
    service, intent_id, worker = origin
    if service.identity is None or service.authority is None:
        raise ActionRequirementError('Canonical Recovery identity/authority unavailable')
    recovery = provider.recovery
    repositories = [service.identity.store, service.execution.registry.store,
        service.execution.resources.store, service.authority.registry.store]
    secret_store = (service.execution.secret_broker.store
                    if service.execution.secret_broker is not None else None)
    if secret_store is not None:
        repositories.append(secret_store)
    if service.execution_profiles is not None:
        repositories.append(service.execution_profiles.registry.store)
        if service.execution_profiles.agent_profiles is not None:
            repositories.append(service.execution_profiles.agent_profiles.store)
    if service.procedure_discovery is not None:
        repositories.append(service.procedure_discovery.skills.definitions.store)
    if service.procedure_execution is not None:
        repositories.append(service.procedure_execution.approvals.store)
    if any(r.store is not recovery.store.store for r in repositories) or service.store.store is not recovery.store.store:
        raise ActionRequirementError('Recovery admission requires one canonical transaction store')
    domains = {r.namespace: (None,) for r in repositories}
    if service.procedure_discovery is not None:
        security = service.procedure_discovery.skills.security
        if security is not None:
            if security.store.state is not recovery.store.store:
                raise ActionRequirementError('Procedure security requires the canonical store')
            names = (security.store.SCANS, security.store.OVERRIDES, security.store.BASELINES)
            exists = recovery.store.store.record_collections_exist(names)
            domains.update({n: () if exists[n] else (None,) for n in names})
    initial = service._intent(intent_id, worker)
    from codex_web.recovery_preservation import ACTIONS as PRESERVATION_ACTIONS
    allowed = PRESERVATION_ACTIONS | {'recovery.scheduled-firing.reconcile'}
    if initial.request != request or initial.binding_id != binding.id or initial.action_id not in allowed or initial.action_id != request.action_id:
        raise ActionRequirementError('Recovery originating intent/request mismatch')
    if initial.execution_id is not None:
        from codex_web.storage.execution_workers import ASSIGNMENTS, WORKERS
        from codex_web.services.publication_recovery import execution_assignment
        if service.execution_workers is None or service.execution_workers.store.store is not recovery.store.store:
            raise ActionRequirementError('Recovery execution assignment unavailable')
        requester = provider.resolve_actor(request.requested_by)
        assignment = execution_assignment(service.execution_workers, initial.execution_id, requester,
            initial.publication_recovery_scope.recovery.assignment_id if initial.publication_recovery_scope else None)
        if not assignment.assigned_worker_id:
            raise ActionRequirementError('Recovery assignment has no current worker')
        domains.update({ASSIGNMENTS: (assignment.id,), WORKERS: (assignment.assigned_worker_id,)})
    domains[service.store.records_namespace] = ('intents:' + intent_id,)
    # Secret use appends audit observations, not credential authority. Keep the
    # same secret namespace in the transaction's locks, but pin every metadata
    # field outside its audit list (including rotation/revocation/ACL/expiry).
    secret_namespace = secret_store.namespace if secret_store is not None else None
    secret_authority = (secret_store.load().model_dump(mode='json', exclude={'audit'})
                        if secret_store is not None else None)
    pins = {n: recovery.store.store.namespace_revision(n) for n in domains
            if n != service.store.records_namespace and n != secret_namespace}
    configurations = (service.security_boundary, service.procedure_discovery, service.procedure_execution,
                      service.execution.procedure_admission, service.execution.secret_broker)

    def guard(_documents):
        if configurations != (service.security_boundary, service.procedure_discovery,
                service.procedure_execution, service.execution.procedure_admission, service.execution.secret_broker):
            raise ActionRequirementError('Recovery runtime admission configuration changed')
        if any(recovery.store.store.namespace_revision(n) != revision for n, revision in pins.items()):
            raise ActionRequirementError('Recovery admission changed before irreversible write')
        if (secret_store is not None
                and secret_store.load().model_dump(mode='json', exclude={'audit'}) != secret_authority):
            raise ActionRequirementError('Recovery secret authority changed before irreversible write')
        current = service._intent(intent_id, worker)
        if (current.status != ActionIntentStatus.EXECUTING or current.request != request
                or current.binding_id != binding.id or current.action_definition != initial.action_definition
                or current.attempt != initial.attempt or current.lease != initial.lease
                or current.lease is None or current.lease.expires_at <= time.time()):
            raise ActionRequirementError('Recovery originating execution is no longer current')
        service._require_worker(worker)
        service.identity.actor_for_identity(worker.identity_id, scope=worker.tenant)
        operator = provider.resolve_actor(request.requested_by)
        require_operator(operator)
        current_binding, current_provider, definition, resolved = service.execution.resolve_contract(
            binding.id, request, actor=worker)
        if current_binding != binding or current_provider is not provider or definition != initial.action_definition or resolved != request:
            raise ActionRequirementError('Recovery binding/Definition changed')
        decision = service._canonical_authority_snapshot(definition=definition,
            request=request, actor=operator, execution_id=initial.execution_id)
        if (decision.outcome != ActionDecisionOutcome.ALLOW
                or decision.definition_refs != (initial.authority_recheck or initial.authority_decision).definition_refs
                or current.policy_decision.outcome == ActionDecisionOutcome.DENY
                or current.security_decision.outcome != SecurityDecisionOutcome.ALLOW
                or current.security_policy != binding.security_policy):
            raise ActionRequirementError('Recovery current authority/security denied')
        if service.security_boundary is not None:
            security = service.security_boundary.assess_action(binding=binding, definition=definition,
                request=request, authority_outcome=decision.outcome.value, authority_source=decision.source,
                policy_outcome=current.policy_decision.outcome.value, policy_source=current.policy_decision.source)
            if security.outcome != SecurityDecisionOutcome.ALLOW:
                raise ActionRequirementError('Current Recovery security denied')
        required_procedures = ()
        if service.procedure_discovery is not None:
            context = service._procedure_context(actor=operator, request=request, definition=definition,
                provider=provider, policy_source=current.policy_decision.source,
                policy_allows=True, execution_id=current.execution_id,
                assignment_id=(current.publication_recovery_scope.recovery.assignment_id if current.publication_recovery_scope else None))
            previous = current.procedure_discoveries[-1] if current.procedure_discoveries else None
            discovered, _ = service.procedure_discovery.discover(context, previous=previous)
            if not discovered.admitted or previous is None or discovered.selected != previous.selected:
                raise ActionRequirementError('Current Recovery procedure admission changed')
            required_procedures = discovered.selected
            for reference in discovered.selected:
                service.procedure_discovery.skills.validate_reference(reference, actor=operator)
        mediated = False
        if service.procedure_execution is not None:
            mediated = service.procedure_execution.provider_admission_current(current, operator, 'execute')
        if required_procedures and not mediated:
            raise ActionRequirementError('Required Recovery procedure has no current canonical step')
    # Read now; repeat under all shared locks immediately before AND after the
    # domain updater. A prior committed denial is never a post-migration error.
    guard(None)
    return {'guard_rows': domains, 'guard': guard, 'intent_id': intent_id}
