"""Request-local guarded verification timings; never authority or cached state."""
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar

from codex_web.observability import (
    CorrelationContext,
    bind_correlation,
    operation_stage,
    reset_correlation,
)

_scope = ContextVar('guarded_verification_timing_scope', default=None)
_logger = logging.getLogger(__name__)
_STAGES = frozenset({'verification', 'provider_verification', 'admission',
                     'inputs', 'custody', 'http'})


@contextmanager
def guarded_verification_timings(intent):
    # Import lazily: the reader budget also uses this otherwise low-level helper.
    from codex_web.services.code_host_action_contract import (
        CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_GUARDED_ACTION_ID,
    )
    enabled = intent.action_id == CODE_HOST_PULL_REQUEST_OBSERVE_READINESS_GUARDED_ACTION_ID
    scope_token = _scope.set((intent.organization_id, intent.workspace_id) if enabled else None)
    correlation_token = None
    try:
        if enabled:
            # Bind exact canonical attribution, including an absent Work ref.
            # correlated() inherits absent fields from its parent; that would
            # mislabel a Work-less bootstrap inside another Work's request.
            correlation_token = bind_correlation(CorrelationContext(
                correlation_id=intent.correlation_id,
                tenant_id=intent.organization_id,
                workspace_id=intent.workspace_id,
                work_item_ref=intent.work_item_ref,
                execution_id=intent.execution_id,
                action_intent_id=intent.id,
            ))
        yield
    finally:
        if correlation_token is not None:
            reset_correlation(correlation_token)
        _scope.reset(scope_token)


@contextmanager
def verification_stage(stage):
    scope = _scope.get()
    if scope is None or stage not in _STAGES:
        yield
        return
    try:
        manager = operation_stage(_logger, 'guarded_readonly.verify.stage', stage,
                                  organization_id=scope[0], workspace_id=scope[1])
        manager.__enter__()
    except BaseException:
        # A broken diagnostic clock/logger must not prevent the actual body.
        yield
        return
    try:
        yield
    except BaseException:
        original = sys.exc_info()
        try:
            manager.__exit__(*original)
        except BaseException:
            pass
        raise
    else:
        try:
            manager.__exit__(None, None, None)
        except BaseException:
            pass
