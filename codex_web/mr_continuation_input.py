"""Stable business identity for governed MR continuation inputs, not authority.

Provider reads, delivery identities and freshness remain in original Evidence.
This projection neither completes unknown policy nor grants merge permission.
"""
import hashlib
import json

from codex_web.code_host_merge_requirements import CodeHostMergeRequirementsFact

METHOD = 'code-host-mr-continuation-input.v1'
INPUT_METHOD = 'code-host-mr-procedure-input.v1'


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _ordered(values):
    return sorted(values, key=encoded)


def semantic_facts(fact: CodeHostMergeRequirementsFact):
    """Keep actual business/scope/UNKNOWN fields; drop observation identities.

    Provider execution IDs/URLs and observation times do not change the meaning
    of an otherwise identical check. Requirements and publisher identity do.
    No records are collapsed: conflicting/multiple observations remain visible.
    """
    value = fact.model_dump(mode='json')
    for key in ('observed_at', 'expires_at', 'web_url'):
        value.pop(key)
    for source in value['sources']:
        source.pop('observed_at')
        source['rule_ids'] = sorted(source['rule_ids'])
    value['sources'] = _ordered(value['sources'])
    for check in value['observed_checks']:
        for key in ('observed_at', 'web_url', 'external_id'):
            check.pop(key)
    value['observed_checks'] = _ordered(value['observed_checks'])
    for requirement in value['required_checks']:
        requirement['source_ids'] = sorted(requirement['source_ids'])
    value['required_checks'] = _ordered(value['required_checks'])
    for key in ('unsupported_rules', 'blockers'):
        value[key] = sorted(value[key])
    return value


def fact_fingerprint(fact: CodeHostMergeRequirementsFact):
    return hashlib.sha256(encoded({'protocol': INPUT_METHOD, 'facts': semantic_facts(fact)})).hexdigest()


def input_values(fact: CodeHostMergeRequirementsFact, *, expected_head):
    if fact.head_revision != expected_head or fact.changed:
        raise ValueError('mr_continuation_changed_head_or_observation')
    number = int(fact.external_id)
    if not 0 < number <= 2**31 - 1 or str(number) != fact.external_id:
        raise ValueError('mr_continuation_number_invalid')
    return {'pull_request_number': number, 'expected_head_sha': expected_head,
        'observed_requirements_fingerprint': fact_fingerprint(fact)}
