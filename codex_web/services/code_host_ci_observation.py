"""Read-only CI observations and current canonical prerequisite receipts.

This module never provisions a host, approves an action, invokes a diagnostic,
retries CI, or creates a second execution/evidence owner.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from codex_web.identity import TenantScope
from codex_web.definitions import DefinitionReference

CI_OBSERVATION_ACTION_ID = 'code-host.pull-request.observe-ci'
CI_OBSERVATION_EVIDENCE = 'code-host-ci-observation'
CI_OBSERVATION_VERSION = '1.0'
PREREQUISITE_RECEIPT_METHOD = 'ci-dependency-readiness.v1'


class CIObservationParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    pull_request_number: StrictInt = Field(ge=1, le=2147483647)
    expected_head_sha: str = Field(pattern=r'^[0-9a-f]{40}$')
    environment: Literal['development']
    change_classification: Literal['cosmetic-only', 'localized functional', 'shared-surface', 'release/security-sensitive']
    work_item_ref: str = Field(min_length=1, max_length=400)
    target_resource_id: str = Field(min_length=1, max_length=200)
    dependency_verification_id: str | None = Field(default=None, pattern=r'^verification-[0-9a-f]{32}$')
    runtime_verification_id: str | None = Field(default=None, pattern=r'^verification-[0-9a-f]{32}$')
    target_verification_id: str | None = Field(default=None, pattern=r'^verification-[0-9a-f]{32}$')


class PrerequisiteResult(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    disposition: Literal['verified', 'needs_input', 'blocked']
    verification_ids: tuple[str, ...] = Field(default=(), max_length=3)
    reasons: tuple[str, ...] = Field(default=(), max_length=12)
    # This is an accountable existing principal, not a Work handoff or a grant.
    next_owner_identity_id: str = Field(min_length=1, max_length=200)
    authorizes_mutation: Literal[False] = False
    execution_target_kind: Literal['canonical-native-assignment', 'external-ci-job'] | None = None
    external_ci_runtime_attested: Literal[False] = False
    execution_target_requirement_ref: DefinitionReference | None = None

    @model_validator(mode='after')
    def verified_receipts(self):
        if self.disposition == 'verified' and (self.execution_target_kind != 'canonical-native-assignment'
                or self.execution_target_requirement_ref is None
                or self.execution_target_requirement_ref.kind != 'execution-role-catalog'
                or self.execution_target_requirement_ref.definition_schema_version != '1.1'
                or len(set(self.verification_ids)) != 3
                or self.reasons or any(not re.fullmatch(r'verification-[0-9a-f]{32}', value)
                                       for value in self.verification_ids)):
            raise ValueError('verified readiness requires three distinct original verification identities')
        return self


def parameters(request):
    return CIObservationParameters.model_validate(request.parameters)


def unavailable_prerequisites(request):
    return PrerequisiteResult(disposition='needs_input',
        reasons=('canonical_prerequisite_validator_unavailable',),
        next_owner_identity_id=request.requested_by or 'unresolved-requester')


def parse_ci_manifest(content, *, kind, request, p, now):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('CI receipt contains duplicate keys')
            result[key] = value
        return result
    body = json.loads(content, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite receipt')))

    required = {'schema_version', 'kind', 'repository_resource_id', 'head_sha', 'target_resource_id',
                'environment', 'work_item_ref', 'expires_at', 'facts'}
    if (not isinstance(body, dict) or set(body) != required
            or body['schema_version'] != '1.0' or body['kind'] != kind
            or body['repository_resource_id'] != request.resource_ids[0]
            or body['head_sha'] != p.expected_head_sha or body['target_resource_id'] != p.target_resource_id
            or body['environment'] != 'development' or body['work_item_ref'] != p.work_item_ref
            or type(body['expires_at']) not in (int, float) or not math.isfinite(body['expires_at'])
            or body['expires_at'] <= now
            or not isinstance(body['facts'], dict)):
        raise ValueError('CI prerequisite exact source, target or validity is unproven')
    facts = body['facts']
    # These are independent verified facts, never an installation grant.
    fields = {'dependency': ('manifest_sha256', 'private_core_provenance_sha256', 'python_abi', 'platform'),
              'runtime': ('assignment_id', 'execution_id', 'execution_workspace_id', 'worker_id', 'assignment_fence',
                          'profile_reference_sha256', 'runtime_contract', 'python_abi', 'platform', 'execution_target_kind'),
              'target': ('target_identity_sha256', 'ci_service_identity_id', 'purpose', 'coordination_receipt_sha256')}
    if set(facts) != set(fields[kind]) or any(not isinstance(facts[k], str) or not facts[k] for k in fields[kind]):
        raise ValueError('CI prerequisite required observed facts are incomplete')
    for key, value in facts.items():
        if key.endswith('_sha256') and not re.fullmatch('[0-9a-f]{64}', value):
            raise ValueError('CI prerequisite provenance digest is invalid')
    if kind == 'runtime' and facts['execution_target_kind'] not in ('canonical-native-assignment', 'external-ci-job'):
        raise ValueError('CI actual execution target kind is unsupported')
    if kind == 'target' and facts['purpose'] not in ('development-validation', 'isolated-ci-fixture'):
        raise ValueError('CI actual target purpose is not admitted development validation')
    return body


class CanonicalCIPrerequisiteValidator:
    """Read exact independent canonical Verification/Artifact receipts.

    The receipt's typed body is informational. Even a verified readiness receipt
    does not authorize helper installation or replace a current ApprovalRequest.
    Existing canonical services own its admission, visibility and verification.
    """
    def __init__(self, evidence, identity, resources, *, workers=None, execution_target_requirement=None, clock=time.time):
        self.evidence, self.identity, self.resources = evidence, identity, resources
        self.workers = workers
        self.execution_target_requirement = execution_target_requirement
        self.clock = clock

    def __call__(self, request, binding):
        p = parameters(request)
        scope = TenantScope(organization_id=request.organization_id, workspace_id=request.workspace_id)
        actor = self.identity.actor_for_identity(request.requested_by, scope=scope)
        target = self.resources.get(p.target_resource_id, actor)
        if (target.lifecycle != 'active' or target.operational_context is None
                or target.operational_context.environment != 'development'):
            return PrerequisiteResult(disposition='blocked', reasons=('actual_target_not_development',),
                next_owner_identity_id=actor.identity_id)
        ids = (p.dependency_verification_id, p.runtime_verification_id, p.target_verification_id)
        if any(value is None for value in ids):
            return PrerequisiteResult(disposition='needs_input', reasons=('required_prerequisite_receipts_missing',),
                next_owner_identity_id=actor.identity_id)
        if len(set(ids)) != 3:
            raise ValueError('CI prerequisite categories require distinct original verifications')
        receipts = {kind: self._receipt(identifier, kind, request, p, actor)
                    for kind, identifier in zip(('dependency', 'runtime', 'target'), ids)}
        dependency, runtime = receipts['dependency']['facts'], receipts['runtime']['facts']
        target_kind = runtime['execution_target_kind']
        if target_kind == 'external-ci-job':
            return PrerequisiteResult(disposition='needs_input', reasons=('external_ci_runtime_attestation_unavailable',),
                next_owner_identity_id=actor.identity_id, execution_target_kind=target_kind)
        selected = (self.execution_target_requirement(request, binding)
                    if self.execution_target_requirement is not None else None)
        if selected is None:
            policy, requirement_ref = None, None
        elif (type(selected) is tuple and len(selected) == 2
                and isinstance(selected[1], DefinitionReference)):
            policy, requirement_ref = selected
        else:
            raise ValueError('CI execution target requirement must come from the canonical Role owner')
        requirement = getattr(policy, 'execution_target_kind', None)
        if policy is not None and (getattr(policy, 'schema_version', None) != '1.0'
                or requirement_ref.kind != 'execution-role-catalog'
                or requirement_ref.definition_schema_version != '1.1'
                or requirement not in ('canonical-native-assignment', 'external-ci-job')):
            raise ValueError('CI published execution target policy/ref is incompatible')
        if requirement != 'canonical-native-assignment':
            return PrerequisiteResult(disposition='needs_input',
                reasons=('declared_execution_target_requirement_unavailable' if policy is None
                         else 'declared_execution_target_kind_mismatch',),
                next_owner_identity_id=actor.identity_id, execution_target_kind=target_kind,
                execution_target_requirement_ref=requirement_ref)
        if any(dependency[key] != runtime[key] for key in ('python_abi', 'platform')):
            raise ValueError('CI dependency and actual runtime ABI/platform differ')
        if self.workers is None:
            return PrerequisiteResult(disposition='needs_input', reasons=('canonical_runtime_validator_unavailable',),
                next_owner_identity_id=actor.identity_id)
        assignment, worker = self.validate_runtime(runtime, request, actor)
        if receipts['target']['facts']['ci_service_identity_id'] != worker.service_identity_id:
            raise ValueError('CI native target service identity does not match the canonical runtime worker')
        if self.resources.get(p.target_resource_id, actor) != target:
            raise ValueError('CI actual target changed during prerequisite observation')
        if (self.workers.store.assignment(assignment.id) != assignment
                or self.workers.store.worker(worker.id) != worker):
            raise ValueError('CI runtime custody changed during observation')
        self.identity.actor_for_identity(actor.identity_id, scope=scope)
        # No cache: later CONNECT/input/procedure checks retain their own owners.
        return PrerequisiteResult(disposition='verified', verification_ids=ids,
            next_owner_identity_id=actor.identity_id, execution_target_kind=target_kind,
            execution_target_requirement_ref=requirement_ref)

    def validate_runtime(self, runtime, request, actor):
        scope = actor.tenant
        assignment = self.workers.store.assignment(runtime['assignment_id'])
        if (assignment is None or
                (assignment.organization_id, assignment.workspace_id, assignment.project_id) !=
                (request.organization_id, request.workspace_id, request.project_id) or
                assignment.execution_id != runtime['execution_id'] or
                assignment.execution_workspace_id != runtime['execution_workspace_id'] or
                assignment.assigned_worker_id != runtime['worker_id'] or
                assignment.execution_contract_version != runtime['runtime_contract'] or
                not set(request.resource_ids).issubset(assignment.resource_ids) or
                assignment.status not in ('claimed', 'running') or assignment.lease is None or
                (assignment.deadline_at is not None and assignment.deadline_at <= self.clock()) or
                assignment.lease.expires_at <= self.clock() or assignment.lease.fence != assignment.fence or
                assignment.lease.worker_id != assignment.assigned_worker_id or
                str(assignment.fence) != runtime['assignment_fence'] or
                assignment.execution_profile_definition is None or
                hashlib.sha256(json.dumps(assignment.execution_profile_definition.model_dump(mode='json'),
                    sort_keys=True, separators=(',', ':')).encode()).hexdigest() != runtime['profile_reference_sha256']):
            raise ValueError('CI runtime receipt does not identify its canonical assignment')
        worker = self.workers.store.worker(runtime['worker_id'])
        if (worker is None or worker.lifecycle not in ('active', 'draining') or worker.revoked_at is not None
                or (worker.organization_id, worker.workspace_id) != (request.organization_id, request.workspace_id)
                or assignment.execution_contract_version not in worker.supported_execution_contract_versions
                or assignment.sandbox not in worker.supported_sandbox_profiles
                or not set(assignment.required_capabilities).issubset(worker.capabilities)):
            raise ValueError('CI runtime canonical worker is unavailable or incompatible')
        worker_actor = self.identity.actor_for_identity(worker.service_identity_id, scope=scope)
        if worker_actor.principal_kind != 'service':
            raise ValueError('CI runtime worker must retain its own current service identity')
        return assignment, worker

    def _receipt(self, identifier, kind, request, p, actor):
        proof = self.evidence.get_verification(identifier, actor=actor)
        if (proof.result != 'verified' or not proof.independent
                or proof.method != PREREQUISITE_RECEIPT_METHOD
                or proof.work_item_ref != p.work_item_ref
                or len(proof.artifact_ids) != 1 or len(proof.evidence_ids) != 1
                or proof.verified_at > self.clock()):
            raise ValueError('CI prerequisite original independent verification is unavailable')
        self.identity.actor_for_identity(proof.verifier_identity_id, scope=actor.tenant)
        evidence = self.evidence.get_evidence(proof.evidence_ids[0], actor=actor)
        artifact = self.evidence.get_artifact(proof.artifact_ids[0], actor=actor)
        expected = (request.organization_id, request.workspace_id, request.project_id, p.work_item_ref)
        if ((evidence.organization_id, evidence.workspace_id, evidence.project_id, evidence.work_item_ref) != expected
                or (artifact.organization_id, artifact.workspace_id, artifact.project_id, artifact.work_item_ref) != expected
                or tuple(evidence.artifact_ids) != (artifact.id,)
                or evidence.result != 'pass' or evidence.lifecycle != 'valid'
                or artifact.lifecycle != 'active' or artifact.superseded_by_id is not None
                or artifact.revision != p.expected_head_sha
                or any(value is not None and value <= self.clock() for value in
                       (artifact.retention_expires_at, evidence.retention_expires_at))
                or proof.verifier_identity_id in (artifact.producer_identity_id, evidence.producer_identity_id)
                or not set(request.resource_ids).issubset(artifact.resource_ids)
                or not set(request.resource_ids).issubset(evidence.resource_ids)):
            raise ValueError('CI prerequisite receipt scope, independence or lifecycle changed')
        pointer, stream = self.evidence.open_artifact_content(artifact.id, actor=actor)
        content = bytearray()
        try:
            for chunk in stream:
                content.extend(chunk)
                if len(content) > 16384:
                    raise ValueError('CI prerequisite receipt exceeds bounded content contract')
        finally:
            close = getattr(stream, 'close', None)
            if close:
                close()
        digest = hashlib.sha256(content).hexdigest()
        if (artifact.digest is None or artifact.digest.algorithm != 'sha256'
                or digest != artifact.digest.value
                or evidence.digest is None or evidence.digest.algorithm != 'sha256' or evidence.digest.value != digest
                or tuple(value for value in proof.findings if value.startswith('ci_readiness_manifest_sha256:'))
                    != ('ci_readiness_manifest_sha256:' + digest,)
                or len(content) != pointer.size_bytes):
            raise ValueError('CI prerequisite content digest changed')
        body = parse_ci_manifest(content, kind=kind, request=request, p=p, now=self.clock())
        facts = body['facts']
        if kind == 'runtime' and (proof.execution_id != facts['execution_id']
                or evidence.execution_id != facts['execution_id'] or artifact.execution_id != facts['execution_id']
                or evidence.execution_workspace_id != facts['execution_workspace_id']
                or artifact.execution_workspace_id != facts['execution_workspace_id']):
            raise ValueError('CI runtime proof does not retain canonical execution attribution')
        # Recheck the original records after content I/O; no partial inventory.
        if (self.evidence.get_verification(identifier, actor=actor) != proof
                or self.evidence.get_evidence(evidence.id, actor=actor) != evidence
                or self.evidence.get_artifact(artifact.id, actor=actor) != artifact):
            raise ValueError('CI prerequisite original receipt changed during observation')
        return body


def project_observation(raw, *, request, binding_id, provider_instance):
    p = parameters(request)
    if not isinstance(raw, dict):
        raise ValueError('CI original observation is malformed')
    if (raw.get('observation_version') != CI_OBSERVATION_VERSION
            or raw.get('provider_instance') != provider_instance
            or raw.get('binding_id') != binding_id
            or raw.get('resource_id') != request.resource_ids[0]
            or raw.get('requested_by') != request.requested_by
            or raw.get('project_id') != request.project_id
            or raw.get('head_sha') != p.expected_head_sha
            or raw.get('pull_request_number') != p.pull_request_number
            or raw.get('environment') != p.environment
            or raw.get('change_classification') != p.change_classification):
        raise ValueError('CI observation original provider/repository/head/inputs mismatch')
    facts = raw.get('runs')
    if not isinstance(facts, list) or not 0 < len(facts) <= 20:
        raise ValueError('CI observation complete pipeline inventory unavailable')
    states, ids = [], set()
    selected = raw.get('current_pipeline_id')
    if not isinstance(selected, str) or not re.fullmatch('[1-9][0-9]*', selected):
        raise ValueError('CI current MR pipeline is unavailable')
    for run in facts:
        if not isinstance(run, dict) or run.get('revision') != p.expected_head_sha:
            raise ValueError('CI observation pipeline revision mismatch')
        identifier = run.get('external_id')
        if not isinstance(identifier, str) or not re.fullmatch('[1-9][0-9]*', identifier) or identifier in ids:
            raise ValueError('CI observation pipeline identity is ambiguous')
        ids.add(identifier)
        checks = run.get('checks')
        if not isinstance(checks, list) or not 0 < len(checks) <= 1000:
            raise ValueError('CI observation jobs are incomplete')
        names = set()
        for check in checks:
            if (not isinstance(check, dict) or check.get('revision') != p.expected_head_sha
                    or not isinstance(check.get('external_id'), str) or not re.fullmatch('[1-9][0-9]*', check['external_id'])
                    or check['external_id'] in names or not isinstance(check.get('name'), str)
                    or check.get('state') not in ('created', 'waiting_for_resource', 'preparing', 'pending',
                        'running', 'success', 'failed', 'canceled', 'skipped', 'manual', 'scheduled')):
                raise ValueError('CI observation job identity/revision is invalid')
            names.add(check['external_id'])
        if run.get('workflow_path') != '.gitlab-ci.yml' or run.get('tested_revision') != p.expected_head_sha:
            raise ValueError('CI pipeline provenance is incomplete')
        state = run.get('state')
        if state not in ('created', 'waiting_for_resource', 'preparing', 'pending', 'running',
                         'success', 'failed', 'canceled', 'skipped', 'manual', 'scheduled'):
            raise ValueError('CI pipeline state is malformed')
        if identifier != selected:
            continue
        if state == 'success' and any(c.get('state') not in ('success', 'skipped') for c in checks):
            states.append('unknown')
        elif state in ('failed', 'canceled'):
            states.append('failed')
        elif state == 'success':
            states.append('passed')
        else:
            states.append('pending')
    if selected not in ids or len(states) != 1:
        raise ValueError('CI current pipeline is absent or ambiguous')
    prerequisite = PrerequisiteResult.model_validate_json(json.dumps(raw.get('prerequisites'), allow_nan=False))
    ci = 'failed' if 'failed' in states else 'pending' if 'pending' in states else 'unknown' if 'unknown' in states else 'passed'
    ready = ci == 'passed' and prerequisite.disposition == 'verified'
    return {'head_sha': p.expected_head_sha, 'pull_request_number': p.pull_request_number,
            'current_pipeline_id': selected, 'ci_disposition': ci, 'readiness': 'ready_for_validation' if ready else 'blocked' if ci == 'failed' or prerequisite.disposition == 'blocked' else 'needs_input',
            'next_owner_identity_id': prerequisite.next_owner_identity_id,
            'prerequisite_reasons_json': json.dumps(prerequisite.reasons),
            'pipeline_ids_json': json.dumps(sorted(ids)),
            'privileged_operation_authorized': False,
            'execution_target_kind': prerequisite.execution_target_kind,
            'external_ci_runtime_attested': False,
            'execution_target_requirement_ref_json': json.dumps(prerequisite.execution_target_requirement_ref.model_dump(mode='json')
                if prerequisite.execution_target_requirement_ref else None, sort_keys=True, separators=(',', ':'))}
