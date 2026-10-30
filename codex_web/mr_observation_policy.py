"""Versioned observation validity from canonical Configuration, never authority."""
import hashlib
import json
import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator
from codex_web.configuration import ConfigurationScope, ConfigurationSpec, ConfigurationValueKind

OBSERVATION_WINDOW = ConfigurationSpec(
    key='code_host.mr_observation_window_seconds', value_kind=ConfigurationValueKind.INTEGER,
    default=30, minimum=1, maximum=300, allowed_scopes=[ConfigurationScope.DEPLOYMENT],
    category='Integrations', startup_only=False, hot_reloadable=True, grants_authority=False,
    description='Validity seconds for NEW version-2 MR observations. Resolved once per observation; '
        'does not extend provider I/O, leases, procedure deadlines, authority or retained observations.')
OBSERVATION_V2 = 'code-host-mr-observation.v2'
CONTINUATION_V2 = 'code-host-mr-continuation-input.v2'
INPUT_V2 = 'code-host-mr-procedure-input.v2'
READINESS_V2 = 'code-host-readiness-observation.v2'
V2_METHODS = (OBSERVATION_V2, CONTINUATION_V2, READINESS_V2)


class ObservationPolicyError(ValueError):
    code = 'mr_observation_policy_invalid'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def record_digest(record):
    # Publication supersedes a record's lifecycle/pointer, never its payload.
    return hashlib.sha256(canonical(record.model_dump(mode='json',
        exclude={'state', 'superseded_by_id'})).encode()).hexdigest()


class ObservationPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    version: Literal['2.0'] = '2.0'
    key: Literal['code_host.mr_observation_window_seconds'] = OBSERVATION_WINDOW.key
    seconds: StrictInt = Field(ge=1, le=300)
    source: Literal['default', 'published']
    resolved_at: float
    record_id: str | None = None
    revision: StrictInt | None = Field(default=None, ge=1)
    record_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def provenance(self):
        if not math.isfinite(self.resolved_at):
            raise ValueError('observation_policy_time_invalid')
        if self.source == 'default':
            if self.seconds != 30 or any(x is not None for x in (self.record_id, self.revision, self.record_sha256)):
                raise ValueError('observation_policy_default_invalid')
        elif not self.record_id or self.revision is None or self.record_sha256 is None:
            raise ValueError('observation_policy_publication_required')
        return self


def resolve_policy(configuration, *, now):
    if configuration is None:
        raise ObservationPolicyError('observation_configuration_unavailable')
    from codex_web.services.configuration import ConfigurationError, ConfigurationNotFoundError
    try:
        effective = configuration.resolve(OBSERVATION_WINDOW.key, now=now)
    except (ConfigurationError, ConfigurationNotFoundError) as exc:
        raise ObservationPolicyError('observation_configuration_invalid') from exc
    try:
        seconds = OBSERVATION_WINDOW.validate_value(effective.value)
    except (ValueError, TypeError) as exc:
        raise ObservationPolicyError('observation_configuration_value_invalid') from exc
    try:
        record = configuration.get_record(effective.record_id) if effective.source == 'published' else None
        policy = ObservationPolicy(seconds=seconds, source=effective.source, resolved_at=now,
            record_id=effective.record_id, revision=effective.revision,
            record_sha256=record_digest(record) if record else None)
    except (ConfigurationNotFoundError, ValidationError) as exc:
        raise ObservationPolicyError('observation_configuration_changed') from exc
    validate_policy(policy, configuration, now=now)
    return policy


def validate_policy(value, configuration, *, now):
    try:
        policy = ObservationPolicy.model_validate(value)
    except ValidationError as exc:
        raise ObservationPolicyError('observation_policy_invalid') from exc
    if configuration is None or not math.isfinite(now) or now < policy.resolved_at:
        raise ObservationPolicyError('observation_policy_current_validation_unavailable')
    records = configuration.store.load()
    relevant = [r for r in records if r.key == policy.key and r.scope_type == ConfigurationScope.DEPLOYMENT
                and r.scope_id is None]
    # Disabling is revocation, even if a later publication enables new reads.
    if any(r.state == 'disabled' and r.created_at >= policy.resolved_at for r in relevant):
        raise ObservationPolicyError('observation_policy_revoked')
    history = [r for r in relevant if r.published_at is not None and r.published_at <= policy.resolved_at]
    at_resolution = max(history, key=lambda r: r.revision, default=None)
    if policy.source == 'default' and at_resolution is not None and at_resolution.state != 'disabled':
        raise ObservationPolicyError('observation_policy_default_not_effective')
    if policy.source == 'published':
        if at_resolution is None or at_resolution.id != policy.record_id:
            raise ObservationPolicyError('observation_policy_not_effective_at_resolution')
        selected = [r for r in relevant if r.id == policy.record_id]
        if len(selected) != 1:
            raise ObservationPolicyError('observation_policy_record_unavailable')
        record = selected[0]
        if (record.state not in {'published', 'superseded'} or record.revision != policy.revision
                or type(record.value) is not int or record.value != policy.seconds
                or record.published_at is None or record.published_at > policy.resolved_at
                or record_digest(record) != policy.record_sha256):
            raise ObservationPolicyError('observation_policy_record_changed')
    return policy


def expiry_valid(policy, observed_at, expires_at):
    return (all(type(x) in (int, float) and math.isfinite(x) for x in (observed_at, expires_at))
            and expires_at == observed_at + policy.seconds)
