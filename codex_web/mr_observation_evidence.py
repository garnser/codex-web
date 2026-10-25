"""Freshness of the fixed MR observation protocol, separate from data retention."""
import math

METHOD = "code-host-mr-observation.v1"
READINESS_METHOD = "code-host-readiness-observation.v1"


def current_observation(evidence, *, now):
    if evidence.metadata.get("verification_method") not in (METHOD, READINESS_METHOD):
        return True
    expiry = evidence.metadata.get("observation_expires_at")
    observed = evidence.observed_at
    return (all(type(x) in (int, float) and math.isfinite(x) for x in (expiry, observed, now))
            and observed <= now < expiry and 0 < expiry - observed <= 30)
