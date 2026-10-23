"""Canonical terminal facts for positively started Home-delegated child inputs.

This observer grants no execution authority. Admission/intent/usage reads are exact canonical points; native session
resolution uses the existing two-candidate canonical lookup; older active markers without the optional reference are left untouched.
"""
from __future__ import annotations

import asyncio
import hashlib

from codex_web.models import HomeOperatorCoordinationDispatch, QueuedTurn
from codex_web.services.queued_native_start import validate_attribution

SOURCE = 'home-delegated-native-terminal'


class HomeChildTerminalService:
    def __init__(self, *, preflight, events, intents=None, sessions=None, usage=None):
        self.preflight, self.events = preflight, events
        self.intents, self.sessions, self.usage = intents, sessions, usage

    def _fact(self, active, succeeded):
        if (active.source or '').removeprefix('queued:') != 'home-operator-coordination':
            return None
        attempt_id = active.queued_admission_attempt_id
        if not attempt_id:
            return None  # Legacy observation metadata is not a historical-start grant.
        attempt = self.preflight.store.get(attempt_id)
        if attempt is None or attempt.status != 'started':
            raise ValueError('Home child positive canonical start required')
        correlation, receipt, evidence = attempt.queued_start_correlation, attempt.queued_start_receipt, attempt.queued_start_evidence
        if correlation is None or receipt is None or evidence is None:
            raise ValueError('Home child start evidence unavailable')
        validate_attribution(self.preflight, attempt, correlation)
        queued = QueuedTurn.model_validate(attempt.queued_turn_snapshot)
        proof = queued.project_coordination_dispatch
        if not isinstance(proof, HomeOperatorCoordinationDispatch):
            raise ValueError('Original typed Home delegation required')
        if (queued.source != 'home-operator-coordination'
                or queued.execution_id != 'home-coordinate-input:' + proof.intent_id
                or proof.target_assignment_id != active.assignment_id or proof.target_fence != active.fence
                or proof.message_sha256 != hashlib.sha256(queued.message.encode()).hexdigest()
                or queued.agent_profile_actor_id != active.agent_profile_actor_id
                or (active.project_id, active.thread_id, active.execution_id, active.assignment_id, active.worker_id, active.fence) !=
                   (attempt.project_id, receipt.provider_native_session_id, receipt.execution_id, correlation.assignment_id,
                    correlation.worker_id, correlation.fence)
                or correlation.provider_native_session_id != active.thread_id
                or correlation.thread_id != active.thread_id
                or receipt.thread_id != active.thread_id
                or receipt.execution_id != correlation.execution_id
                or active.turn_id != receipt.provider_native_turn_id
                or evidence.provider_native_turn_id != active.turn_id
                or evidence.provider_native_session_id != active.thread_id
                or evidence.runtime_generation != correlation.runtime_generation
                or evidence.rpc_request_id != correlation.rpc_request_id
                or receipt.admission_claim_id != correlation.admission_claim_id
                or receipt.requested_execution_id != correlation.requested_execution_id
                or (receipt.provider_id, receipt.runtime_id) != (correlation.provider_id, correlation.runtime_id)):
            raise ValueError('Home child terminal attribution changed')
        if self.intents is None:
            raise ValueError('Original Home intent reader unavailable')
        intent = self.intents.get(proof.intent_id)
        params = intent.request.parameters if intent is not None else {}
        # The queue can start before its provider returns and the engine commits
        # its parent receipt. Positive START remains mandatory; recording this
        # child fact neither certifies the parent action nor authorizes input.
        if (intent is None or intent.status not in {'executing', 'succeeded'}
                or intent.action_id != 'operator.project.thread.message'
                or (intent.provider_type, intent.provider_instance, intent.project_id) != ('home-operator-coordination', 'canonical', 'home')
                or (intent.organization_id, intent.workspace_id) != (attempt.organization_id, attempt.workspace_id)
                or intent.requested_by != queued.agent_profile_actor_id
                or params.get('assignment_id') != proof.assignment_id or params.get('fence') != proof.fence
                or params.get('home_revision') != proof.home_revision
                or params.get('target_assignment_id') != active.assignment_id or params.get('target_fence') != active.fence
                or params.get('thread_id') != active.thread_id or params.get('project_id') != active.project_id
                or params.get('message') != queued.message):
            raise ValueError('Original Home action changed')
        key = f'{attempt.id}:{receipt.provider_native_session_id}:{receipt.provider_native_turn_id}'
        existing = self.events.bus.store.event(self.events._event_id(SOURCE, key))
        if existing is not None:
            return None  # Immutable observation; no subscription replay. Only a
            # configured durable transport owns delivery retries.
        usage = None
        if self.sessions is not None and self.usage is not None:
            session = self.sessions.find_unique_by_native_id(active.thread_id,
                provider_id=receipt.provider_id, runtime_id=receipt.runtime_id)
            if session is not None:
                if (session.organization_id, session.workspace_id, session.project_id, session.assignment_id,
                        session.execution_id, session.execution_workspace_id) != (attempt.organization_id, attempt.workspace_id,
                        active.project_id, active.assignment_id, active.execution_id, active.execution_workspace_id):
                    raise ValueError('Home child session attribution changed')
                usage_id = 'runtime-usage-' + hashlib.sha256(f'{session.id}\n{active.turn_id}'.encode()).hexdigest()[:32]
                usage = self.usage.get(usage_id)
                if usage is not None and ((usage.organization_id,usage.workspace_id,usage.project_id,usage.assignment_id,
                        usage.execution_id,usage.execution_workspace_id,usage.agent_session_id,usage.provider_id,usage.runtime_id,
                        usage.provider_native_session_id,usage.provider_native_turn_id) !=
                        (attempt.organization_id,attempt.workspace_id,active.project_id,active.assignment_id,
                         active.execution_id,active.execution_workspace_id,session.id,receipt.provider_id,receipt.runtime_id,
                         active.thread_id,active.turn_id)):
                    raise ValueError('Home child usage attribution changed')
        payload = dict(transition='home_child_native_terminal', project_id=active.project_id,
            thread_id=active.thread_id, native_turn_id=active.turn_id, execution_id=active.execution_id,
            assignment_id=active.assignment_id, execution_workspace_id=active.execution_workspace_id,
            worker_id=active.worker_id, fence=active.fence, provider_id=receipt.provider_id, runtime_id=receipt.runtime_id,
            parent_intent_id=proof.intent_id, parent_intent_status=str(intent.status), home_assignment_id=proof.assignment_id, home_revision=proof.home_revision,
            admission_attempt_id=attempt.id, queued_turn_id=queued.id,
            completion_succeeded=bool(succeeded), outcome='unknown', usage_available=False)
        if usage is not None and usage.completed_at is not None and usage.terminal_outcome != 'unknown':
            payload.update(outcome=str(usage.terminal_outcome), usage_available=True, usage_id=usage.id,
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                runtime_duration_seconds=usage.runtime_duration_seconds, usage_completed_at=usage.completed_at, telemetry_completeness=str(usage.telemetry_completeness))
        return key, payload, attempt.organization_id, attempt.workspace_id

    async def record_terminal(self, active, *, succeeded):
        fact = await asyncio.to_thread(self._fact, active, succeeded)
        if fact is not None:
            key, payload, tenant, workspace = fact
            await self.events.ingest(event_type='work.transition', source=SOURCE, idempotency_key=key,
                payload=payload, correlation_id=active.execution_id, causation_id=payload['parent_intent_id'],
                tenant_id=tenant, workspace_id=workspace)
