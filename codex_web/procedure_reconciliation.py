"""Internal read-only recovery context; serialized evidence never grants admission."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import quote


RECOVERY_PREFIX = 'canonical-readonly-reconciliation:1:'
MAX_RECOVERY_ATTEMPTS = 4
MAX_RECOVERY_DECISIONS = 8
NORMAL_PROCEDURE_HISTORY = 64


@dataclass(frozen=True)
class ReadOnlyReconciliationScope:
    id: str
    intent_id: str
    assignment_id: str
    execution_id: str
    worker_id: str
    fence: int
    requester_identity_id: str
    organization_id: str
    workspace_id: str
    project_id: str
    resource_ids: tuple[str, ...]
    agent_profile: object
    execution_profile_definition: object
    expires_at: float

    @property
    def marker(self):
        return RECOVERY_PREFIX + self.id + ':' + quote(self.intent_id, safe='')


_current = ContextVar('readonly_reconciliation_procedure_scope', default=None)


def current_reconciliation_scope():
    return _current.get()


@contextmanager
def read_only_reconciliation(scope):
    previous = _current.get()
    if previous is not None and previous != scope:
        raise ValueError('read-only reconciliation context changed')
    token = _current.set(scope)
    try:
        yield
    finally:
        _current.reset(token)
