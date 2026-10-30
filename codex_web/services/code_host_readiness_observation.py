"""Fixed readiness-observation result protocol; no approval or mutation authority."""
import json
import hashlib
import re

from codex_web.code_host_merge_requirements import CodeHostMergeRequirementsFact
from codex_web.code_host_mr_observation import dispositions

VERSION = '1.0'
VERSION_V2 = '2.0'


def borrowed_credential_metadata():
    from codex_web.services.secrets import current_async_use_reference
    reference = current_async_use_reference()
    if reference is None or reference.status().value != 'active':
        raise ValueError('readiness_observation_active_credential_borrow_required')
    return {'credential_metadata_sha256': hashlib.sha256(reference.model_dump_json().encode()).hexdigest(),
        'credential_rotation': reference.rotation}


def observation_outputs(raw, *, binding_id, resource_id, provider_instance, parameters, observed_at):
    if raw.get('observation_version') not in (VERSION, VERSION_V2):
        raise ValueError('readiness_observation_version_invalid')
    if (re.fullmatch('[0-9a-f]{64}', str(raw.get('credential_metadata_sha256', ''))) is None
            or type(raw.get('credential_rotation')) is not int or raw['credential_rotation'] < 0):
        raise ValueError('readiness_observation_credential_metadata_required')
    fact = CodeHostMergeRequirementsFact.model_validate(raw.get('merge_requirements'))
    if raw.get('observation_version') == VERSION_V2:
        from codex_web.mr_observation_policy import ObservationPolicy, expiry_valid
        policy = ObservationPolicy.model_validate(raw.get('observation_policy'))
        if not expiry_valid(policy, fact.observed_at, fact.expires_at) or fact.observed_at < policy.resolved_at:
            raise ValueError('readiness_observation_policy_mismatch')
    elif not 0 < fact.expires_at - fact.observed_at <= 30:
        raise ValueError('readiness_observation_legacy_window_invalid')
    if (len(fact.model_dump_json().encode()) > 128*1024
            or fact.binding_id != binding_id or fact.resource_id != resource_id
            or fact.provider_type != 'gitlab' or fact.provider_instance != provider_instance
            or fact.external_id != str(parameters['pull_request_number'])
            or raw.get('pull_request_number') != parameters['pull_request_number']
            or raw.get('expected_head_sha') != parameters['expected_head_sha']):
        raise ValueError('readiness_observation_scope_invalid')
    projected = dispositions(fact, expected_head=parameters['expected_head_sha'], now=observed_at)
    statuses = {key: value[1] for key,value in projected.items()}
    # Completeness and the three dispositions cannot erase actual provider gates.
    satisfied = (all(value == 'pass' for value in statuses.values()) and fact.complete
        and not fact.blockers and fact.pull_request_state == 'OPENED' and fact.draft is False
        and fact.mergeable == 'MERGEABLE' and fact.merge_state in {'mergeable', 'clean'})
    blocked = (any(value == 'fail' for value in statuses.values()) or bool(fact.blockers)
        or fact.pull_request_state != 'OPENED' or fact.draft is not False
        or fact.mergeable == 'CONFLICTING')
    return {'pull_request_number': parameters['pull_request_number'], 'head_sha': fact.head_revision,
        'observation_succeeded': True,
        'readiness': 'requirements_observed_satisfied' if satisfied else 'blocked' if blocked else 'pending',
        **statuses, 'blockers_json': json.dumps(fact.blockers, separators=(',', ':')),
        'pull_request_state': fact.pull_request_state, 'draft': fact.draft,
        'mergeable': fact.mergeable or 'UNKNOWN', 'merge_state': fact.merge_state or 'UNKNOWN',
        'observed_at': fact.observed_at, 'expires_at': fact.expires_at}
