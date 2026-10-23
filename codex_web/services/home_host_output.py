"""Bounded governed host-output derivatives; original command output stays denied."""
import hashlib
import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codex_web.artifact_evidence import ArtifactCreate
from codex_web.crypto import EncryptedEnvelope, EncryptionContext
from codex_web.data_governance import ContextFilterRequest, ReviewedHostOutputRelease
from codex_web.definitions import DefinitionContext, reference_for
from codex_web.home_output_policy import (HOME_OUTPUT_POLICY_KIND, HOME_OUTPUT_POLICY_VERSION,
    HomeOutputReleasePolicy, validate_home_output_policy)
from codex_web.identity import PrincipalKind, TenantScope
from codex_web.services.definitions import DefinitionKindSchema
from codex_web.services.home_host_action_provider import HOST_COMMAND_ACTION, HomeHostActionProvider
from codex_web.services.identity import IdentityService
from codex_web.services.data_governance import GovernanceConflictError

OUTPUT_READ = 'operator.host.output.read'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def context_digest(context):
    return digest(json.dumps(context.model_dump(mode='json'), sort_keys=True).encode())


class HostOutputSelection(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    stream: Literal['stdout', 'stderr'] = 'stdout'
    byte_start: int = Field(default=0, ge=0, le=65535, strict=True)
    byte_end: int = Field(ge=1, le=65536, strict=True)
    expected_sha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def bounds(self):
        if self.byte_end <= self.byte_start:
            raise ValueError('Nonempty bounded output range required')
        return self


class HostOutputReadOptions(BaseModel):
    """Native request for a server-selected prefix; never a release override."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    stream: Literal['stdout', 'stderr'] = 'stdout'
    max_bytes: int = Field(default=4096, ge=1, le=65536, strict=True)


class HomeHostOutputService:
    def __init__(self, *, home_host, worker_runtime, intents, artifacts, crypto, registry):
        self.home_host, self.runtime, self.intents = home_host, worker_runtime, intents
        self.artifacts, self.crypto, self.registry = artifacts, crypto, registry
        schema = DefinitionKindSchema(HOME_OUTPUT_POLICY_KIND, HOME_OUTPUT_POLICY_VERSION,
            validate_home_output_policy, publication_guard=self._publish_guard)
        registry.register_schema(schema)
        if artifacts.governance is None:
            raise ValueError('Canonical DataGovernance required for Home output')
        if artifacts.governance.host_output_release_validator is not None:
            raise ValueError('Home output release validator already registered')
        artifacts.governance.host_output_release_validator = self._validate_release

    def denied(self, message='Current governed Home output authority is unavailable'):
        raise GovernanceConflictError(message)

    def _admin(self, identity_id, organization_id, workspace_id):
        actor = self.home_host.broker.identity.actor_for_identity(identity_id,
            scope=TenantScope(organization_id=organization_id, workspace_id=workspace_id))
        if actor.principal_kind != PrincipalKind.HUMAN:
            self.denied()
        IdentityService.require_admin(actor)
        return actor

    def _origin(self, assignment_id, *, actor=None, worker_actor=None):
        assignment = self.home_host.broker.execution_workers.store.assignment(assignment_id)
        if assignment is None:
            self.denied()
        origin = self.home_host.authorize_operation(assignment, operation_id=OUTPUT_READ,
            worker_actor=worker_actor or self.runtime.worker_actor,
            requester_id=actor.identity_id if actor else None, mutating=False)
        if origin.envelope is None or origin.assignment.assigned_worker_id not in origin.envelope.trusted_worker_ids:
            self.denied()
        if actor and (actor.tenant != origin.requester.tenant or actor.principal_kind != PrincipalKind.HUMAN):
            self.denied()
        return origin

    def _publish_guard(self, record, publisher):
        policy = HomeOutputReleasePolicy.model_validate(record.payload)
        actor = self._admin(publisher, policy.organization_id, policy.workspace_id)
        if record.scope_type != 'project' or record.scope_id != 'home' or publisher != policy.administrator_id:
            self.denied('Output policy publication requires the same current Home HumanAdmin')
        project = self.home_host.projects.get('home', actor.tenant)
        operator = project.home_operator
        if not operator or not operator.thread_id:
            self.denied()
        binding = self.home_host.bootstrap_bindings.get_by_thread(operator.thread_id, actor)
        origin = self._origin(binding.assignment_id, actor=actor)
        self._match_policy(policy, origin)

    def _match_policy(self, policy, origin):
        a, e, pin = origin.assignment, origin.envelope, origin.assignment.agent_profile
        if (policy.organization_id != a.organization_id or policy.workspace_id != a.workspace_id
                or policy.administrator_id != origin.requester.identity_id or pin is None
                or (policy.agent_profile_id, policy.agent_profile_revision) != (pin.profile_id, pin.profile_revision)
                or policy.host_resource_id != e.host_resource_id
                or a.assigned_worker_id not in policy.trusted_worker_ids
                or not set(policy.trusted_worker_ids).issubset(e.trusted_worker_ids)
                or policy.max_excerpt_bytes > e.max_output_bytes):
            self.denied('Output policy does not match the current explicit Home envelope')

    def _policy(self, origin):
        pin = origin.envelope.output_release_policy
        if pin is None or pin.kind != HOME_OUTPUT_POLICY_KIND or pin.definition_schema_version != HOME_OUTPUT_POLICY_VERSION:
            self.denied('Host output needs HumanAdmin review or a current explicitly pinned output policy')
        a = origin.assignment
        record = self.registry.resolve(definition_id=pin.definition_id, kind=pin.kind,
            context=DefinitionContext(organization_id=a.organization_id, workspace_id=a.workspace_id, project_id='home'),
            now=time.time())
        if (reference_for(record) != pin or record.scope_type != 'project' or record.scope_id != 'home'
                or record.published_by != origin.requester.identity_id):
            self.denied('Pinned output policy is no longer current')
        policy = HomeOutputReleasePolicy.model_validate(record.payload)
        self._match_policy(policy, origin)
        return policy, pin

    def _source(self, artifact_id, origin):
        a, actor, e = origin.assignment, origin.requester, origin.envelope
        source = self.artifacts.get_artifact(artifact_id, actor=actor)
        intent_id = source.metadata.get('intent_id')
        if (source.project_id != 'home' or source.lifecycle != 'active' or source.provider != 'home-operator'
                or not isinstance(intent_id, str) or source.source != 'action-intent:' + intent_id
                or source.execution_id != a.execution_id or source.execution_workspace_id != a.execution_workspace_id
                or source.resource_ids != (e.host_resource_id,) or source.content is None or source.digest is None
                or source.content.encryption_key_ref != e.host_output_key_id or not source.governance_record_id
                or source.metadata.get('assignment_id') != a.id or source.metadata.get('fence') != a.fence
                or source.metadata.get('worker_id') != a.assigned_worker_id
                or source.producer_identity_id != self.runtime.worker_actor.identity_id):
            self.denied('Exact owned encrypted host output Artifact required')
        key = self.crypto.get_key(e.host_output_key_id, actor)
        if key.status != 'active' or key.purpose != 'artifact':
            self.denied('Current Artifact-purpose output key required')
        self.crypto._ensure_key_context(key, HomeHostActionProvider._output_context(a, e.host_resource_id, source.id))
        intent = self.intents.get(intent_id, actor)
        if (intent.action_id != HOST_COMMAND_ACTION or intent.provider_type != 'home-operator'
                or intent.provider_instance != 'trusted-host' or intent.execution_id != a.execution_id
                or intent.requested_by != actor.identity_id or intent.status not in {'succeeded', 'failed'}
                or intent.attempt != 1 or intent.completed_at is None or not intent.last_receipt_id
                or intent.request.parameters.get('assignment_id') != a.id
                or intent.request.parameters.get('fence') != a.fence
                or intent.resource_ids != (e.host_resource_id,)):
            self.denied('Known completed original host ActionIntent required')
        state = self.intents.store.load(intent_id=intent.id)
        receipts = [r for r in state.receipts if r.id == intent.last_receipt_id and r.intent_id == intent.id]
        if len(receipts) != 1:
            self.denied()
        receipt = receipts[0]
        result = receipt.result
        if (receipt.attempt != 1 or receipt.action_id != HOST_COMMAND_ACTION or receipt.outcome not in {'completed', 'failed'}
                or result is None or result.output.get('output_artifact_id') != source.id
                or result.output.get('process_completed') is not True
                or type(result.output.get('exit_code')) is not int
                or result.output.get('timed_out') is not False or result.output.get('output_truncated') is not False
                or result.output.get('limit_breach') is not None):
            self.denied('A positive known process-completion receipt is required; no unknown/partial output release')
        argv = tuple(intent.request.parameters.get('argv', ()))
        command_digest = 'sha256:' + digest('\0'.join(argv).encode())
        if result.output.get('command_digest') != command_digest:
            self.denied()
        return source, intent, receipt, argv, command_digest

    @staticmethod
    def _content_bound(chunks, maximum):
        data = bytearray()
        for chunk in chunks:
            if len(data) + len(chunk) > maximum:
                raise GovernanceConflictError('Host output content exceeds its hard bound')
            data.extend(chunk)
        return bytes(data)

    def _source_envelope(self, source, origin):
        if source.content.size_bytes > 2 * 1024 * 1024:
            self.denied('Host output envelope exceeds its hard bound')
        _, chunks = self.artifacts.open_artifact_content(source.id, actor=origin.requester)
        cipher = self._content_bound(chunks, 2 * 1024 * 1024)
        if digest(cipher) != source.digest.value:
            self.denied('Host output content changed')
        context = HomeHostActionProvider._output_context(origin.assignment, origin.envelope.host_resource_id, source.id)
        envelope = EncryptedEnvelope.model_validate_json(cipher)
        if (envelope.key_id != source.content.encryption_key_ref
                or envelope.context_digest != self.crypto._context_digest(context)):
            self.denied('Source output envelope key or context changed')
        return envelope, context

    def _decrypt_source(self, source, origin):
        envelope, context = self._source_envelope(source, origin)
        clear = self.crypto.decrypt(envelope, context, actor=origin.requester)
        if len(clear) > 1024 * 1024:
            self.denied('Host output plaintext exceeds its hard bound')
        output = json.loads(clear)
        if (not isinstance(output, dict) or set(output) != {'stdout', 'stderr'}
                or any(not isinstance(v, str) or len(v.encode()) > 3 * origin.envelope.max_output_bytes for v in output.values())):
            self.denied('Invalid canonical host output envelope')
        return output, context, envelope

    def _current_decryption_key(self, envelope, context, origin):
        key = self.crypto.get_key(envelope.key_id, origin.requester)
        self.crypto._ensure_key_context(key, context)
        version = self.crypto._version(key, envelope.key_version)
        if key.status != 'active' or key.purpose != 'artifact' or version.status == 'revoked':
            self.denied('Output decryption key or version was revoked')

    @staticmethod
    def _select(output, selection):
        raw = output[selection.stream].encode('utf-8')
        if selection.byte_end > len(raw):
            raise GovernanceConflictError('Output selection exceeds the actual stream')
        selected = raw[selection.byte_start:selection.byte_end]
        selected.decode('utf-8')  # No replacement/silent byte-boundary changes in reviewed text.
        if selection.expected_sha256 and digest(selected) != selection.expected_sha256:
            raise GovernanceConflictError('Previewed output selection changed')
        return selected

    def _command_rule(self, origin, argv, receipt, stream):
        policy, pin = self._policy(origin)
        rules = [rule for rule in policy.commands if rule.argv == argv]
        if (len(rules) != 1 or receipt.result.output['exit_code'] not in rules[0].exit_codes
                or stream not in rules[0].streams):
            self.denied('Output is outside the explicit command-release policy; HumanAdmin review required')
        return policy, pin

    def _rule(self, origin, argv, receipt, selection):
        policy, pin = self._command_rule(origin, argv, receipt, selection.stream)
        if selection.byte_end - selection.byte_start > policy.max_excerpt_bytes:
            self.denied('Output exceeds the explicit command-release policy bound')
        return policy, pin

    def preview(self, artifact_id, *, actor, expected_home_revision):
        if actor.principal_kind != PrincipalKind.HUMAN:
            self.denied('Only a current HumanAdmin may preview denied command output')
        IdentityService.require_admin(actor)
        artifact = self.artifacts.get_artifact(artifact_id, actor=actor)
        origin = self._origin(artifact.metadata.get('assignment_id'), actor=actor)
        if type(expected_home_revision) is not int or origin.binding.revision != expected_home_revision:
            self.denied('Home selection changed before output preview')
        source, _, receipt, _, _ = self._source(artifact_id, origin)
        output, context, envelope = self._decrypt_source(source, origin)
        final = self._origin(origin.assignment.id, actor=actor)
        if final.binding != origin.binding or final.assignment != origin.assignment:
            # Native renewal may legitimately change the lease: compare its
            # incarnation instead of certifying a changed Home selection.
            if (final.binding != origin.binding or final.assignment.fence != origin.assignment.fence
                    or final.assignment.execution_id != origin.assignment.execution_id):
                self.denied()
        current_source, _, current_receipt, _, _ = self._source(artifact_id, final)
        if current_source != source or current_receipt != receipt:
            self.denied('Output source or receipt changed during preview')
        self._current_decryption_key(envelope, context, final)
        return {'artifact_id': source.id, 'source_content_sha256': source.digest.value,
            'receipt_id': receipt.id, 'streams': output, 'model_context_allowed': False,
            'review_required': True, 'expires_at': min(time.time() + 60, final.assignment.lease.expires_at,
                final.assignment.deadline_at or float('inf'))}

    def list_sources(self, *, actor, expected_home_revision):
        """References only; at most twenty matching histories, existing catalog read."""
        if actor.principal_kind != PrincipalKind.HUMAN:
            self.denied('Only the current HumanAdmin may list protected command outputs')
        IdentityService.require_admin(actor)
        project = self.home_host.projects.get('home', actor.tenant)
        operator = project.home_operator
        if not operator or not operator.thread_id:
            self.denied()
        binding = self.home_host.bootstrap_bindings.get_by_thread(operator.thread_id, actor)
        origin = self._origin(binding.assignment_id, actor=actor)
        if type(expected_home_revision) is not int or origin.binding.revision != expected_home_revision:
            self.denied('Home selection changed before output listing')
        candidates = [item for item in self.intents.list(actor)
            if item.project_id == 'home' and item.action_id == HOST_COMMAND_ACTION
            and item.execution_id == origin.assignment.execution_id
            and item.request.parameters.get('assignment_id') == origin.assignment.id
            and item.request.parameters.get('fence') == origin.assignment.fence
            and item.status in {'succeeded', 'failed'} and item.last_receipt_id]
        items = []
        for intent in candidates[:20]:
            history = self.intents.history(intent.id, actor)
            receipts = [value for value in history['receipts'] if value['id'] == intent.last_receipt_id]
            if len(receipts) != 1:
                self.denied('Canonical output receipt is unavailable')
            output = (receipts[0].get('result') or {}).get('output') or {}
            artifact_id = output.get('output_artifact_id')
            if not isinstance(artifact_id, str):
                continue
            # These references grant no decrypt/review permission themselves.
            items.append({'artifact_id': artifact_id, 'intent_id': intent.id,
                'receipt_id': intent.last_receipt_id, 'status': intent.status,
                'completed_at': intent.completed_at, 'exit_code': output.get('exit_code'),
                'process_completed': output.get('process_completed') is True,
                'timed_out': output.get('timed_out') is True,
                'output_truncated': output.get('output_truncated') is True})
        final = self._origin(origin.assignment.id, actor=actor)
        if (final.binding != origin.binding or final.assignment.fence != origin.assignment.fence
                or final.assignment.execution_id != origin.assignment.execution_id):
            self.denied('Home origin changed during output listing')
        return {'items': items, 'has_more': len(candidates) > 20,
            'home_revision': origin.binding.revision, 'thread_id': origin.binding.thread_id,
            'model_context_allowed': False}

    def release(self, artifact_id, selection: HostOutputSelection, *, snapshot=None,
                worker_actor=None, actor=None, classification=None, reason=None, expected_home_revision=None):
        manual = actor is not None
        if manual:
            if actor.principal_kind != PrincipalKind.HUMAN:
                self.denied('Output review is HumanAdmin-only')
            IdentityService.require_admin(actor)
            source_hint = self.artifacts.get_artifact(artifact_id, actor=actor)
            origin = self._origin(source_hint.metadata.get('assignment_id'), actor=actor)
            if type(expected_home_revision) is not int or origin.binding.revision != expected_home_revision:
                self.denied('Home selection changed before output review')
            if classification not in {'internal', 'confidential'} or not reason or len(reason) > 512 or not selection.expected_sha256:
                self.denied('Explicit reviewed byte digest, reason and bounded classification required')
        else:
            if snapshot is None or worker_actor is None:
                self.denied()
            origin = self._origin(snapshot.id, worker_actor=worker_actor)
            if (origin.assignment.fence != snapshot.fence or origin.assignment.execution_id != snapshot.execution_id):
                self.denied()
        source, intent, receipt, argv, command_digest = self._source(artifact_id, origin)
        policy_ref = None
        if not manual:
            policy, policy_ref = self._rule(origin, argv, receipt, selection)
            classification, reason = policy.classification, 'Explicit current Home command-output release policy'
        output, source_context, source_envelope = self._decrypt_source(source, origin)
        self._current_decryption_key(source_envelope, source_context, origin)
        selected = self._select(output, selection)
        if len(selected) > origin.envelope.max_output_bytes:
            self.denied()
        stable = {'source': source.id, 'source_digest': source.digest.value,
            'selection': selection.model_dump(mode='json'), 'mode': 'human-review' if manual else 'policy',
            'policy_ref': policy_ref.model_dump(mode='json') if policy_ref else None,
            'classification': classification, 'reason': reason, 'home_revision': origin.binding.revision}
        key = digest(json.dumps(stable, sort_keys=True).encode())
        target = self.artifacts.create_artifact(ArtifactCreate(project_id='home',
            execution_id=origin.assignment.execution_id, execution_workspace_id=origin.assignment.execution_workspace_id,
            resource_ids=(origin.envelope.host_resource_id,), artifact_type='log', name='Reviewed host output excerpt',
            provider='home-operator', source='home-host-output-release:' + source.id,
            classification=classification, deny_model_context=False,
            metadata={'source_intent_id': intent.id, 'source_artifact_id': source.id,
                'source_receipt_id': receipt.id, 'excerpt_sha256': digest(selected)}),
            actor=origin.requester, source_artifact_ids=(source.id,), idempotency_key=key)
        if target.content is None:
            context = self._target_context(origin, target.id)
            encrypted = self.crypto.encrypt(origin.envelope.host_output_key_id, selected, context, actor=origin.requester)
            cipher = encrypted.model_dump_json().encode()
            target = self.artifacts.attach_artifact_content(target.id, (cipher,), actor=origin.requester,
                media_type='application/vnd.codex-web.encrypted-envelope+json',
                expected_sha256=digest(cipher), encryption_key_ref=encrypted.key_id)
        proof = ReviewedHostOutputRelease(mode='human-review' if manual else 'policy',
            source_artifact_id=source.id, source_record_id=source.governance_record_id,
            source_content_sha256=source.digest.value, source_intent_id=intent.id,
            source_receipt_id=receipt.id, source_assignment_id=origin.assignment.id,
            source_execution_id=origin.assignment.execution_id, source_fence=origin.assignment.fence,
            source_key_id=source.content.encryption_key_ref, source_key_context_sha256=context_digest(source_context),
            command_digest=command_digest, worker_id=origin.assignment.assigned_worker_id,
            requested_by=origin.requester.identity_id, home_thread_id=origin.binding.thread_id,
            home_selection_revision=origin.binding.revision, profile_definition=origin.assignment.execution_profile_definition,
            target_artifact_id=target.id, target_content_sha256=target.digest.value,
            stream=selection.stream, byte_start=selection.byte_start, byte_end=selection.byte_end,
            excerpt_sha256=digest(selected), classification=classification, policy_ref=policy_ref,
            reviewed_by=origin.requester.identity_id, reason=reason)
        governed = self.artifacts.governance.get_record(target.governance_record_id, origin.requester)
        if governed.host_output_release is not None:
            prior = governed.host_output_release
            if prior.model_dump(exclude={'reviewed_at'}) != proof.model_dump(exclude={'reviewed_at'}):
                self.denied('Prior output release does not match this exact selection')
            proof = prior
        self.artifacts.governance.release_reviewed_host_output(governed.id, proof, actor=origin.requester)
        # Never return the plaintext from the release operation itself.
        return {'artifact_id': target.id, 'governance_record_id': governed.id,
            'source_artifact_id': source.id, 'excerpt_sha256': digest(selected), 'mode': proof.mode}

    @staticmethod
    def _target_context(origin, artifact_id):
        return EncryptionContext(organization_id=origin.assignment.organization_id,
            workspace_id=origin.assignment.workspace_id, project_id='home',
            resource_id=origin.envelope.host_resource_id,
            object_type='home-host-output-release', object_id=artifact_id)

    def _validate_release(self, record, proof, actor):
        try:
            return self._validate_release_current(record, proof, actor)
        except GovernanceConflictError:
            raise
        except Exception as error:
            # Store/authority errors must not become policy fallback or disclose
            # a raw driver/path/credential message in the native response.
            raise GovernanceConflictError('Current reviewed output provenance is unavailable') from error

    def _validate_release_current(self, record, proof, actor):
        if (record.lifecycle != 'active'
                or record.retention_expires_at is not None and record.retention_expires_at <= time.time()
                or actor.identity_id != proof.requested_by or actor.principal_kind != PrincipalKind.HUMAN
                or (record.organization_id, record.workspace_id) != (actor.organization_id, actor.workspace_id)):
            self.denied()
        origin = self._origin(proof.source_assignment_id, actor=actor)
        if (origin.assignment.fence != proof.source_fence or origin.assignment.execution_id != proof.source_execution_id
                or origin.assignment.assigned_worker_id != proof.worker_id
                or origin.assignment.execution_profile_definition != proof.profile_definition
                or origin.binding.thread_id != proof.home_thread_id or origin.binding.revision != proof.home_selection_revision):
            self.denied('Reviewed output Home origin changed')
        source, intent, receipt, argv, command_digest = self._source(proof.source_artifact_id, origin)
        context = HomeHostActionProvider._output_context(origin.assignment, origin.envelope.host_resource_id, source.id)
        # An active derivative version cannot launder revocation of the version
        # that encrypted the original source. Its canonical ciphertext digest
        # binds this parsed envelope; no source plaintext is needed here.
        source_envelope, source_context = self._source_envelope(source, origin)
        self._current_decryption_key(source_envelope, source_context, origin)
        if (source.digest.value != proof.source_content_sha256 or source.governance_record_id != proof.source_record_id
                or intent.id != proof.source_intent_id or receipt.id != proof.source_receipt_id
                or source.content.encryption_key_ref != proof.source_key_id
                or context_digest(context) != proof.source_key_context_sha256 or command_digest != proof.command_digest
                or record.object_id != proof.target_artifact_id or record.source_record_ids != (proof.source_record_id,)):
            self.denied('Reviewed output source or completion receipt changed')
        original = self.artifacts.governance.get_record(proof.source_record_id, origin.requester)
        if (original.lifecycle != 'active' or original.host_output_release is not None
                or original.retention_expires_at is not None and original.retention_expires_at <= time.time()):
            self.denied('Reviewed output original source is unavailable')
        selection = HostOutputSelection(stream=proof.stream, byte_start=proof.byte_start,
            byte_end=proof.byte_end, expected_sha256=proof.excerpt_sha256)
        if proof.mode == 'policy':
            policy, pin = self._rule(origin, argv, receipt, selection)
            if pin != proof.policy_ref or policy.classification != proof.classification:
                self.denied('Reviewed output release policy changed')
        else:
            self._admin(proof.reviewed_by, actor.organization_id, actor.workspace_id)
        target = self.artifacts.get_artifact(proof.target_artifact_id, actor=origin.requester)
        if (target.lifecycle != 'active' or target.content is None or target.digest is None
                or target.governance_record_id != record.id
                or target.digest.value != proof.target_content_sha256
                or target.content.encryption_key_ref != proof.source_key_id
                or target.project_id != 'home' or target.execution_id != proof.source_execution_id
                or target.source != 'home-host-output-release:' + source.id
                or target.resource_ids != (origin.envelope.host_resource_id,)
                or target.metadata.get('excerpt_sha256') != proof.excerpt_sha256):
            self.denied('Reviewed output derivative changed')
        return origin

    def read(self, snapshot, artifact_id, *, worker_actor):
        origin = self._origin(snapshot.id, worker_actor=worker_actor)
        if snapshot.fence != origin.assignment.fence or snapshot.execution_id != origin.assignment.execution_id:
            self.denied()
        target = self.artifacts.get_artifact(artifact_id, actor=origin.requester)
        record = self.artifacts.governance.get_record(target.governance_record_id, origin.requester)
        proof = record.host_output_release
        if proof is None or proof.source_assignment_id != snapshot.id:
            self.denied('Output is not released for this current Home command origin')
        filtered = self.artifacts.governance.filter_context(
            ContextFilterRequest(record_ids=(record.id,)), actor=origin.requester)
        if filtered.allowed_record_ids != (record.id,):
            self.denied('Output remains denied to model context')
        if target.content.size_bytes > 512 * 1024:
            self.denied('Reviewed excerpt envelope exceeds its bound')
        _, chunks = self.artifacts.open_artifact_content(target.id, actor=origin.requester)
        cipher = self._content_bound(chunks, 512 * 1024)
        if digest(cipher) != proof.target_content_sha256:
            self.denied()
        envelope = EncryptedEnvelope.model_validate_json(cipher)
        context = self._target_context(origin, target.id)
        clear = self.crypto.decrypt(envelope, context, actor=origin.requester)
        if len(clear) != proof.byte_end - proof.byte_start or digest(clear) != proof.excerpt_sha256:
            self.denied('Reviewed excerpt content changed')
        final = self._origin(snapshot.id, worker_actor=worker_actor)
        if final.binding != origin.binding:
            self.denied()
        self._validate_release(record, proof, final.requester)
        self._current_decryption_key(envelope, context, final)
        return {'artifact_id': target.id, 'source_artifact_id': proof.source_artifact_id,
            'text': clear.decode('utf-8'), 'classification': record.classification,
            'policy_ref': proof.policy_ref.model_dump(mode='json') if proof.policy_ref else None,
            'review_mode': proof.mode, 'effect_verified': False}

    def read_source(self, snapshot, artifact_id, selection=None, *, worker_actor,
                    stream='stdout', max_bytes=4096):
        if selection is None:
            options = HostOutputReadOptions(stream=stream, max_bytes=max_bytes)
            origin = self._origin(snapshot.id, worker_actor=worker_actor)
            if snapshot.fence != origin.assignment.fence or snapshot.execution_id != origin.assignment.execution_id:
                self.denied()
            source, _, receipt, argv, _ = self._source(artifact_id, origin)
            policy, pin = self._command_rule(origin, argv, receipt, options.stream)
            output, context, envelope = self._decrypt_source(source, origin)
            raw = output[options.stream].encode('utf-8')
            limit = min(options.max_bytes, policy.max_excerpt_bytes, origin.envelope.max_output_bytes)
            prefix = raw[:limit]
            try:
                prefix.decode('utf-8')
            except UnicodeDecodeError as error:
                # raw came from a UTF-8 string: only the final split codepoint
                # can be incomplete. Never replace bytes in reviewed output.
                if error.end != len(prefix) or error.reason != 'unexpected end of data':
                    raise
                prefix = prefix[:error.start]
            if not prefix:
                final = self._origin(snapshot.id, worker_actor=worker_actor)
                current_source, _, current_receipt, current_argv, _ = self._source(artifact_id, final)
                current_policy, current_pin = self._command_rule(final, current_argv, current_receipt, options.stream)
                if (final.binding != origin.binding or final.assignment.fence != snapshot.fence
                        or final.assignment.execution_id != snapshot.execution_id
                        or current_source != source or current_receipt != receipt
                        or current_pin != pin or current_policy != policy):
                    self.denied('Output origin or policy changed during empty-stream read')
                self._current_decryption_key(envelope, context, final)
                return {'artifact_id': None, 'source_artifact_id': source.id, 'text': '',
                    'stream': options.stream, 'available_bytes': len(raw), 'selected_bytes': 0,
                    'empty_stream': not raw, 'excerpt_empty': True,
                    'classification': policy.classification, 'policy_ref': pin.model_dump(mode='json'),
                    'review_mode': 'policy', 'effect_verified': False}
            selection = HostOutputSelection(stream=options.stream, byte_end=len(prefix),
                expected_sha256=digest(prefix))
        elif stream != 'stdout' or max_bytes != 4096:
            self.denied('Explicit byte selection cannot be combined with default-read options')
        released = self.release(artifact_id, selection, snapshot=snapshot, worker_actor=worker_actor)
        return self.read(snapshot, released['artifact_id'], worker_actor=worker_actor)
