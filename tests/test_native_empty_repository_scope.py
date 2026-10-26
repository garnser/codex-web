"""Canonical scratch binding through the native credential context guard; no launch."""
import hashlib
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_workers import AssignmentClaimRequest, ExecutionRuntimeBinding
from codex_web.identity import PrincipalKind
from codex_web.resources import RepositoryTargetEvidence
from codex_web.services.codex_reasoning_policy import NativeReasoningPolicyError
from codex_web.services.native_model_provider import NativeModelInvocationContext, NativeProcessOwnership, _current_native_model_invocation
from codex_web.services.native_reasoning_bridge import NativeReasoningBinding
from codex_web.services.native_reasoning_runtime import build_native_reasoning_components
from tests import test_turn_execution_binding as canonical


class NativeEmptyRepositoryScopeTests(unittest.TestCase):
    def setUp(self):
        self.f = f = canonical.TurnExecutionBindingTests()
        f.setUp()
        self.addCleanup(f.tearDown)
        f.service._clock = time.time
        f.service.local_session_probe = lambda: True
        payload = f.execution_profiles.record(organization_id=f.actor.organization_id,
            workspace_id=f.actor.workspace_id).payload.copy()
        payload['profiles'] = [*payload['profiles'], dict(id='reasoning-only', name='Reasoning',
            description='Private scratch fixture', workspace_mode='scratch', repository_access='none',
            network_enabled=False, required_worker_capabilities=['command_execution'], control_plane_operations=[])]
        registry = f.execution_profiles.registry
        record = registry.create_draft(DefinitionDraftCreate(definition_id='execution.profiles.default',
            kind='execution-profile-catalog', definition_schema_version='1.3', scope_type='global',
            payload=payload, actor=f.actor.identity_id))
        registry.publish(record.record_id, DefinitionPublishRequest(actor=f.actor.identity_id))
        profile = AgentProfileExecutionBinding(profile_id='thinker', profile_revision=2,
            profile_record_id='profile-revision-two')
        bound = f.service.prepare_bootstrap(bootstrap_id='native-empty', execution_id='native-empty-exec',
            project_id=f.project.id, sandbox='read-only', approval_policy='never',
            execution_profile_id='reasoning-only', agent_profile=profile,
            runtime_binding=ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex-reasoning',
                capability_revision=1, sandbox_profiles=('read-only',)))
        self.worker_actor = f.actor.model_copy(update={'identity_id': f.worker.service_identity_id,
            'principal_kind': PrincipalKind.SERVICE, 'service_scopes': ('execution-worker:run',)})
        self.assignment = f.workers.claim(f.worker.id, AssignmentClaimRequest(),
            actor=self.worker_actor, assignment_id=bound.assignment_id)
        owner = NativeProcessOwnership(assignment_id=self.assignment.id)
        self.current = True
        def current():
            if not self.current:
                raise NativeReasoningPolicyError('current authority revoked')
        context = NativeModelInvocationContext(organization_id=f.actor.organization_id, workspace_id=f.actor.workspace_id,
            actor_id=f.actor.identity_id, project_id=f.project.id, activation_id='private', role_id='cpo',
            execution_id='executive:private:cpo', max_input_tokens=None, max_output_tokens=None,
            concrete_model='model', requester=f.actor, process_owner=owner, validate_current=current)
        token = _current_native_model_invocation.set(context)
        self.addCleanup(_current_native_model_invocation.reset, token)
        home = Path(f.temp.name)/'native-home'
        home.mkdir()
        (home/'auth.json').write_text('{}')  # Synthetic reference only; never read by provider.
        catalog = Path(f.temp.name)/'model.json'
        body = json.dumps({'models':[{'slug':'model','shell_type':'disabled',
            'apply_patch_tool_type':None,'experimental_supported_tools':[],'tool_mode':None}]}).encode()
        catalog.write_bytes(body)
        components = build_native_reasoning_components(binding=NativeReasoningBinding(provider_id='native',
            model_id='model', concrete_model='model', agent_profile_id='thinker', agent_profile_revision=2,
            execution_profile_id='reasoning-only', worker_id=f.worker.id),
            local_worker=NS(worker=f.worker, workspace_service=f.workspaces, control_actor=f.actor), host=NS(), codex_home=home,
            executable='/never-launched', catalog_path=catalog, catalog_sha256=hashlib.sha256(body).hexdigest(),
            validate_profile=lambda assignment: current(), invocation_authorizer=lambda *a: current,
            agent_profiles=NS(resolve_for_execution=lambda *a, **k:(NS(record_id=profile.profile_record_id),None)),
            execution_profiles=f.execution_profiles, threads=NS(), turns=None, sessions=None,
            usage=None, preflight=None, execution=None, hub=None, executives=None)
        self.credentials = components.manager.credential_provider
        self.consumed = []

    def use(self, assignment=None):
        return self.credentials.use(assignment or self.assignment, worker_id=self.f.worker.id,
            fence=self.assignment.fence, actor=self.worker_actor,
            consumer=lambda launch:self.consumed.append(launch.delegation.assignment_id))

    def test_canonical_scratch_binding_reaches_credential_consumer_without_launch(self):
        self.assertEqual(self.assignment.repository_scope.source, 'orchestration_only')
        self.assertIsNotNone(self.assignment.repository_target)
        self.use()
        self.assertEqual(self.consumed, [self.assignment.id])
        self.assertEqual(self.f.workers.store.assignment(self.assignment.id), self.assignment)
        self.assertEqual(self.assignment.resource_ids, ())

    def test_nonempty_cross_scope_and_target_mismatches_refuse_before_consumer(self):
        scope, target = self.assignment.repository_scope, self.assignment.repository_target
        evidence = (RepositoryTargetEvidence(source='explicit', repository_id='repo'),)
        updates = [
            {'repository_scope':scope.model_copy(update={'read_only_repository_ids':('repo',)})},
            {'repository_scope':scope.model_copy(update={'writable_repository_ids':('repo',)})},
            {'repository_scope':scope.model_copy(update={'organization_id':'other'})},
            {'repository_scope':scope.model_copy(update={'workspace_id':'other'})},
            {'repository_scope':scope.model_copy(update={'project_id':'other'})},
            {'repository_scope':scope.model_copy(update={'source':'explicit'})},
            {'repository_scope':scope.model_copy(update={'source_ref':'other'})},
            {'repository_scope':scope.model_copy(update={'write_mode':'coordinated'})},
            {'repository_target':target.model_copy(update={'mutable_repository_id':'repo'})},
            {'repository_target':target.model_copy(update={'read_only_repository_ids':('repo',)})},
            {'repository_target':target.model_copy(update={'project_id':'other'})},
            {'repository_target':target.model_copy(update={'source_ref':'other'})},
            {'repository_scope':scope.model_copy(update={'selection_evidence':evidence})},
            {'repository_target':target.model_copy(update={'selection_evidence':evidence})},
            {'repository_scope':None}, {'repository_target':None},
        ]
        for update in updates:
            with self.subTest(update=tuple(update)):
                with self.assertRaises(NativeReasoningPolicyError):
                    self.use(self.assignment.model_copy(update=update))
        self.assertEqual(self.consumed, [])

    def test_legacy_absent_envelopes_preserved_and_current_authority_still_required(self):
        self.use(self.assignment.model_copy(update={'repository_scope':None,'repository_target':None}))
        self.current = False
        with self.assertRaises(NativeReasoningPolicyError):
            self.use()
        self.assertEqual(self.consumed, [self.assignment.id])
