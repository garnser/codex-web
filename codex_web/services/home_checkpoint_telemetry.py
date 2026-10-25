"""Request-local timings for dormant Home preservation; never authority or state."""
import logging
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps

from codex_web.observability import operation_stage

_scope = ContextVar('home_checkpoint_timing_scope', default=None)
_logger = logging.getLogger(__name__)


@contextmanager
def checkpoint_timings(actor):
    token = _scope.set((actor.organization_id, actor.workspace_id))
    try:
        yield
    finally:
        _scope.reset(token)


def checkpoint_stage(stage):
    scope = _scope.get()
    if scope is None:
        return nullcontext()
    return operation_stage(_logger, 'home.checkpoint.stage', stage,
        organization_id=scope[0], workspace_id=scope[1])


def checkpoint_component(stage):
    def decorate(function):
        @wraps(function)
        def timed(*args, **kwargs):
            with checkpoint_stage(stage):
                return function(*args, **kwargs)
        return timed
    return decorate
