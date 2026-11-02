"""Bounded canonical observations of an explicitly authorized Home delegation.

No provider reads, mutations, history scans, or historical lease resurrection.
Event data locates evidence; current Home authority and exact original lineage
are checked independently before any selected metadata is returned.
"""
from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from codex_web.authority import AuthorityEvaluationRequest, AuthorityLevel
from codex_web.models import HomeOperatorCoordinationDispatch, QueuedNativeStartReceipt, QueuedTurn
from codex_web.services.canonical_events import CanonicalEventIngestionService
from codex_web.services.home_child_terminal import SOURCE as TERMINAL_SOURCE
from codex_web.services.queued_native_start import validate_attribution
from codex_web.storage.agent_sessions import AgentSessionConflictError

READ = 'operator.project.delegation.read'


class HomeDelegationRead(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    intent_id: str = Field(pattern=r'^action-intent-[A-Za-z0-9]+$', max_length=200)
    event_id: str | None = Field(default=None, pattern=r'^evt-[A-Za-z0-9]+$', max_length=200)


def selected(value, names):
    return {name: getattr(value, name) for name in names}


def read_delegation(service, *, assignment_id, fence, query):
    # Local import avoids making coordination's declaration depend on its adapter.
    from codex_web.services.home_operator_coordination import HomeProjectEffect, MESSAGE_ACTION, PROVIDER, denied
    data = HomeDelegationRead.model_validate(query)
    current, actor, home, envelope = service.origin(assignment_id, fence, READ)
    if service.preflight is None or service.events is None or service.broker.action_intents is None:
        denied('Canonical Home delegation readers are unavailable')
    intents = service.broker.action_intents
    intent = intents.get(data.intent_id, actor)
    effect = HomeProjectEffect.model_validate(intent.request.parameters)
    if (intent.action_id != MESSAGE_ACTION
            or (intent.provider_type, intent.provider_instance) != (PROVIDER, 'canonical')
            or (intent.organization_id, intent.workspace_id, intent.project_id, intent.requested_by,
                intent.resource_ids) != (actor.organization_id, actor.workspace_id, 'home',
                    actor.identity_id, (envelope.host_resource_id,))
            or intent.request.action_id != MESSAGE_ACTION
            or (intent.request.organization_id, intent.request.workspace_id, intent.request.project_id,
                intent.request.requested_by, intent.request.resource_ids) !=
                (intent.organization_id, intent.workspace_id, intent.project_id, intent.requested_by, intent.resource_ids)
            or intent.idempotency_key != effect.admission_key
            or not effect.admission_key.startswith(f'home-coordinate:{effect.assignment_id}:{effect.fence}:')):
        denied('Original Home delegation identity differs')
    # Historical target proof is not the current target bootstrap. A legitimate
    # successor binding/fence does not erase the original child's observation.
    target = service.broker.execution_workers.store.assignment(effect.target_assignment_id)
    declaration = next((p for p in envelope.project_targets if p.project_id == effect.project_id), None)
    if (declaration is None or effect.project_id == 'home' or target is None
            or (target.organization_id, target.workspace_id, target.project_id, target.created_by) !=
               (actor.organization_id, actor.workspace_id, effect.project_id, actor.identity_id)
            or target.agent_profile is None or target.fence < effect.target_fence
            or (target.agent_profile.profile_id, target.agent_profile.profile_revision) not in
               {(p.profile_id, p.revision) for p in declaration.child_profiles}):
        denied('Historical child is outside the current Home declaration')
    service.scope.project(effect.project_id, actor)
    decision = service.broker.authority.evaluate(AuthorityEvaluationRequest(
        capability='operator.project.coordinate', level=AuthorityLevel.READ,
        project_id=effect.project_id, resource_ids=target.resource_ids), actor=actor)
    if decision.outcome != 'allow':
        denied('Current child Project read authority denied')
    try:
        receipt = intents.store.recovery_last_receipt(intent)
    except ValueError:
        denied('Canonical parent receipt inventory is incomplete')
    event = service.events.event(data.event_id) if data.event_id else None
    if data.event_id and event is None:
        denied('The exact delegated terminal event is unavailable')
    queued_id = None
    if receipt is not None and receipt.result is not None:
        output = receipt.result.output
        if receipt.outcome == 'completed' and receipt.result.status == 'succeeded':
            if (output.get('intent_id'), output.get('thread_id'), output.get('project_id')) != (
                    intent.id, effect.thread_id, effect.project_id):
                denied('Canonical admission receipt belongs to another delegation')
            queued_id = output.get('queued_id')
    admission_accepted = queued_id is not None
    if event is not None:
        payload = event.payload
        if (event.tenant_id, event.workspace_id, event.event_type, event.source, event.causation_id) != (
                actor.organization_id, actor.workspace_id, 'work.transition', TERMINAL_SOURCE, intent.id):
            denied('Terminal event is outside the original Home delegation')
        if queued_id is not None and queued_id != payload.get('queued_turn_id'):
            denied('Terminal event and admission receipt differ')
        queued_id = payload.get('queued_turn_id')
        if not isinstance(queued_id, str) or not queued_id or payload.get('parent_intent_id') != intent.id:
            denied('Terminal event original input metadata is invalid')
    result = {'schema_version': '1.0', 'observed_at': service.broker._clock(),
        'home_revision': home.revision, 'assignment_id': current.id,
        'intent': selected(intent, ('id', 'status', 'attempt', 'execution_id', 'completed_at',
            'last_receipt_id', 'last_verification_id')),
        'receipt': None if receipt is None else {**selected(receipt, ('id', 'intent_id', 'attempt', 'outcome', 'received_at')),
            'result_status': None if receipt.result is None else receipt.result.status},
        'admission': {'queued_id': queued_id, 'accepted': True if admission_accepted else None},
        'child': {'project_id': effect.project_id, 'thread_id': effect.thread_id,
            'assignment_id': target.id, 'original_fence': effect.target_fence,
            'start': None, 'terminal': None, 'outcome': 'unknown'},
        'verification': {'id': intent.last_verification_id, 'verified': None},
        'read_only': True, 'execution_authority': False}
    if queued_id is not None:
        if not isinstance(queued_id, str) or not queued_id or len(queued_id) > 200:
            denied('Canonical retained input identifier is invalid')
        attempt = service.preflight.store.get('preflight-queued-' + queued_id)
        if attempt is not None:
            queued = QueuedTurn.model_validate(attempt.queued_turn_snapshot)
            proof = queued.project_coordination_dispatch
            if (not isinstance(proof, HomeOperatorCoordinationDispatch)
                    or (attempt.organization_id, attempt.workspace_id, attempt.project_id, attempt.thread_id,
                        attempt.execution_id, attempt.source) != (actor.organization_id, actor.workspace_id,
                        effect.project_id, effect.thread_id, 'home-coordinate-input:' + intent.id, 'home-operator-coordination')
                    or (queued.id, queued.thread_id, queued.project_id, queued.execution_id, queued.source,
                        queued.agent_profile_actor_id, queued.agent_profile_id, queued.agent_profile_revision) !=
                        (queued_id, effect.thread_id, effect.project_id, attempt.execution_id, attempt.source,
                        actor.identity_id, target.agent_profile.profile_id, target.agent_profile.profile_revision)
                    or (proof.intent_id, proof.assignment_id, proof.fence, proof.home_revision,
                        proof.target_assignment_id, proof.target_fence, proof.message_sha256) !=
                        (intent.id, effect.assignment_id, effect.fence, effect.home_revision,
                        target.id, effect.target_fence, hashlib.sha256(effect.message.encode()).hexdigest())
                    or queued.message != effect.message):
                denied('Retained child input differs from the original delegation')
            scope = target.repository_scope
            if (queued.execution_profile_id != target.execution_profile_id
                    or queued.writable_repository_resource_ids != (scope.writable_repository_ids if scope else ())
                    or queued.read_only_repository_resource_ids != (scope.read_only_repository_ids if scope else ())):
                denied('Retained child repository scope differs from historical custody')
            result['admission']['preflight_id'] = attempt.id
            result['admission']['preflight_status'] = attempt.status
            correlation, evidence, start = attempt.queued_start_correlation, attempt.queued_start_evidence, attempt.queued_start_receipt
            if correlation is not None and evidence is not None and start is not None:
                validate_attribution(service.preflight, attempt, correlation)
                expected = QueuedNativeStartReceipt(admission_claim_id=correlation.admission_claim_id,
                    requested_execution_id=correlation.requested_execution_id, execution_id=correlation.execution_id,
                    thread_id=correlation.thread_id, provider_native_session_id=evidence.provider_native_session_id,
                    provider_native_turn_id=evidence.provider_native_turn_id, provider_id=correlation.provider_id,
                    runtime_id=correlation.runtime_id, observed_at=evidence.observed_at)
                if (attempt.status != 'started' or start != expected
                        or (correlation.assignment_id, correlation.execution_id, correlation.fence,
                            correlation.provider_native_session_id, evidence.runtime_generation, evidence.rpc_request_id) !=
                            (target.id, target.execution_id, effect.target_fence, effect.thread_id,
                            correlation.runtime_generation, correlation.rpc_request_id)):
                    denied('Child native START attribution differs')
                result['child']['start'] = selected(start, ('execution_id', 'provider_native_session_id', 'provider_native_turn_id', 'observed_at'))
                key = f'{attempt.id}:{start.provider_native_session_id}:{start.provider_native_turn_id}'
                expected_id = CanonicalEventIngestionService._event_id(TERMINAL_SOURCE, key)
                if event is None:
                    event = service.events.event(expected_id)
                if event is not None:
                    payload = event.payload
                    if (event.event_id != expected_id or (event.tenant_id, event.workspace_id, event.event_type,
                            event.source, event.causation_id, event.correlation_id) !=
                            (actor.organization_id, actor.workspace_id, 'work.transition', TERMINAL_SOURCE, intent.id, target.execution_id)
                            or any(payload.get(k) != v for k, v in {
                                'transition':'home_child_native_terminal', 'parent_intent_id':intent.id,
                                'home_assignment_id':effect.assignment_id, 'home_revision':effect.home_revision,
                                'project_id':effect.project_id, 'thread_id':effect.thread_id,
                                'assignment_id':target.id, 'fence':effect.target_fence,
                                'execution_id':target.execution_id, 'execution_workspace_id':target.execution_workspace_id,
                                'admission_attempt_id':attempt.id, 'queued_turn_id':queued_id,
                                'native_turn_id':start.provider_native_turn_id, 'worker_id':correlation.worker_id,
                                'provider_id':start.provider_id, 'runtime_id':start.runtime_id}.items())):
                        denied('Child terminal observation differs from its canonical START')
                    if type(payload.get('completion_succeeded')) is not bool or type(payload.get('usage_available')) is not bool:
                        denied('Child terminal metadata is invalid')
                    terminal = {'event_id':event.event_id, 'observed_at':event.occurred_at,
                        'completion_succeeded':payload['completion_succeeded'], 'outcome':'unknown',
                        'usage_available':False, 'usage':None}
                    # The event locates usage; it does not substitute for the
                    # canonical usage record or manufacture known success.
                    usage_id = payload.get('usage_id')
                    if usage_id is None and service.sessions is not None:
                        try:
                            session = service.sessions.find_unique_by_native_id(start.provider_native_session_id,
                                provider_id=start.provider_id, runtime_id=start.runtime_id)
                        except AgentSessionConflictError:
                            denied('Canonical child session identity is ambiguous')
                        if session is not None:
                            if (session.organization_id, session.workspace_id, session.project_id) != (
                                    actor.organization_id, actor.workspace_id, effect.project_id):
                                denied('Child native session belongs to another tenant or Project')
                            # A canonical conversation identity may now have
                            # successor custody. Usage itself must still match
                            # every historical assignment/turn field below.
                            usage_id = 'runtime-usage-' + hashlib.sha256(
                                f'{session.id}\n{start.provider_native_turn_id}'.encode()).hexdigest()[:32]
                    if isinstance(usage_id, str) and service.usage is not None:
                        usage = service.usage.get(usage_id)
                        if usage is not None:
                            if (usage.organization_id, usage.workspace_id, usage.project_id,
                                    usage.assignment_id, usage.execution_id, usage.execution_workspace_id,
                                    usage.provider_id, usage.runtime_id, usage.provider_native_session_id,
                                    usage.provider_native_turn_id) != (actor.organization_id, actor.workspace_id,
                                    effect.project_id, target.id, target.execution_id, target.execution_workspace_id,
                                    start.provider_id, start.runtime_id, start.provider_native_session_id,
                                    start.provider_native_turn_id):
                                denied('Child usage belongs to a different native turn')
                            if usage.completed_at is not None and usage.terminal_outcome != 'unknown':
                                terminal.update(outcome=usage.terminal_outcome, usage_available=True,
                                    usage=selected(usage, ('id', 'terminal_outcome', 'completed_at',
                                        'input_tokens', 'output_tokens', 'model_call_count', 'runtime_duration_seconds')))
                    result['child']['terminal'] = terminal
                    result['child']['outcome'] = terminal['outcome']
            if event is not None and result['child']['start'] is None:
                denied('Terminal observation has no positive canonical START')
        elif event is not None:
            denied('Terminal observation has no retained original child input')
    service.scope.project(effect.project_id, actor)
    if service.broker.authority.evaluate(AuthorityEvaluationRequest(
            capability='operator.project.coordinate', level=AuthorityLevel.READ,
            project_id=effect.project_id, resource_ids=target.resource_ids), actor=actor).outcome != 'allow':
        denied('Child Project authority changed during inspection')
    _, _, after_home, after_envelope = service.origin(assignment_id, fence, READ)
    if home != after_home or envelope != after_envelope or intents.get(intent.id, actor) != intent:
        denied('Home declaration or original delegation changed during inspection')
    return result
