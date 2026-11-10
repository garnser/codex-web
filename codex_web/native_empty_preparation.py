"""Closed observational diagnostics; never admission, non-start or retry proof."""
STAGES = frozenset({
    'session_attestation', 'native_owner', 'physical_scope',
    'preparation_context', 'candidate_validation', 'candidate_consistency', 'discovery', 'fallback',
    'credential_custody',
})
EXCEPTION_TYPES = frozenset({
    'TimeoutError', 'ValueError', 'TypeError', 'LookupError', 'OSError',
    'RuntimeError', 'AssertionError', 'Exception', 'AttributeError', 'NameError',
    'KeyError', 'IndexError', 'PermissionError', 'FileNotFoundError', 'NotImplementedError',
})


CANONICAL_CODES = frozenset({
    'procedure_assignment_fence_expired_or_revoked',
    'procedure_resource_lease_expired_or_revoked',
    'procedure_worker_identity_revoked',
    'procedure_canonical_provider_hook_unavailable',
})


def closed_native_preparation_diagnostics(value):
    """At most one safe failure category per phase, in observation order."""
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    seen = set()
    # The internal producer is bounded; arbitrary transported metadata is not.
    for item in value[:len(STAGES)]:
        if (not isinstance(item, dict) or set(item) not in (
                {'stage', 'exception_type'}, {'stage', 'exception_type', 'code'})):
            continue
        stage, kind = item['stage'], item['exception_type']
        if (not isinstance(stage, str) or not isinstance(kind, str)
                or stage not in STAGES or kind not in EXCEPTION_TYPES or stage in seen):
            continue
        seen.add(stage)
        entry = {'stage': stage, 'exception_type': kind}
        if isinstance(item.get('code'), str) and item['code'] in CANONICAL_CODES:
            entry['code'] = item['code']
        result.append(entry)
    return result


# Compatibility for the already-published empty-scope field/producer (#2036).
closed_empty_preparation_diagnostics = closed_native_preparation_diagnostics
