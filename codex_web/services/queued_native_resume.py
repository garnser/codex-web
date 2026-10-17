"""Assignment-bound pre-turn resume observations; never a provider retry path."""
from __future__ import annotations

from codex_web.models import (
    QueuedNativeResumeCorrelation, QueuedNativeResumeEvidence,
    QueuedNativeResumeObservation, ExecutionPreflightBlocker,
    ExecutionPreflightBlockerSnapshot,
)
from codex_web.services.queued_native_start import QueuedNativeStartCapture, validate_attribution


def validate_resume_observation(service, current):
    correlation, evidence = current.queued_resume_correlation, current.queued_resume_evidence
    if correlation is None or evidence is None:
        raise ValueError('no exactly correlated resume observation')
    validate_attribution(service, current, correlation)
    if (correlation.rpc_method != 'thread/resume' or correlation.runtime_version != '0.160.0'
            or any(getattr(correlation, name) != getattr(evidence, name) for name in (
                'runtime_generation', 'rpc_request_id', 'provider_native_session_id'))):
        raise ValueError('resume observation attribution changed')
    return correlation, evidence


def reconcile_captured_resume(service, attempt_id):
    """Explicit authorized inspector calls this; never starts/resumes/requeues."""
    def reconcile(current):
        correlation, evidence = validate_resume_observation(service, current)
        if (evidence.outcome != 'archived_refusal' or current.queued_start_correlation is not None
                or current.queued_start_evidence is not None or current.queued_start_receipt is not None
                or current.started_at is not None or current.scheduling_retirement is not None
                or current.status not in {'reconciliation_required', 'blocked'}):
            raise ValueError('resume refusal conflicts with native start disposition')
        if current.status == 'blocked':
            return current
        blocker = ExecutionPreflightBlocker(code='native_resume_archived_refusal',
            message='Pinned native thread/resume explicitly refused the archived session before turn/start.',
            correlation=correlation.model_dump(mode='json'))
        history = ExecutionPreflightBlockerSnapshot(blockers=(blocker,),
            recorded_at=float(service.clock()), attempt_number=current.attempt_number)
        return current.model_copy(update={'status':'blocked', 'blockers':(blocker,),
            'blocker_history':(*current.blocker_history, history)[-service.MAX_HISTORY:],
            'updated_at':float(service.clock())})
    return service.store.update(attempt_id, reconcile)


class QueuedNativeResumeCapture(QueuedNativeStartCapture):
    rpc_method = 'thread/resume'
    correlation_field = 'queued_resume_correlation'
    evidence_field = 'queued_resume_evidence'
    correlation_type = QueuedNativeResumeCorrelation

    def prepare(self, *, generation, rpc_request_id, native_session_id, runtime_version=None):
        if runtime_version != '0.160.0':
            raise ValueError('native resume evidence requires the qualified runtime version')
        # A new explicit retry may retain a prior consumed refusal. Preserve its
        # complete typed provenance before recording the new claim, never erase it.
        def archive(current):
            if current.queued_start_correlation is not None or current.queued_start_evidence is not None:
                raise ValueError("Resume capture cannot follow a possible turn/start")
            previous = current.queued_resume_correlation
            if previous is None:
                return current
            evidence = current.queued_resume_evidence
            if (previous.admission_claim_id == self.claim_id or evidence is None
                    or evidence.outcome != 'archived_refusal'
                    or current.status != 'reconciliation_required'
                    or current.queued_admission_claim_id != self.claim_id
                    or current.queued_start_correlation is not None
                    or current.queued_start_receipt is not None or current.started_at is not None
                    or current.scheduling_retirement is not None
                    or len(current.queued_resume_history) >= 8
                    or not any(blocker.code == 'native_resume_archived_refusal'
                        and blocker.model_dump(mode='json').get('correlation') == previous.model_dump(mode='json')
                        for snapshot in current.blocker_history for blocker in snapshot.blockers)
                    or previous.snapshot_sha256 != self.service.queued_snapshot_digest(current.queued_turn_snapshot)
                    or any(getattr(previous, name) != getattr(evidence, name) for name in (
                        'runtime_generation', 'rpc_request_id', 'provider_native_session_id'))):
                raise ValueError('prior resume outcome requires explicit reconciliation')
            observed = QueuedNativeResumeObservation(correlation=previous, evidence=evidence)
            return current.model_copy(update={'queued_resume_correlation':None,
                'queued_resume_evidence':None, 'queued_resume_history':(*current.queued_resume_history, observed)})
        self.service.store.update(self.attempt_id, archive)
        return super().prepare(generation=generation, rpc_request_id=rpc_request_id,
            native_session_id=native_session_id, runtime_version=runtime_version)

    def observe(self, correlation, *, generation, message):
        if (generation != correlation.runtime_generation or message.get('id') != correlation.rpc_request_id
                or 'method' in message or correlation.runtime_version != '0.160.0'):
            return False
        sid = correlation.provider_native_session_id
        # Exact qualified protocol and method, not a generic error-code/string
        # classifier. Ambiguous, mixed, foreign and malformed results stay unknown.
        if set(message) == {'id', 'error'} and message['error'] == {
                'code':-32600,
                'message':f'session {sid} is archived. Run `codex unarchive {sid}` to unarchive it first.'}:
            outcome = 'archived_refusal'
        elif ('error' not in message and isinstance(message.get('result'), dict)
                and isinstance(message['result'].get('thread'), dict)
                and message['result']['thread'].get('id') == sid
                and 'error' not in message['result']):
            outcome = 'resumed'
        else:
            return False
        evidence = QueuedNativeResumeEvidence(runtime_generation=generation,
            rpc_request_id=correlation.rpc_request_id, provider_native_session_id=sid,
            outcome=outcome, observed_at=float(self.service.clock()))
        def capture(current):
            validate_attribution(self.service, current, correlation)
            if current.queued_resume_correlation != correlation:
                raise ValueError('resume correlation changed')
            previous = current.queued_resume_evidence
            if previous is not None:
                if previous.model_dump(exclude={'observed_at'}) != evidence.model_dump(exclude={'observed_at'}):
                    raise ValueError('conflicting resume observation')
                return current
            if (current.status != 'reconciliation_required' or current.queued_start_correlation is not None
                    or current.queued_start_receipt is not None or current.started_at is not None):
                raise ValueError('resume disposition changed')
            return current.model_copy(update={'queued_resume_evidence':evidence})
        self.service.store.update(self.attempt_id, capture)
        return True
