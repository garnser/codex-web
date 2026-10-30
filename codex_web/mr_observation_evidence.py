"""Freshness of the fixed MR observation protocol, separate from data retention."""
import math
import json
from codex_web.mr_observation_policy import V2_METHODS, validate_policy, expiry_valid
from codex_web.mr_continuation_input import METHOD as CONTINUATION_METHOD

METHOD = "code-host-mr-observation.v1"
READINESS_METHOD = "code-host-readiness-observation.v1"


def current_observation(evidence, *, now, configuration=None):
    method = evidence.metadata.get("verification_method")
    if method in V2_METHODS:
        try:
            policy = validate_policy(json.loads(evidence.metadata['observation_policy']), configuration, now=now)
            started = evidence.metadata['observation_started_at']
            return (expiry_valid(policy, started, evidence.metadata.get('observation_expires_at'))
                    and policy.resolved_at <= started <= evidence.observed_at <= now
                    and now < evidence.metadata['observation_expires_at'])
        except (ValueError, TypeError, KeyError):
            return False
    if isinstance(method, str) and method.startswith(('code-host-mr-', 'code-host-readiness-observation.')) \
            and method not in (METHOD, READINESS_METHOD, CONTINUATION_METHOD):
        return False
    if method not in (METHOD, READINESS_METHOD, CONTINUATION_METHOD):
        return True
    expiry = evidence.metadata.get("observation_expires_at")
    observed = evidence.observed_at
    return (all(type(x) in (int, float) and math.isfinite(x) for x in (expiry, observed, now))
            and observed <= now < expiry and 0 < expiry - observed <= 30)
