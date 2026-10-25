"""Request-local WorkGraph read timings; never row data, cache or authority."""
import logging
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar

from codex_web.observability import operation_stage

_scope = ContextVar('work_graph_read_timing_scope', default=None)
_logger = logging.getLogger(__name__)


@contextmanager
def graph_read_timings(scope):
    token = _scope.set((scope.organization_id, scope.workspace_id))
    try:
        yield
    finally:
        _scope.reset(token)


def graph_read_stage(stage):
    scope = _scope.get()
    if scope is None:
        return nullcontext()
    return operation_stage(_logger, 'work_graph.read.stage', stage,
        organization_id=scope[0], workspace_id=scope[1])


@contextmanager
def graph_read_connection(factory):
    # Enter/exit the original context manager exactly once; no new lease owner.
    with ExitStack() as stack:
        with graph_read_stage('postgres.connection.acquire'):
            connection = stack.enter_context(factory())
        yield connection
