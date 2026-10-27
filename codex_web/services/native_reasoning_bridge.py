"""One bounded inference through the existing canonical Thread/Turn owners.

The bridge owns no durable queue, worker, session, credential or retry authority.
A failed/unknown original is retained by normal bootstrap/preflight owners.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import dataclass

from codex_web.model_gateway import ModelProviderResult, ModelProviderUsage
from codex_web.model_providers import ModelProviderAdapterError, ModelProviderOutcomeUnknownError
from codex_web.models import TurnCreate
from codex_web.services.post_turn_readiness import PostTurnReadinessService
from codex_web.services.queued_native_start import validate_attribution
from codex_web.services.threads import bootstrap_exception_type, bootstrap_preparation_diagnostic


_LIFECYCLE_PHASES = frozenset({
    'initial_current_guard', 'authentication_metadata', 'post_authentication_current_guard',
    'thread_bootstrap', 'session_lookup', 'session_binding', 'assignment_lookup',
    'assignment_validation', 'session_attestation', 'input_preparation',
    'queued_admission', 'terminal_observation', 'usage_observation',
    'completion_observation', 'result_attribution', 'assignment_completion', 'cleanup',
})


class NativeReasoningPrestartRefused(ModelProviderAdapterError):
    """The original canonical queue owner retained a bound preinput refusal."""


@dataclass(frozen=True, slots=True)
class NativeReasoningBinding:
    provider_id: str
    model_id: str
    concrete_model: str
    agent_profile_id: str
    agent_profile_revision: int
    execution_profile_id: str
    worker_id: str
    max_output_bytes: int = 65536

    def __post_init__(self):
        if (not all((self.provider_id, self.model_id, self.concrete_model,
                     self.agent_profile_id, self.execution_profile_id, self.worker_id))
                or self.agent_profile_revision < 1 or not 1 <= self.max_output_bytes <= 1048576):
            raise ValueError('Exact bounded native reasoning binding required')


class NativeReasoningText:
    """Bounded native response parsing; commentary is not the final answer."""
    def __init__(self, max_bytes):
        self.max_bytes, self.observed_bytes = max_bytes, 0
        self.completed = []

    def observe(self, method, params):
        if method == 'item/agentMessage/delta':
            value = params.get('delta')
            if isinstance(value, str):
                self.observed_bytes += len(value.encode())
        elif method == 'item/completed':
            item = params.get('item') or {}
            if item.get('type') == 'agentMessage':
                value = item.get('text')
                if not isinstance(value, str) or len(self.completed) >= 64:
                    raise ValueError('Native completed answer unavailable or oversized')
                self.observed_bytes += len(value.encode())
                self.completed.append((item.get('phase'), value))
        if self.observed_bytes > self.max_bytes:
            raise ValueError('Native output observation limit exceeded')

    def result(self):
        final = [text for phase,text in self.completed if phase in {'final_answer', 'final'}]
        eligible = [text for phase,text in self.completed if phase != 'commentary']
        if len(final) > 1 or not (final or eligible):
            raise ValueError('Native final answer missing or ambiguous')
        return final[0] if final else eligible[-1]


class CanonicalNativeReasoningBridge:
    """Opt-in composition; attestation must inspect the actual serving session."""

    runtime_id = 'codex-reasoning'

    def __init__(self, *, binding, threads, turns, sessions, usage, preflight,
                 execution, hub, attest_session, authentication_preparation=None):
        self.binding = binding
        self.threads, self.turns = threads, turns
        self.sessions, self.usage, self.preflight = sessions, usage, preflight
        self.execution, self.hub = execution, hub
        # The configured canonical credential/backend owner verifies the exact
        # version, restricted catalog/config, scratch contract, worker and lease.
        # A deployment assertion or model/provider metadata is insufficient.
        self.attest_session = attest_session
        self.authentication_preparation = authentication_preparation
        self._usage_waiters = {}
        self._admission_waiters = {}

    def observe_admission(self, attempt):
        # This notification only wakes the owner. It is never the refusal proof;
        # _wait_original_terminal re-reads the exact committed original below.
        waiting = self._admission_waiters.get(attempt.execution_id)
        if waiting is None or not self.preflight.recorded_reasoning_refusal(attempt):
            return
        loop, future = waiting
        def signal():
            if not future.done():
                future.set_result(attempt.id)
        loop.call_soon_threadsafe(signal)

    async def _wait_original_terminal(self, terminal, refused, cancelled, *,
                                      context, thread_id, queued_id):
        waiter = asyncio.create_task(cancelled.wait())
        try:
            done, _ = await asyncio.wait((terminal, refused, waiter),
                                        return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                raise asyncio.CancelledError
            if refused in done:
                def read_refusal():
                    attempt = self.preflight.store.get('preflight-queued-' + queued_id)
                    if (attempt is None or attempt.id != refused.result()
                            or (attempt.organization_id, attempt.workspace_id, attempt.project_id,
                                attempt.execution_id, attempt.thread_id) !=
                               (context.organization_id, context.workspace_id, context.project_id,
                                context.execution_id, thread_id)
                            or not self.preflight.recorded_reasoning_refusal(attempt)
                            or hashlib.sha256(attempt.queued_turn_snapshot['message'].encode()).hexdigest()
                               != context.input_message_sha256):
                        raise ModelProviderOutcomeUnknownError(
                            'Native original admission attribution changed')
                    return attempt
                await self._owned(read_refusal)
                raise NativeReasoningPrestartRefused(
                    'Native reasoning preparation refused; inspect original preflight')
            return terminal.result()
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    def observe_usage(self, record):
        # Notification is not permission or accounting authority: _result reads
        # and validates the original committed record again before returning.
        waiting = self._usage_waiters.get(record.agent_session_id)
        if waiting is None or record.runtime_id != self.runtime_id:
            return
        loop, future = waiting
        if (record.terminal_outcome == 'succeeded' and record.model_call_count == 1
                and record.input_tokens is not None and record.output_tokens is not None):
            def signal():
                if not future.done():
                    future.set_result(record.provider_native_turn_id)
            loop.call_soon_threadsafe(signal)

    def supports(self, provider, model, context):
        b = self.binding
        return (provider.id == b.provider_id and model.id == b.model_id
                and model.concrete_model == b.concrete_model and not model.supports_tools
                and bool(context.project_id) and callable(self.attest_session))

    @staticmethod
    async def _owned(function, *args):
        return await PostTurnReadinessService._run_owned(
            lambda: asyncio.to_thread(function, *args), seconds=5., name='native-model-canonical-read')

    @staticmethod
    async def _wait_or_cancel(future, cancelled):
        waiter = asyncio.create_task(cancelled.wait())
        try:
            done, _ = await asyncio.wait((future, waiter), return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                raise asyncio.CancelledError
            return future.result()
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    def _preparation_failed(self, context, stage, exc):
        # This is diagnostic evidence, not permission to retry or proof that a
        # provider thread was never created. Only ThreadService owns that proof.
        try:
            event = dict(type='native_reasoning_preparation_failed',
                organization_id=context.organization_id, workspace_id=context.workspace_id,
                project_id=context.project_id, execution_id=context.execution_id,
                activation_id=context.activation_id, role_id=context.role_id,
                native_preparation_stage=stage, native_exception_type=bootstrap_exception_type(exc),
                native_input_admitted=False)
            event.update(bootstrap_preparation_diagnostic(exc) or {})
            self.threads.event_sink(event)
        except Exception:
            # An unavailable diagnostic sink must not change the original error,
            # its cleanup ownership, or the no-replay outcome.
            pass

    @staticmethod
    def _lifecycle_phase(checkpoint, phase, **identifiers):
        checkpoint.update(phase=phase, phase_started_at=time.monotonic(), **identifiers)

    def _lifecycle_interrupted(self, context, checkpoint, reason, error=None):
        # Diagnostic only: queue submission is not canonical native START, and
        # missing terminal/usage evidence remains UNKNOWN rather than retryable.
        try:
            if checkpoint.get('reported'):
                return
            checkpoint['reported'] = True
            now = time.monotonic()
            elapsed = now - checkpoint['started_at']
            phase_elapsed = now - checkpoint['phase_started_at']
            if not (math.isfinite(elapsed) and math.isfinite(phase_elapsed)):
                return
            phase = checkpoint['phase']
            if phase not in _LIFECYCLE_PHASES or reason not in {'cancelled', 'timeout', 'failed'}:
                return
            event = dict(type='native_reasoning_lifecycle_interrupted',
                native_lifecycle_phase=phase, interruption=reason,
                elapsed_seconds=min(86400., max(0., elapsed)),
                phase_elapsed_seconds=min(86400., max(0., phase_elapsed)),
                native_input_requested=checkpoint.get('input_requested', False),
                native_queue_submitted=checkpoint.get('queue_submitted', False))
            if reason == 'failed':
                event['native_exception_type'] = bootstrap_exception_type(error)
            for key in ('organization_id', 'workspace_id', 'project_id', 'execution_id',
                        'activation_id', 'role_id'):
                value = getattr(context, key, None)
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value):
                    event[key] = value
            for key in ('thread_id', 'session_id', 'assignment_id', 'queued_id', 'turn_id'):
                value = checkpoint.get(key)
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value):
                    event[key] = value
            self.threads.event_sink(event)
        except (Exception, asyncio.CancelledError):
            # A failed diagnostic must never replace cancellation/the original
            # timeout or interrupt the owned process's positive cleanup join.
            pass

    def _result(self, *, context, actor, session, assignment, thread_id,
                turn_id, queued_id, text):
        context.validate_current()
        attempt = self.preflight.store.get('preflight-queued-' + queued_id)
        if attempt is None or attempt.status != 'started':
            raise ValueError('Native inference positive original START unavailable')
        correlation, receipt = attempt.queued_start_correlation, attempt.queued_start_receipt
        evidence = attempt.queued_start_evidence
        if correlation is None or receipt is None or evidence is None:
            raise ValueError('Native inference START evidence unavailable')
        validate_attribution(self.preflight, attempt, correlation)
        if ((attempt.organization_id, attempt.workspace_id, attempt.project_id,
             attempt.execution_id, receipt.thread_id, receipt.provider_native_session_id,
             receipt.provider_native_turn_id, correlation.assignment_id,
             correlation.worker_id, correlation.fence) !=
            (context.organization_id, context.workspace_id, context.project_id,
             context.execution_id, thread_id, thread_id, turn_id, assignment.id,
             assignment.assigned_worker_id, assignment.fence)
                or (correlation.provider_id,correlation.runtime_id,receipt.provider_id,receipt.runtime_id) !=
                   ('openai',self.runtime_id,'openai',self.runtime_id)
                or receipt.execution_id != assignment.execution_id
                or correlation.execution_id != assignment.execution_id
                or evidence.provider_native_turn_id != turn_id
                or evidence.provider_native_session_id != thread_id):
            raise ValueError('Native inference original START attribution changed')
        record_id = 'runtime-usage-' + hashlib.sha256(
            f'{session.id}\n{turn_id}'.encode()).hexdigest()[:32]
        usage = self.usage.get(record_id)
        if (usage is None or (usage.organization_id, usage.workspace_id, usage.project_id,
                usage.agent_session_id, usage.assignment_id, usage.execution_id,
                usage.provider_id, usage.runtime_id, usage.provider_native_session_id,
                usage.provider_native_turn_id) !=
                (context.organization_id, context.workspace_id, context.project_id,
                 session.id, assignment.id, assignment.execution_id,
                 'openai', self.runtime_id, thread_id, turn_id)
                or usage.terminal_outcome != 'succeeded'
                or usage.runtime_duration_seconds is None
                or usage.runtime_duration_seconds > context.max_duration_seconds
                or any(m != self.binding.concrete_model for m in usage.observed_model_ids)
                or usage.model_call_count != 1
                or usage.input_tokens is None or usage.output_tokens is None
                or usage.tool_call_count or usage.shell_command_count or usage.file_edit_count
                or usage.git_operation_count or usage.subagent_count
                or not context.observed_tokens_allowed(usage.input_tokens, usage.output_tokens)):
            raise ValueError('Native inference terminal measured usage unavailable or outside budget')
        return ModelProviderResult(text=text,
            usage=ModelProviderUsage(input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens),
            provider_request_id=f'{session.id}/{turn_id}', stop_reason='completed')

    async def invoke(self, provider, model, request, *, actor, context):
        cancelled = asyncio.Event()
        started_at = time.monotonic()
        lifecycle = dict(started_at=started_at, phase_started_at=started_at,
                         phase='initial_current_guard')
        owner = asyncio.create_task(self._invoke_owned(provider, model, request,
            actor=actor, context=context, cancelled=cancelled, lifecycle=lifecycle),
            name='canonical-native-model-owner')
        try:
            # wait() never propagates cancellation into this owned task. Unlike
            # a detached shield Future, its exception is observed by this owner.
            await asyncio.wait((owner,))
            return owner.result()
        except TimeoutError:
            self._lifecycle_interrupted(context, lifecycle, 'timeout')
            raise
        except asyncio.CancelledError:
            self._lifecycle_interrupted(context, lifecycle, 'cancelled')
            cancelled.set()
            if context is not None and context.process_owner is not None:
                context.process_owner.cancelled.set()
            # Preparation may own an actual worker process before returning its
            # thread DTO. Join it; its cancellation edge forbids the first input.
            while not owner.done():
                try:
                    await asyncio.wait((owner,))
                except asyncio.CancelledError:
                    cancelled.set()
                except Exception:
                    break
            if owner.done() and not owner.cancelled():
                owner.exception()
            raise
        except NativeReasoningPrestartRefused:
            raise
        except Exception as exc:
            # Also observe an ordinary initial guard/cleanup failure. The first
            # snapshot wins when an admitted failure was recorded before cleanup.
            self._lifecycle_interrupted(context, lifecycle, 'failed', exc)
            raise

    async def _invoke_owned(self, provider, model, request, *, actor, context, cancelled, lifecycle=None):
        if lifecycle is None:
            started_at = time.monotonic()
            lifecycle = dict(started_at=started_at, phase_started_at=started_at,
                             phase='initial_current_guard')
        if not self.supports(provider, model, context):
            raise ModelProviderAdapterError('Native reasoning binding unavailable')
        try:
            await self._owned(context.validate_current)
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                self._lifecycle_interrupted(context, lifecycle, 'timeout')
            self._preparation_failed(context, 'initial_current_guard', exc)
            raise
        b = self.binding
        manager = serving = None
        assignment = session = None
        native_admitted = False
        native_runtime = None
        usage_ready = None
        thread_id = None
        terminal = asyncio.get_running_loop().create_future()
        refused = asyncio.get_running_loop().create_future()
        text = NativeReasoningText(b.max_output_bytes)
        loop = asyncio.get_running_loop()

        def finish(value=None, error=None):
            if not terminal.done():
                terminal.set_exception(error) if error is not None else terminal.set_result(value)

        def observe_on_loop(event):
            if event.get('type') != 'codex.event' or terminal.done():
                return
            message = event.get('message') or {}
            params = message.get('params') or {}
            turn = params.get('turn') or {}
            if (params.get('threadId') or turn.get('threadId')) != thread_id:
                return
            method = message.get('method')
            try:
                text.observe(method, params)
            except ValueError:
                finish(error=ModelProviderOutcomeUnknownError('Native output observation refused'))
                return
            if method in {'item/started','item/completed'}:
                item = params.get('item') or {}
                if item.get('type') not in {'userMessage', 'agentMessage', 'reasoning', 'plan', 'contextCompaction'}:
                    finish(error=ModelProviderOutcomeUnknownError(
                        'Unexpected native operational item refused'))
            elif method == 'thread/tokenUsage/updated':
                from codex_web.services.agent_runtime_telemetry import AgentRuntimeTelemetryService
                measured = AgentRuntimeTelemetryService._usage_fields(message)
                if not context.observed_tokens_allowed(measured['input_tokens'], measured['output_tokens']):
                    loop.call_soon_threadsafe(finish, None, ModelProviderOutcomeUnknownError(
                        'Native observed token budget exceeded'))
            elif method == 'turn/completed':
                turn_id = params.get('turnId') or turn.get('id')
                if turn.get('status') != 'completed' or turn.get('error'):
                    loop.call_soon_threadsafe(finish, None,
                        ModelProviderOutcomeUnknownError('Native inference did not complete successfully'))
                elif isinstance(turn_id, str):
                    loop.call_soon_threadsafe(finish, turn_id)

        def observe(event):
            loop.call_soon_threadsafe(observe_on_loop, event)

        self.hub.subscribe_filtered(observe, event_types=('codex.event',))
        preparation_stage = 'thread_bootstrap'
        self._lifecycle_phase(lifecycle, preparation_stage)
        try:
            if self.authentication_preparation is not None:
                # Scope was checked above. Reuse the canonical coalesced account
                # metadata owner before the existing authentication preflight;
                # never infer sign-in from transport readiness or stale evidence.
                preparation_stage = 'authentication_metadata'
                self._lifecycle_phase(lifecycle, preparation_stage)
                await PostTurnReadinessService._run_owned(
                    self.authentication_preparation, seconds=5.,
                    name='native-authentication-metadata')
                preparation_stage = 'post_authentication_current_guard'
                self._lifecycle_phase(lifecycle, preparation_stage)
                await self._owned(context.validate_current)
                if cancelled.is_set():
                    raise asyncio.CancelledError
                preparation_stage = 'thread_bootstrap'
                self._lifecycle_phase(lifecycle, preparation_stage)
            # Stable bootstrap correlation is provenance, never permission to
            # retry an unknown invocation. Executive owns the single reservation.
            bootstrap = uuid.uuid5(uuid.NAMESPACE_URL, context.execution_id).hex
            response = await self.threads.create(project_id=context.project_id,
                sandbox='read-only', approval_policy='never', model=b.concrete_model,
                provider_id='openai', runtime_id=self.runtime_id,
                execution_profile_id=b.execution_profile_id,
                agent_profile_id=b.agent_profile_id, agent_profile_revision=b.agent_profile_revision,
                actor=actor, bootstrap_token=bootstrap, admission_guard=context.validate_current)
            thread_id = response['thread']['id']
            preparation_stage = 'session_lookup'
            self._lifecycle_phase(lifecycle, preparation_stage, thread_id=thread_id,
                                  session_id=response['agentSessionId'])
            context.process_owner.bind_native_session(thread_id)
            session = await self._owned(lambda: self.sessions.get(response['agentSessionId'],
                organization_id=context.organization_id, workspace_id=context.workspace_id))
            preparation_stage = 'session_binding'
            self._lifecycle_phase(lifecycle, preparation_stage, assignment_id=session.assignment_id)
            if (session.provider_id != 'openai' or session.runtime_id != self.runtime_id
                    or session.provider_native_session_id != thread_id
                    or session.project_id != context.project_id or session.resource_ids
                    or session.worker_id != b.worker_id or session.model != b.concrete_model):
                raise ValueError('Native reasoning canonical session binding changed')
            preparation_stage = 'assignment_lookup'
            self._lifecycle_phase(lifecycle, preparation_stage)
            manager, serving = self.execution._session_for_assignment(session.assignment_id)
            native_runtime = serving.runtime
            usage_ready = loop.create_future()
            self._usage_waiters[session.id] = (loop, usage_ready)
            preparation_stage = 'assignment_validation'
            self._lifecycle_phase(lifecycle, preparation_stage)
            assignment = await self._owned(serving.validate_current)
            preparation_stage = 'session_attestation'
            self._lifecycle_phase(lifecycle, preparation_stage)
            await self._owned(lambda: self.attest_session(serving, assignment, context, model))
            # JSON keeps individual message boundaries as data. The system
            # policy is separately installed by the restricted native runtime.
            preparation_stage = 'input_preparation'
            self._lifecycle_phase(lifecycle, preparation_stage)
            message = json.dumps([item.model_dump(mode='json') for item in request.messages],
                                 ensure_ascii=False, separators=(',', ':'))
            if cancelled.is_set():
                raise asyncio.CancelledError
            native_admitted = True  # Subsequent unknowns cannot be replayed.
            self._lifecycle_phase(lifecycle, 'queued_admission', input_requested=True)
            self._admission_waiters[context.execution_id] = (loop, refused)
            started = await self.turns.start(thread_id, TurnCreate(message=message,
                project_id=context.project_id, sandbox='read-only', approval_policy='never',
                model=b.concrete_model, reasoning_effort=request.reasoning_effort,
                execution_profile_id=b.execution_profile_id, agent_profile_id=b.agent_profile_id,
                agent_profile_revision=b.agent_profile_revision, defer_start=True),
                actor=actor, execution_id=context.execution_id,
                admission_guard=context.validate_current, allow_assignment_renewal=False)
            queued_id = started['queuedId']
            self._lifecycle_phase(lifecycle, 'terminal_observation', queued_id=queued_id,
                                  queue_submitted=True)
            # A disposition may commit before TurnService returns its queue ID.
            # The notification was installed first; the result remains subject
            # to an exact canonical re-read, including absence of any START.
            turn_id = await self._wait_original_terminal(terminal, refused, cancelled,
                context=context, thread_id=thread_id, queued_id=queued_id)
            self._lifecycle_phase(lifecycle, 'usage_observation', turn_id=turn_id)
            observed_turn = await self._wait_or_cancel(usage_ready, cancelled)
            if observed_turn != turn_id or native_runtime.reasoning_refusal:
                raise ValueError('Native usage or restricted request attribution changed')
            completion = self.execution.thread_completion_tasks.get(thread_id)
            self._lifecycle_phase(lifecycle, 'completion_observation')
            if completion is not None:
                await asyncio.shield(completion)
            self._lifecycle_phase(lifecycle, 'result_attribution')
            result = await self._owned(lambda: self._result(context=context, actor=actor,
                session=session, assignment=assignment, thread_id=thread_id,
                turn_id=turn_id, queued_id=queued_id, text=text.result()))
            # Only a positively attributed completed inference can release its
            # ephemeral scratch custody. Unknown originals remain held.
            self._lifecycle_phase(lifecycle, 'assignment_completion')
            await manager.complete(assignment.id, succeeded=True)
            return result
        except asyncio.CancelledError:
            raise
        except NativeReasoningPrestartRefused:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                self._lifecycle_interrupted(context, lifecycle, 'timeout')
            elif native_admitted:
                # Observe the original category before UNKNOWN wrapping and
                # before cleanup changes the phase. This grants no result or retry.
                self._lifecycle_interrupted(context, lifecycle, 'failed', exc)
            if native_admitted:
                raise ModelProviderOutcomeUnknownError(
                    'Native inference original retained; inspect canonical START/usage') from exc
            self._preparation_failed(context, preparation_stage, exc)
            raise ModelProviderAdapterError('Native reasoning preparation refused') from exc
        finally:
            self.hub.unsubscribe(observe)
            if terminal.done() and not terminal.cancelled():
                terminal.exception()  # Observe any simultaneous cancellation/terminal error.
            if session is not None:
                self._usage_waiters.pop(session.id, None)
            self._admission_waiters.pop(context.execution_id, None)
            stop = (context.process_owner.stop if context.process_owner is not None else None)
            if stop is None and manager is not None and assignment is not None:
                stop = lambda: manager.stop(assignment.id)
            if stop is not None:
                self._lifecycle_phase(lifecycle, 'cleanup')
                # Existing stop owns watchdog/process interruption and positive
                # reap, including failures before thread/create returns a DTO.
                cleanup = asyncio.create_task(stop())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
