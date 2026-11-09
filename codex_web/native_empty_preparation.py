"""Closed observational diagnostics; never admission, non-start or retry proof."""
STAGES = frozenset({
    'session_attestation', 'native_owner', 'physical_scope',
    'preparation_context', 'candidate_validation', 'candidate_consistency', 'discovery', 'fallback',
})
EXCEPTION_TYPES = frozenset({
    'TimeoutError', 'ValueError', 'TypeError', 'LookupError', 'OSError',
    'RuntimeError', 'AssertionError', 'Exception', 'AttributeError', 'NameError',
    'KeyError', 'IndexError', 'PermissionError', 'FileNotFoundError', 'NotImplementedError',
})


def closed_empty_preparation_diagnostics(value):
    """At most one safe failure category per phase, in observation order."""
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    seen = set()
    # The internal producer is bounded; arbitrary transported metadata is not.
    for item in value[:len(STAGES)]:
        if not isinstance(item, dict) or set(item) != {'stage', 'exception_type'}:
            continue
        stage, kind = item['stage'], item['exception_type']
        if (not isinstance(stage, str) or not isinstance(kind, str)
                or stage not in STAGES or kind not in EXCEPTION_TYPES or stage in seen):
            continue
        seen.add(stage)
        result.append({'stage': stage, 'exception_type': kind})
    return result
