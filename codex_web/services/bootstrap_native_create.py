"""Assignment-owned native creation observations; no recovery or provider I/O."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from codex_web.execution_workers import BootstrapCreateObservation


def attribution(assignment):
    # A hash binds the complete immutable admission without copying secrets or
    # mutable lease/status into historical observation metadata.
    ignored = {'status', 'fence', 'lease', 'updated_at', 'completed_at', 'failure',
        'failure_code', 'failure_message', 'bootstrap_create_observation',
        'artifact_ids', 'evidence_ids', 'procedure_checkpoint', 'procedure_history',
        'procedure_discoveries'}
    return hashlib.sha256(json.dumps(assignment.model_dump(mode='json', exclude=ignored),
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def update_observation(service, original, actor, updater, *, preparing=False):
    """Narrow historical append: cannot renew, rebind, reclassify or execute."""
    service._require_admin(actor)
    result = []
    def apply(state):
        current = service._assignment(state, original.id, actor)
        live = (str(current.status) == 'running' and current.fence == original.fence)
        cancelled = (str(current.status) == 'cancelled'
            and current.fence == original.fence + 1 and current.lease is None
            and current.failure_code == 'thread_bootstrap_cancelled')
        if (current.subject is None or current.subject.kind != 'thread_bootstrap'
                or current.assigned_worker_id != original.assigned_worker_id
                or attribution(current) != attribution(original)
                or not (live or (cancelled and not preparing))):
            raise ValueError('bootstrap observation original custody changed')
        if preparing and (current.lease is None or current.lease.expires_at <= time.time()
                or current.lease.worker_id != original.assigned_worker_id
                or current.lease.fence != original.fence):
            raise ValueError('bootstrap observation lease is not live')
        previous = current.bootstrap_create_observation
        observed = updater(previous)
        if observed is not None:
            observed = BootstrapCreateObservation.model_validate(observed.model_dump(mode='json'))
        current = current.model_copy(update={'bootstrap_create_observation': observed})
        state.assignments = [current]
        result.append(observed)
        return state
    service.store.transition_assignment(original.assigned_worker_id, original.id, apply)
    return result[0]


@dataclass(frozen=True)
class BootstrapNativeCreateCapture:
    rpc_method: ClassVar[str] = 'thread/start'
    service: Any
    actor: Any
    assignment: Any = None
    validator: Any = None

    def bind_assignment(self, assignment, *, worker_id, fence, validator):
        if (assignment.subject is None or assignment.subject.kind != 'thread_bootstrap'
                or str(assignment.status) != 'running' or assignment.lease is None
                or assignment.fence != fence or assignment.assigned_worker_id != worker_id
                or assignment.lease.worker_id != worker_id or assignment.lease.fence != fence):
            raise ValueError('bootstrap capture requires current original custody')
        return replace(self, assignment=assignment.model_copy(deep=True), validator=validator)

    def prepare(self, *, generation, rpc_request_id, native_session_id, process_pid=None):
        if self.assignment is None or native_session_id is not None:
            raise ValueError('bootstrap create capture must be assignment bound')
        current = self.validator()
        if current.id != self.assignment.id or current.fence != self.assignment.fence:
            raise ValueError('bootstrap create admission changed')
        observation = BootstrapCreateObservation(
            assignment_id=self.assignment.id, execution_id=self.assignment.execution_id,
            execution_workspace_id=self.assignment.execution_workspace_id,
            worker_id=self.assignment.assigned_worker_id, fence=self.assignment.fence,
            attribution_sha256=attribution(self.assignment), runtime_generation=generation,
            rpc_request_id=rpc_request_id, process_pid=process_pid, prepared_at=time.time())
        def reserve(previous):
            if previous is not None:
                raise ValueError('bootstrap create correlation already reserved; outcome may be unknown')
            return observation
        return update_observation(self.service, self.assignment, self.actor, reserve, preparing=True)

    def validate_before_send(self, correlation):
        current = self.validator()
        if (current.id != self.assignment.id or current.fence != correlation.fence
                or current.bootstrap_create_observation != correlation
                or attribution(current) != correlation.attribution_sha256):
            raise ValueError('bootstrap create send custody changed')

    def observe(self, correlation, *, generation, message):
        if (generation != correlation.runtime_generation or message.get('id') != correlation.rpc_request_id
                or 'method' in message or ('result' in message and 'error' in message)):
            return False
        result, error = message.get('result'), message.get('error')
        if isinstance(result, dict) and error is None and 'error' not in result:
            thread = result.get('thread', result)
            if not isinstance(thread, dict) or thread.get('error') is not None:
                return False
            native_id = thread.get('id') or thread.get('threadId')
            if thread.get('id') and thread.get('threadId') and thread['id'] != thread['threadId']:
                return False
            if (not isinstance(native_id, str) or not native_id.strip()
                    or native_id != native_id.strip() or len(native_id) > 256):
                return False
            changes = {'outcome': 'created', 'provider_native_session_id': native_id}
        elif result is None and isinstance(error, dict) and type(error.get('code')) is int:
            # An error response is not a no-effects witness. Retain only the
            # protocol code; never raw provider messages/data or credentials.
            changes = {'outcome': 'error_response', 'provider_error_code': error['code']}
        else:
            return False
        def capture(previous):
            if previous is None or previous.model_dump(exclude={
                    'outcome', 'provider_native_session_id', 'provider_error_code',
                    'observed_at', 'cleanup_joined_at', 'process_exit_code'}) != correlation.model_dump(exclude={
                    'outcome', 'provider_native_session_id', 'provider_error_code',
                    'observed_at', 'cleanup_joined_at', 'process_exit_code'}):
                raise ValueError('bootstrap create correlation changed')
            if previous.outcome != 'unknown':
                if any(getattr(previous, key) != value for key, value in changes.items()):
                    raise ValueError('conflicting bootstrap create response')
                return previous
            return previous.model_copy(update={**changes, 'observed_at': time.time()})
        update_observation(self.service, self.assignment, self.actor, capture)
        return True


def record_joined_cleanup(service, original, actor, *, runtime_generation, process, status):
    """Called after actual session.stop joins; absence is never a stop witness."""
    if (process is None or status.running or status.pending_executor_work != 0
            or status.worker_id != original.assigned_worker_id or status.fence != original.fence):
        return
    exit_code = process.poll()
    if type(exit_code) is not int:
        return
    def joined(previous):
        if previous is None:
            return None  # Historical assignments acquire no invented correlation.
        if (previous.runtime_generation != runtime_generation or previous.process_pid != process.pid
                or previous.fence != original.fence):
            raise ValueError('bootstrap cleanup process generation changed')
        if previous.cleanup_joined_at is not None:
            if previous.process_exit_code != exit_code:
                raise ValueError('bootstrap cleanup exit changed')
            return previous
        return previous.model_copy(update={'cleanup_joined_at': time.time(), 'process_exit_code': exit_code})
    update_observation(service, original, actor, joined)
