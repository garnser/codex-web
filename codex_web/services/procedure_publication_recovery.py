"""Exact original-publication operations for the canonical procedure interpreter.

No provider wrapper, new publication intent, actor construction or Git mutation.
The engine supplies authenticated actors and owns checkpoint/evidence writes.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import time

from pydantic import BaseModel, ConfigDict, Field

from codex_web.action_intents import ActionIntentClaimRequest, ActionIntentReconcileRequest
from codex_web.procedure_control_plane import (
    CanonicalOperationContract, CanonicalOperationError, CanonicalOperationResult,
)
from codex_web.services.code_host_action_contract import CODE_HOST_BRANCH_PUBLISH_ACTION_ID
from codex_web.services.gitlab_action_provider import GitLabActionProvider
from codex_web.skill_procedures import ProcedureValueContract

INSPECT = 'workspace.publication.inspect.v1'
RECOVER = 'publication.intent.recover.v1'
VERIFY = 'publication.intent.verify.v1'
_active_input = ContextVar('publication_recovery_input', default=None)


class PublicationRecoveryInput(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True, str_strip_whitespace=True)
    work_item_ref: str = Field(min_length=1, max_length=500)
    execution_workspace_id: str = Field(min_length=1, max_length=200)
    expected_assignment_id: str = Field(min_length=1, max_length=200)
    expected_assignment_fence: int = Field(ge=1, strict=True)
    original_intent_id: str = Field(min_length=1, max_length=200)
    expected_original_request_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_original_receipt_id: str | None = Field(default=None, min_length=1, max_length=200)
    expected_head_revision: str = Field(pattern=r'^[a-f0-9]{40,64}$')
    expected_canonical_branch: str = Field(min_length=1, max_length=240)


def request_checksum(request):
    """Exact immutable request; includes its existing key and credential reference."""
    return hashlib.sha256(json.dumps(request.model_dump(mode='json'),
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def output_contracts():
    return tuple(ProcedureValueContract(name=name, type=kind, description=description)
        for name, kind, description in (
            ('observed_original_intent_id', 'string', 'Unchanged original ActionIntent identity; not rebound as a later input'),
            ('original_outcome', 'string', 'Canonical original status, including unresolved outcomes'),
            ('verified', 'boolean', 'Positive exact original receipt and verification, never absence inference')))


def register_publication_operations(registry, owner):
    for operation, effect in ((INSPECT, 'read'), (RECOVER, 'mutation'), (VERIFY, 'read')):
        registry.register(CanonicalOperationContract(operation_id=operation,
            contract_version='1.0', resource_types=('repository',),
            required_capability='repository.branch.publish' if effect == 'mutation' else 'repository.read.metadata',
            authority_level='execute' if effect == 'mutation' else 'read',
            input_model=PublicationRecoveryInput, output_contracts=output_contracts(),
            effect=effect, timeout_seconds=600, max_attempts=1,
            allow_original_intent=effect == 'mutation'), owner)


class PublicationRecoveryOperations:
    def __init__(self, *, workers, workspaces, intents, work_items, clock=time.time):
        self.workers, self.workspaces, self.intents = workers, workspaces, intents
        self.work_items, self.clock = work_items, clock

    def _deny(self, code):
        raise CanonicalOperationError(code)

    def _identity(self, context, values):
        context.validate_current()
        actor, expected = context.requester_actor, context.assignment
        current = self.workers.store.assignment(expected.id)
        if (current is None or current.id != values.expected_assignment_id
                or current.fence != values.expected_assignment_fence
                or current.id != expected.id or current.fence != expected.fence
                or current.execution_id != expected.execution_id
                or current.execution_workspace_id != values.execution_workspace_id
                or current.status not in {'running', 'claimed'} or current.lease is None
                or current.lease.expires_at <= self.clock()
                or current.lease.fence != current.fence
                or current.lease.worker_id != current.assigned_worker_id
                or (current.deadline_at is not None and current.deadline_at <= self.clock())
                or current.created_by != actor.identity_id
                or (current.organization_id, current.workspace_id) != (actor.organization_id, actor.workspace_id)
                or len(context.resource_ids) != 1
                or not set(context.resource_ids).issubset(current.resource_ids)):
            self._deny('publication_recovery_current_custody_changed')
        worker = self.workers.store.worker(current.assigned_worker_id)
        if (worker is None or worker.lifecycle not in {'active', 'draining'}
                or context.worker_actor is None
                or context.worker_actor.principal_kind != 'service'
                or worker.service_identity_id != context.worker_actor.identity_id
                or (worker.organization_id, worker.workspace_id) != (current.organization_id, current.workspace_id)):
            self._deny('publication_recovery_worker_identity_changed')
        self.intents._require_worker(context.worker_actor)
        try:
            state = self.work_items.state_machine._work_item_state(values.work_item_ref)
        except (KeyError, ValueError):
            self._deny('publication_recovery_work_unavailable')
        profile = current.agent_profile
        if (state.ref != values.work_item_ref or state.closed_at is not None or state.current_stage == 'closed'
                or (state.organization_id, state.workspace_id, state.project_id) !=
                    (current.organization_id, current.workspace_id, current.project_id)
                or tuple(state.resource_ids) != tuple(context.resource_ids) or profile is None
                or state.current_owner not in {profile.profile_id, profile.role_id}):
            self._deny('publication_recovery_current_work_scope_changed')
        intent = self.intents.get(values.original_intent_id, actor)
        if (intent.action_id != CODE_HOST_BRANCH_PUBLISH_ACTION_ID
                or intent.provider_type != 'gitlab' or intent.requested_by != actor.identity_id
                or (intent.organization_id, intent.workspace_id, intent.project_id) !=
                    (current.organization_id, current.workspace_id, current.project_id)
                or intent.work_item_ref != state.ref
                or tuple(intent.resource_ids) != tuple(context.resource_ids)
                or intent.request.idempotency_key != intent.idempotency_key
                or request_checksum(intent.request) != values.expected_original_request_sha256
                or intent.request.parameters.get('head_revision') != values.expected_head_revision):
            self._deny('publication_recovery_original_identity_changed')
        binding = self.intents.execution.registry.binding(intent.binding_id, actor=actor)
        provider = self.intents.execution.registry.provider(intent.provider_type, intent.provider_instance, actor=actor)
        if (not binding.enabled or binding.provider_type != intent.provider_type
                or binding.provider_instance != intent.provider_instance
                or binding.project_id not in {None, current.project_id}
                or (bool(binding.resource_ids) and not set(context.resource_ids).issubset(binding.resource_ids))
                or type(provider) is not GitLabActionProvider):
            self._deny('publication_recovery_first_party_binding_changed')
        return current, intent, provider

    def _history(self, intent, values):
        history = self.intents.store.load(intent_id=intent.id)
        expected = next((r for r in history.receipts if r.id == values.expected_original_receipt_id), None)
        if values.expected_original_receipt_id is not None and (expected is None or expected.intent_id != intent.id
                or (expected.provider_type, expected.provider_instance, expected.action_id,
                    expected.idempotency_key, expected.correlation_id) !=
                   (intent.provider_type, intent.provider_instance, intent.action_id,
                    intent.idempotency_key, intent.correlation_id)):
            self._deny('publication_recovery_original_receipt_changed')
        latest = next((r for r in history.receipts if r.id == intent.last_receipt_id), None)
        verification = next((v for v in history.verifications if v.id == intent.last_verification_id), None)
        return expected, latest, verification

    def _workspace(self, context, values, current, provider):
        workspace = self.workspaces.get(values.execution_workspace_id, context.requester_actor)
        lease = self.workspaces.store.lease(workspace.lease_id)
        if (workspace.status != 'active' or workspace.execution_id != current.execution_id
                or workspace.subject != current.subject
                or workspace.owner_identity_id != context.requester_actor.identity_id
                or (workspace.organization_id, workspace.workspace_id, workspace.project_id) !=
                    (current.organization_id, current.workspace_id, current.project_id)
                or lease is None or not lease.active or lease.execution_workspace_id != workspace.id
                or lease.execution_id != workspace.execution_id or lease.subject != workspace.subject
                or lease.owner_identity_id != workspace.owner_identity_id
                or lease.resource_modes.get(context.resource_ids[0]) != 'write'):
            self._deny('publication_recovery_workspace_lease_changed')
        recovery = self.workspaces.publication_recovery(workspace.id, context.resource_ids[0], actor=context.requester_actor)
        scope = workspace.work_item_ref or (recovery.work_item_ref if recovery else None)
        if (scope != values.work_item_ref or (recovery is not None and
                (recovery.assignment_id != current.id or recovery.assignment_fence != current.fence))):
            self._deny('publication_recovery_workspace_work_changed')
        member = next((m for m in workspace.repository_members
            if m.resource_id == context.resource_ids[0] and m.access_mode == 'write'), None)
        if member is None or member.branch_name != values.expected_canonical_branch:
            self._deny('publication_recovery_canonical_branch_changed')
        root = Path(self.workspaces.backend.root).resolve(strict=True)
        path = Path(member.workspace_path).resolve(strict=True)
        if path == root or not path.is_relative_to(root):
            self._deny('publication_recovery_workspace_path_invalid')
        def git(*args):
            return provider.contract.git(path, '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', *args)
        head = git('rev-parse', 'HEAD')
        branch = git('symbolic-ref', '--short', 'HEAD')
        clean = not bool(git('status', '--porcelain'))
        ancestry = False
        if head == values.expected_head_revision:
            try:
                git('merge-base', '--is-ancestor', member.base_revision, head)
                ancestry = True
            except ValueError:
                ancestry = False
        return {'head_matches': head == values.expected_head_revision,
            'branch_matches': branch == member.branch_name, 'clean': clean,
            'base_ancestry': ancestry, 'actual_head': head, 'canonical_branch': member.branch_name}

    @staticmethod
    def _known_preprovider_failure(intent, receipt, canonical_branch):
        # This is a positive, canonical first-party failure classification AND
        # a specific code-owned pre-push attestation refusal. No receipt/result
        # absence by itself proves that an external effect did not happen.
        failure = intent.failure
        diagnostic = 'workspace Git head does not match requested publication'
        branch_diagnostic = ("workspace Git branch does not match canonical workspace branch "
            f"{canonical_branch!r}; keep the requested commit on that local branch")
        messages = {'provider precondition failed: ' + prefix + message
            for prefix in ('', 'GitLab branch could not be attested: ')
            for message in (diagnostic, branch_diagnostic)}
        return bool(intent.status == 'failed' and failure is not None
            and failure.reason_code == 'configuration_missing_or_invalid'
            and failure.retryability == 'after_remediation' and failure.outcome == 'known'
            and failure.source_native_code == 'ActionRequirementError'
            and failure.action_intent_id == intent.id and failure.execution_id == intent.execution_id
            and failure.attempt == intent.attempt and failure.correlation_id == intent.correlation_id
            and intent.last_error in messages and receipt is not None
            and receipt.id == intent.last_receipt_id and receipt.attempt == intent.attempt
            and receipt.outcome == 'failed' and receipt.details.get('reason') == 'ActionRequirementError'
            and receipt.result is None and receipt.provider_external_id is None)

    def _observe(self, context, values):
        current, intent, provider = self._identity(context, values)
        expected, receipt, verification = self._history(intent, values)
        facts = self._workspace(context, values, current, provider)
        verified = bool(intent.status == 'succeeded' and receipt is not None
            and receipt.intent_id == intent.id and receipt.attempt == intent.attempt
            and receipt.outcome == 'completed' and receipt.result is not None
            and receipt.result.status == 'succeeded'
            and (receipt.provider_type, receipt.provider_instance, receipt.action_id, receipt.correlation_id)
                == (intent.provider_type, intent.provider_instance, intent.action_id, intent.correlation_id)
            and receipt.idempotency_key == intent.idempotency_key
            and receipt.result.provider_binding_id == intent.binding_id
            and receipt.result.action_id == intent.action_id
            and receipt.result.idempotency_key == intent.idempotency_key
            and receipt.result.output.get('head_revision') == values.expected_head_revision
            and receipt.result.external_id == values.expected_head_revision
            and receipt.result.output.get('resource_id') == context.resource_ids[0]
            and receipt.result.output.get('branch') == intent.request.parameters.get('branch')
            and receipt.result.output.get('execution_workspace_id') == intent.request.parameters.get('execution_workspace_id')
            and verification is not None and verification.intent_id == intent.id
            and verification.verified is True and verification.verified_at >= receipt.received_at
            and verification.verified_at <= self.clock())
        facts.update(known_preprovider_failure=self._known_preprovider_failure(intent, receipt, values.expected_canonical_branch),
            same_origin=intent.execution_id == current.execution_id,
            original_status=str(intent.status), original_attempt=intent.attempt,
            original_max_attempts=intent.retry_policy.max_attempts)
        return current, intent, expected, receipt, verification, facts, verified

    def _result(self, disposition, intent, expected, receipt, verification, facts, verified, code=None):
        references = tuple(dict.fromkeys(v for v in (intent.id, expected.id if expected else None,
            receipt.id if receipt else None, verification.id if verification else None) if v))
        return CanonicalOperationResult(disposition=disposition,
            outputs={'observed_original_intent_id': intent.id, 'original_outcome': str(intent.status), 'verified': verified},
            facts=facts, original_references=references, code=code)

    @contextmanager
    def _original(self, context, values):
        token = _active_input.set((context, values, self.intents.get(values.original_intent_id, context.requester_actor).model_copy(deep=True)))
        try:
            with context.original_intent_scope(values.original_intent_id, values.expected_original_request_sha256):
                yield
        finally:
            _active_input.reset(token)

    async def execute(self, context, values):
        operation = context.step.control_plane.operation_id
        observed = await asyncio.to_thread(self._observe, context, values)
        current, intent, expected, receipt, verification, facts, verified = observed
        if operation == INSPECT:
            return self._result('succeeded', intent, expected, receipt, verification, facts, verified)
        if operation == VERIFY or verified:
            return self._result('succeeded' if verified else 'reconciliation_required',
                intent, expected, receipt, verification, facts, verified,
                None if verified else 'publication_original_verification_unconfirmed')
        if operation != RECOVER:
            self._deny('publication_recovery_operation_unavailable')
        if (not facts['same_origin'] or not facts['known_preprovider_failure']):
            return await self._read_only_reconcile(context, values, observed)
        return await self._recover_known(context, values, observed)

    async def _recover_known(self, context, values, observed):
        current, intent, expected, receipt, verification, facts, verified = observed
        if not all(facts[k] for k in ('head_matches', 'branch_matches', 'clean', 'base_ancestry')):
            return self._result('waiting', intent, expected, receipt, verification, facts, False,
                'publication_worker_owned_workspace_repair_required')
        if (intent.rollback_checkpoint is not None or not intent.provider_idempotency_supported
                or intent.attempt >= intent.retry_policy.max_attempts
                or intent.request.parameters.get('execution_workspace_id') != current.execution_workspace_id
                or expected is None or expected.id != intent.last_receipt_id):
            return self._result('refused', intent, expected, receipt, verification, facts, False,
                'publication_original_retry_not_admitted')
        # Repeat every mutable read immediately before entering the canonical
        # owner. That owner repeats authority/lease/attestation and remote CAS.
        fresh = await asyncio.to_thread(self._observe, context, values)
        if (not fresh[5]['known_preprovider_failure'] or fresh[1].last_receipt_id != values.expected_original_receipt_id
                or fresh[1].attempt != intent.attempt):
            return await self._read_only_reconcile(context, values, fresh)
        with self._original(context, values):
            retried = await self.intents.reconcile(intent.id,
                ActionIntentReconcileRequest(retry_if_idempotent=True), actor=context.worker_actor)
            if retried.status == 'pending':
                context.validate_current()
                worker_id = 'procedure-publication:' + current.id
                claimed = await asyncio.to_thread(self.intents.claim,
                    ActionIntentClaimRequest(worker_id=worker_id),
                    actor=context.worker_actor, intent_id=intent.id)
                if claimed is not None:
                    await self.intents.execute_claimed(intent.id, worker_id, actor=context.worker_actor)
        observed = await asyncio.to_thread(self._observe, context, values)
        return self._result('succeeded' if observed[-1] else 'reconciliation_required',
            *observed[1:5], observed[5], observed[6],
            None if observed[-1] else 'publication_original_outcome_unresolved')

    async def reconcile(self, context, values):
        # Repeated/crashed read operations remain reads. Their registrations
        # deliberately have no original-intent execution capability.
        if context.step.control_plane.operation_id in {INSPECT, VERIFY}:
            return await self.execute(context, values)
        if context.step.control_plane.operation_id != RECOVER:
            self._deny('publication_recovery_operation_unavailable')
        observed = await asyncio.to_thread(self._observe, context, values)
        if observed[-1]:
            return self._result('succeeded', *observed[1:5], observed[5], True)
        # The interpreter consumes this immutable prior diagnostic before its
        # RUNNING CAS. A crash/unknown reservation cannot reuse an old waiting
        # marker. Even a valid marker never replaces fresh original-state CAS,
        # current origin/custody, real remediation, idempotency or retry bounds.
        if (observed[5]['same_origin'] and observed[5]['known_preprovider_failure']
                and await asyncio.to_thread(self._previous_waiting, context, values, observed)):
            return await self._recover_known(context, values, observed)
        return await self._read_only_reconcile(context, values, observed)

    def _previous_waiting(self, context, values, observed):
        previous = getattr(context, 'previous_step', None)
        code = 'publication_worker_owned_workspace_repair_required'
        if (previous is None or previous.status != 'waiting'
                or previous.operation_code != code or len(previous.diagnostic_evidence_ids) != 1):
            return False
        service = self.intents.artifact_evidence
        if service is None or not getattr(context.checkpoint, 'id', None):
            return False
        proof = service.get_evidence(previous.diagnostic_evidence_ids[0], actor=context.requester_actor)
        intent, expected = observed[1], observed[2]
        if (expected is None or proof.lifecycle != 'valid' or proof.result != 'info'
                or (proof.organization_id, proof.workspace_id, proof.project_id, proof.work_item_ref)
                    != (intent.organization_id, intent.workspace_id, intent.project_id, context.assignment.work_item_ref)
                or proof.producer_identity_id != context.requester_actor.identity_id
                or proof.execution_id != context.assignment.execution_id
                or proof.execution_workspace_id != context.assignment.execution_workspace_id
                or proof.resource_ids != context.assignment.resource_ids
                or proof.provider != 'canonical-procedure-owner'
                or proof.source != f'procedure:{context.assignment.id}:{context.checkpoint.id}:{context.step.id}'
                or proof.metadata.get('canonical_operation') != RECOVER
                or not expected.received_at <= proof.observed_at <= self.clock()
                or proof.retention_expires_at is not None and proof.retention_expires_at <= self.clock()):
            return False
        raw = proof.metadata.get('canonical_result')
        if not isinstance(raw, str) or len(raw.encode()) > 262144:
            return False
        try:
            diagnostic = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(diagnostic, dict):
            return False
        facts = diagnostic.get('facts')
        return bool(isinstance(facts, dict) and diagnostic.get('operation') == RECOVER
            and diagnostic.get('disposition') == 'waiting' and diagnostic.get('code') == code
            and diagnostic.get('original_references') == [intent.id, expected.id]
            and facts.get('known_preprovider_failure') is True and facts.get('same_origin') is True
            and facts.get('original_status') == 'failed' and facts.get('original_attempt') == intent.attempt
            and facts.get('original_max_attempts') == intent.retry_policy.max_attempts)

    async def _read_only_reconcile(self, context, values, observed):
        with self._original(context, values):
            await self.intents.reconcile(observed[1].id,
                ActionIntentReconcileRequest(retry_if_idempotent=False), actor=context.worker_actor)
        observed = await asyncio.to_thread(self._observe, context, values)
        return self._result('succeeded' if observed[-1] else 'reconciliation_required',
            *observed[1:5], observed[5], observed[6],
            None if observed[-1] else 'publication_original_outcome_unresolved')

    def admit_original(self, context, intent, request, phase):
        active = _active_input.get()
        if active is None or active[0] is not context or phase not in {'execute', 'verify'}:
            self._deny('publication_original_admission_unavailable')
        values = active[1]
        current, original, _ = self._identity(context, values)
        state = context.checkpoint.steps[context.step.id]
        if (context.step.control_plane.operation_id != RECOVER or state.status != 'running'
                or intent.id != original.id or intent.execution_id != current.execution_id
                or request_checksum(request) != values.expected_original_request_sha256):
            self._deny('publication_original_admission_changed')
        return True

    def guard_original_retry(self, context, current):
        """Pure observed-state CAS, called inside the existing intent writer.

        Do not read any store/current-context callback here: the caller holds
        canonical locks and the existing publication scope guard owns custody.
        """
        active = _active_input.get()
        if active is None or active[0] is not context:
            self._deny('publication_original_retry_scope_unavailable')
        values, expected = active[1:]
        fields = ('id', 'status', 'attempt', 'last_receipt_id', 'binding_id',
            'provider_type', 'provider_instance', 'execution_id', 'work_item_ref',
            'requested_by', 'resource_ids', 'idempotency_key', 'credential_ref',
            'provider_idempotency_supported', 'rollback_checkpoint', 'failure', 'retry_policy')
        if (expected.status != 'failed' or expected.last_receipt_id != values.expected_original_receipt_id
                or any(getattr(current, name) != getattr(expected, name) for name in fields)
                or request_checksum(current.request) != values.expected_original_request_sha256):
            self._deny('publication_original_retry_observation_changed')
        return True
