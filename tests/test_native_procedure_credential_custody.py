"""Real native bootstrap, selected procedure and credential-reference custody."""
import copy
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.agent_providers import AgentProviderUpsert
from codex_web.artifact_evidence import ArtifactCreate, sha256_digest
from codex_web.configuration import ConfigurationDraftCreate, ConfigurationPublishRequest, ConfigurationScope, SecretReference
from codex_web.execution_workers import (AssignmentClaimRequest, AssignmentStartRequest,
    ExecutionRuntimeBinding, ExecutionWorkerRegister)
from codex_web.execution_workspaces import ExecutionWorkspaceRelease
from codex_web.runtime.execution import ExecutionPrestartRefusal
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate
from codex_web.services.artifact_content import ArtifactContentRegistry, ArtifactContentService
from codex_web.services.codex_worker_configuration import CODEX_WORKER_ACCESS_TOKEN_CONFIG, install_codex_worker_configuration
from codex_web.services.configuration import ConfigurationService
from codex_web.services.local_artifact_content import LocalArtifactContentStore
from codex_web.services.secrets import SecretBroker
from codex_web.services.turn_execution_binding import TurnExecutionBindingService, TurnExecutionBindingError
from codex_web.storage.configuration_registry import ConfigurationRegistryStore
from codex_web.storage.secret_state import SecretStateStore
from tests import test_native_provider_preparation as native
from tests import test_turn_execution_binding as binding_fixtures


class NativeProcedureCredentialCustodyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Existing canonical Skill/Profile/security/provider fixture; replace its
        # old assignment with a genuine builder-created bootstrap, never seed
        # assignment.secret_refs through storage.
        self.fixture = native.NativeProviderPreparationTests()
        await self.fixture.asyncSetUp()
        self.f = f = self.fixture.fixture
        self.owner = self.fixture.owner
        from codex_web.skills import SkillUpdate, SkillPublish
        from codex_web.definitions import DefinitionReference
        content=copy.deepcopy(self.fixture.content)
        content["operation_triggers"]=["gitlab.logs.read"]
        content["inputs"]=[]
        step=content["steps"][0]
        step["inputs"]=[]
        step["outputs"]=[{"name":"done", "type":"boolean", "description":"Controlled observation"}]
        step["expected_evidence"]=[{"name":"observed", "kind":"verification", "description":"Controlled observation"}]
        content["steps"]=[step]
        updated=f.skills.update("native-provider-required", SkillUpdate(name="Credential-bound native procedure",
            instructions="Use the exact canonical read step.", operational_procedure=content,
            reason="Private credential-custody composition"), actor=f.actor)
        published=f.skills.publish(updated["skillId"],updated["recordId"],SkillPublish(),actor=f.actor)
        self.fixture.reference=DefinitionReference.model_validate(published["definitionReference"])
        self.admission = self.fixture.admission
        self.legacy_assignment = f.native_assignment
        self.worker_actor = f.identity.bootstrap_service_actor(identity_id='procedure-worker',
            name='Controlled procedure worker', scope=f.actor.tenant,
            service_scopes=('execution-worker:run', 'secret:use'))
        self.owner.worker_actor = self.worker_actor
        self.secrets = SecretBroker(SecretStateStore(f.sqlite),
            {'local': LocalFileSecretBackend(Path(f.temp.name)/'custody-secrets')})
        self.provider_secret = self.secrets.create(SecretCreate(name='Scoped provider fixture',
            value='private-provider-value-must-never-enter-native', provider='gitlab', purpose='api_token',
            allowed_identity_ids=[self.worker_actor.identity_id]), actor=f.actor)
        f.execution.secret_broker = self.secrets
        # Replace the fixture binding through the canonical registry owner.
        # Disable the old one solely in this private binding repository, not an
        # assignment grant; no public binding-update API currently exists.
        self.binding = self.fixture.binding
        f.registry.store.update(lambda state: state.model_copy(update={'bindings':[
            b.model_copy(update={'credential_ref': self.provider_secret.id}) if b.id == self.binding.id else b
            for b in state.bindings]}))
        self.binding = f.registry.binding(self.binding.id, f.actor)
        f.resources.bind_project(project=f.project, resource_id=f.repo.id, actor=f.actor)
        self.configuration = ConfigurationService(ConfigurationRegistryStore(f.sqlite))
        install_codex_worker_configuration(self.configuration)
        # A registered inert runtime supplies capabilities only. No model,
        # native process, provider call or credential material is executed.
        adapter = copy.copy(f.adapter); adapter.provider_id='openai'; adapter.runtime_id='codex'
        f.runtimes.register(adapter, capability_revision=7)
        f.providers.upsert(AgentProviderUpsert(id='openai', display_name='Private native capability fixture',
            declared_capabilities=adapter.capabilities, granted_capabilities=adapter.capabilities, health='healthy'), actor=f.actor)
        self.worker = f.workers.register(ExecutionWorkerRegister(service_identity_id=self.worker_actor.identity_id,
            version='private', pool='custody-fixture', capabilities=f.procedure_worker.capabilities,
            supported_execution_contract_versions=('thread-bootstrap/1.0',), max_concurrency=4), actor=f.actor)
        self.builder = TurnExecutionBindingService(self.configuration, binding_fixtures._Projects(f.project),
            f.resources, f.workspaces, f.workers, control_actor=f.actor, execution_profiles=f.profiles,
            secrets=self.secrets, local_session_probe=lambda: True)
        self.runtime_ref = None
        self.assignment = None

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def bootstrap(self, *, seeded=False):
        f=self.f
        f.workspaces.release(f.workspace.id, ExecutionWorkspaceRelease(reason='Replace old fixture bootstrap'), actor=f.actor)
        if seeded:
            runtime = self.secrets.create(SecretCreate(name='Runtime fixture', value='private-runtime-token',
                provider='openai', purpose='codex_access_token', allowed_identity_ids=[self.worker_actor.identity_id],
                expires_at=time.time()+200), actor=f.actor)
            self.runtime_ref=runtime.id
            self.publish_runtime_ref(runtime.id)
        self.options=dict(bootstrap_id='credential-custody-bootstrap', execution_id='credential-custody-execution',
            project_id=f.project.id, sandbox='workspace-write', approval_policy='on-request',
            explicit_repository_id=f.repo.id, execution_profile_id='repository-write',
            runtime_binding=ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex', capability_revision=7,
                authentication_mode='delegated_worker_token' if seeded else 'trusted_local_session'))
        bound=self.builder.prepare_bootstrap(**self.options)
        claimed=f.workers.claim(self.worker.id, AssignmentClaimRequest(lease_seconds=300),
            actor=self.worker_actor, assignment_id=bound.assignment_id)
        self.assignment=f.workers.start(self.worker.id, bound.assignment_id,
            AssignmentStartRequest(fence=claimed.fence, lease_token=claimed.lease.lease_token), actor=self.worker_actor)
        return bound

    def publish_runtime_ref(self, ref):
        f=self.f
        draft=self.configuration.create_draft(ConfigurationDraftCreate(key=CODEX_WORKER_ACCESS_TOKEN_CONFIG,
            scope_type=ConfigurationScope.PROJECT, scope_id=f.project.id, value=SecretReference(secret_id=ref),
            actor=f.actor.identity_id))
        self.configuration.publish(draft.id, ConfigurationPublishRequest(actor=f.actor.identity_id))

    async def admit(self):
        f=self.f
        return await self.admission(project=f.project, actor=f.actor, thread_id='custody-native-thread',
            execution_id=self.assignment.execution_id, assignment_id=self.assignment.id,
            sandbox=self.assignment.sandbox, source='queued:web', request_checksum='a'*64)

    def current(self):
        return self.f.workers.store.assignment(self.assignment.id)

    def input_artifact(self):
        f=self.f
        registry=ArtifactContentRegistry();registry.register(LocalArtifactContentStore(Path(f.temp.name)/'custody-content'))
        f.artifacts.content=ArtifactContentService(registry)
        self.owner.evidence=f.artifacts
        raw=b'{}'
        artifact=f.artifacts.create_artifact(ArtifactCreate(project_id=f.project.id,
            execution_id=self.assignment.execution_id, execution_workspace_id=self.assignment.execution_workspace_id,
            resource_ids=self.assignment.resource_ids, artifact_type='report', name='Private procedure input',
            digest=sha256_digest(raw)), actor=f.actor)
        f.artifacts.attach_artifact_content(artifact.id,(raw,),actor=f.actor)
        return artifact

    async def test_real_supporting_memory_retrieval_stays_outside_admission_transaction(self):
        from contextlib import ExitStack
        import threading
        from codex_web.services.organizational_memory import OrganizationalMemoryService
        from codex_web.services.procedure_discovery import ProcedureSupportingContextService
        from codex_web.storage.organizational_memory import OrganizationalMemoryStore

        f = self.f
        from codex_web.services.data_governance import DataGovernanceService
        from codex_web.storage.data_governance import DataGovernanceStore
        governance = DataGovernanceService(DataGovernanceStore(f.sqlite))
        memory = OrganizationalMemoryService(OrganizationalMemoryStore(f.sqlite),
            governance, f.authority)
        self.admission.discovery.supporting_context = ProcedureSupportingContextService(memory)
        self.bootstrap()
        artifact = self.input_artifact()
        callback = threading.local()
        nested_attempts = []
        writes = []
        mutate = f.sqlite.record_mutate_many
        update = f.sqlite.update

        def guarded_mutate(selected, updater):
            def inside(rows):
                callback.active = True
                try:
                    return updater(rows)
                finally:
                    callback.active = False
            return mutate(selected, inside)

        def guarded_update(namespace, *args, **kwargs):
            # Match PostgresStateStore._connection's actual callback rule,
            # while retaining the real SQLite owning transaction and writes.
            if getattr(callback, "active", False):
                nested_attempts.append(namespace)
                raise RuntimeError("nested mutation inside a canonical state callback is unsupported")
            writes.append(namespace)
            return update(namespace, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(patch.object(f.sqlite, "record_mutate_many", side_effect=guarded_mutate))
            stack.enter_context(patch.object(f.sqlite, "update", side_effect=guarded_update))
            stack.enter_context(patch.object(self.secrets, "use", side_effect=AssertionError("material forbidden")))
            try:
                discovery, text = await self.admit()
            except ExecutionPrestartRefusal as exc:
                self.assertEqual(self.current().secret_refs, ())
                self.assertIsNone(self.current().native_credential_custody.admitted_at)
                self.fail(f"Real supporting Memory blocked native admission: {exc.detail}; nested={nested_attempts}")
            self.assertTrue(discovery.admitted)
            self.assertEqual(nested_attempts, [])
            self.assertIn(memory.store.namespace, writes)
            retrieval_ids = [ref.split(":", 1)[1] for ref in discovery.supporting_sources
                             if ref.startswith("memory-retrieval:")]
            self.assertTrue(retrieval_ids)
            for identifier in retrieval_ids:
                self.assertEqual(memory.retrieval_run(identifier, actor=f.actor).filters["project_ids"], [f.project.id])
            self.assertEqual(self.current().secret_refs, (self.provider_secret.id,))
            self.assertIsNotNone(self.current().native_credential_custody.admitted_at)
            before_guard = self.current().model_dump(mode="json")
            retrievals_before_guard = memory.store.load().retrieval_runs
            writes_before_guard = list(writes)
            guard = self.admission.preparation_input_guard(discovery, project=f.project, actor=f.actor,
                execution_id=self.assignment.execution_id, assignment_id=self.assignment.id,
                sandbox=self.assignment.sandbox, request_checksum="a" * 64)
            guard()
            self.assertEqual(writes, writes_before_guard)
            self.assertEqual(memory.store.load().retrieval_runs, retrievals_before_guard)
            self.assertEqual(self.current().model_dump(mode="json"), before_guard)
            started = self.owner.start(self.assignment.id, definition=self.fixture.reference,
                input_snapshot_id=artifact.id, actor=f.actor)
            self.assertIsNotNone(started.procedure_checkpoint)
        self.assertEqual(f.ops.calls, [])

    async def test_real_zero_auth_bootstrap_admits_provider_and_starts_procedure_without_material(self):
        bound=self.bootstrap()
        self.assertEqual(self.assignment.secret_refs, ())
        artifact=self.input_artifact()
        with self.assertRaisesRegex(Exception, 'procedure_(secret_reference_not_assigned|credential_initial_admission_required)'):
            self.owner.start(self.assignment.id, definition=self.fixture.reference,
                input_snapshot_id=artifact.id, actor=self.f.actor)
        with patch.object(self.secrets, 'use', side_effect=AssertionError('native material delivery forbidden')):
            discovery,_=await self.admit()
            self.assertTrue(discovery.admitted)
            current=self.current()
            self.assertEqual(current.secret_refs, (self.provider_secret.id,))
            self.assertIsNone(current.native_credential_custody.runtime_secret_ref)
            self.assertIsNotNone(current.native_credential_custody.admitted_at)
            self.assertEqual(self.builder.prepare_bootstrap(**self.options).secret_ref, None)
            started=self.owner.start(current.id, definition=self.fixture.reference,
                input_snapshot_id=artifact.id, actor=self.f.actor)
            self.assertIsNotNone(started.procedure_checkpoint)
        self.assertEqual(self.f.ops.calls, [])
        self.assertNotIn('private-provider-value', json.dumps(discovery.model_dump(mode='json')))

    async def test_seeded_auth_reuse_and_delegation_never_select_provider_credential(self):
        from codex_web.services.codex_auth_delegation import CodexAuthDelegationService
        self.bootstrap(seeded=True)
        with patch.object(self.secrets, 'use', side_effect=AssertionError('native material delivery forbidden')):
            await self.admit()
            current=self.current()
            self.assertEqual(set(current.secret_refs), {self.runtime_ref, self.provider_secret.id})
            self.assertEqual(self.builder.prepare_bootstrap(**self.options).secret_ref, self.runtime_ref)
            selected=CodexAuthDelegationService(self.secrets)._reference(current, actor=self.worker_actor, now=time.time())
            self.assertEqual(selected.id, self.runtime_ref)
            before=current.model_dump(mode='json')
            self.publish_runtime_ref(self.provider_secret.id)
            with self.assertRaises(TurnExecutionBindingError) as caught:
                self.builder.prepare_bootstrap(**self.options)
            self.assertEqual(caught.exception.code, 'authentication_custody_changed')
            self.assertEqual(self.current().model_dump(mode='json'), before)

    async def test_distinct_ingress_without_provider_use_keeps_canonical_owner_custody(self):
        from codex_web.identity import HumanUserCreate
        human,_=self.f.identity.create_human_user(HumanUserCreate(display_name='Authorized separate input author',
            roles=('member',)),actor=self.f.actor)
        requester=self.f.identity.actor_for_identity(human.id,scope=self.f.actor.tenant)
        from codex_web.services.secrets import SecretUseDeniedError
        with self.assertRaises(SecretUseDeniedError):
            self.secrets.metadata(self.provider_secret.id,actor=requester,require_use=True)
        self.bootstrap()
        f=self.f
        discovery,_=await self.admission(project=f.project,actor=requester,thread_id='custody-native-thread',
            execution_id=self.assignment.execution_id,assignment_id=self.assignment.id,
            sandbox=self.assignment.sandbox,source='web',request_checksum='b'*64)
        self.assertTrue(discovery.admitted)
        self.assertNotEqual(requester.identity_id,self.current().created_by)
        self.assertEqual(self.current().native_credential_custody.requester_identity_id,f.actor.identity_id)
        artifact=self.input_artifact()
        started=self.owner.start(self.assignment.id,definition=self.fixture.reference,input_snapshot_id=artifact.id,actor=f.actor)
        self.assertIsNotNone(started.procedure_checkpoint)
        self.assertEqual(self.current().native_credential_custody.requester_identity_id,f.actor.identity_id)

    async def test_legacy_missing_marker_cannot_gain_provider_reference(self):
        self.assignment=self.legacy_assignment
        before=self.current().model_dump(mode='json')
        with self.assertRaises(ExecutionPrestartRefusal) as caught:
            await self.admit()
        self.assertEqual(caught.exception.detail['code'],'procedure_credential_initial_admission_required')
        self.assertEqual(self.current().model_dump(mode='json'),before)
        self.assertIsNone(self.current().native_credential_custody)
        self.assertEqual(self.current().secret_refs,())

    async def test_repeat_and_input_guard_read_only_binding_change_refuses_without_append(self):
        self.bootstrap()
        discovery,_=await self.admit()
        before=self.current().model_dump(mode='json')
        guard=self.admission.preparation_input_guard(discovery,project=self.f.project,actor=self.f.actor,
            execution_id=self.assignment.execution_id,assignment_id=self.assignment.id,sandbox=self.assignment.sandbox,
            request_checksum='a'*64)
        guard()
        self.assertEqual(self.current().model_dump(mode='json'),before)
        await self.admit()
        self.assertEqual(self.current().model_dump(mode='json'),before)
        self.f.registry.bind(ActionProviderBindingCreate(provider_type='ops',provider_instance='test',
            project_id=self.f.project.id,resource_ids=(self.f.repo.id,),credential_ref=self.provider_secret.id),
            actor=self.f.actor,resources=self.f.resources)
        with self.assertRaises(ExecutionPrestartRefusal): guard()
        with self.assertRaises(ExecutionPrestartRefusal): await self.admit()
        # Existing admission retains a denial discovery; credential custody,
        # scope and existing proofs stay exact. The input guard above writes none.
        after=self.current().model_dump(mode='json')
        self.assertEqual(after['native_credential_custody'],before['native_credential_custody'])
        self.assertEqual(after['secret_refs'],before['secret_refs'])
        self.assertEqual(after['procedure_checkpoint'],before['procedure_checkpoint'])
        self.assertEqual(after['procedure_discoveries'][:len(before['procedure_discoveries'])],before['procedure_discoveries'])

    async def test_actual_worker_secret_use_denied_before_atomic_admission(self):
        self.bootstrap()
        before=self.current().model_dump(mode='json')
        self.owner.worker_actor=self.worker_actor.model_copy(update={'service_scopes':('execution-worker:run',)})
        with self.assertRaises(ExecutionPrestartRefusal) as caught:
            await self.admit()
        self.assertEqual(caught.exception.detail['code'],'procedure_credential_secret_use_denied')
        self.assertEqual(self.current().model_dump(mode='json'),before)

    async def test_same_fence_renewal_between_resolution_and_cas_is_allowed(self):
        from codex_web.execution_workers import AssignmentRenewRequest
        self.bootstrap()
        original=self.f.workers.store.transition_assignment
        def renew_then_commit(*args,**kwargs):
            with patch.object(self.f.workers.store,'transition_assignment',original):
                self.f.workers.renew(self.worker.id,self.assignment.id,
                    AssignmentRenewRequest(fence=self.assignment.fence,lease_token=self.assignment.lease.lease_token,
                        lease_seconds=600),actor=self.worker_actor)
            return original(*args,**kwargs)
        with patch.object(self.f.workers.store,'transition_assignment',side_effect=renew_then_commit):
            await self.admit()
        self.assertEqual(self.current().secret_refs,(self.provider_secret.id,))
        self.assertGreater(self.current().lease.expires_at,self.assignment.lease.expires_at)

    async def test_changed_fence_between_resolution_and_cas_never_admits(self):
        from codex_web.execution_workers import AssignmentCancelRequest
        self.bootstrap()
        original=self.f.workers.store.transition_assignment
        def cancel_then_commit(*args,**kwargs):
            # Original canonical retirement, not a fake assignment mutation.
            with patch.object(self.f.workers.store,'transition_assignment',original):
                self.f.workers.cancel_bootstrap(self.assignment.id,AssignmentCancelRequest(
                    expected_fence=self.assignment.fence,reason='Controlled private custody retirement'),actor=self.f.actor)
            return original(*args,**kwargs)
        with patch.object(self.f.workers.store,'transition_assignment',side_effect=cancel_then_commit):
            with self.assertRaises(ExecutionPrestartRefusal) as caught:
                await self.admit()
        self.assertEqual(caught.exception.detail['code'],'procedure_credential_assignment_changed')
        self.assertEqual(self.current().secret_refs,())
        self.assertIsNone(self.current().native_credential_custody.admitted_at)

    async def test_revoked_provider_reference_blocks_current_use_without_changing_custody(self):
        self.bootstrap()
        discovery,_=await self.admit()
        before=self.current().model_dump(mode='json')
        self.secrets.revoke(self.provider_secret.id,actor=self.f.actor)
        guard=self.admission.preparation_input_guard(discovery,project=self.f.project,actor=self.f.actor,
            execution_id=self.assignment.execution_id,assignment_id=self.assignment.id,sandbox=self.assignment.sandbox,
            request_checksum='a'*64)
        with self.assertRaises(ExecutionPrestartRefusal): guard()
        artifact=self.input_artifact()
        with self.assertRaisesRegex(Exception,'procedure_credential_reference_revoked'):
            self.owner.start(self.assignment.id,definition=self.fixture.reference,input_snapshot_id=artifact.id,actor=self.f.actor)
        self.assertEqual(self.current().model_dump(mode='json'),before)

    async def test_service_ingress_without_secret_use_is_not_provider_requester(self):
        from codex_web.services.secrets import SecretUseDeniedError
        ingress=self.f.identity.bootstrap_service_actor(identity_id='native-input-delivery',
            name='Private input delivery',scope=self.f.actor.tenant,service_scopes=('thread:write',))
        with self.assertRaises(SecretUseDeniedError):
            self.secrets.metadata(self.provider_secret.id,actor=ingress,require_use=True)
        self.bootstrap()
        seen=[]
        def authorize_input(**kwargs):
            # The ingress guard remains separate and receives the actual actor;
            # it supplies no provider/secret credential authority.
            self.assertEqual(kwargs['actor'],ingress)
            self.assertEqual(kwargs['assignment'].id,self.assignment.id)
            seen.append(kwargs['source'])
        self.admission.input_authorizer=authorize_input
        discovery,_=await self.admission(project=self.f.project,actor=ingress,thread_id='custody-native-thread',
            execution_id=self.assignment.execution_id,assignment_id=self.assignment.id,
            sandbox=self.assignment.sandbox,source='slack',request_checksum='c'*64)
        self.assertEqual(seen,['slack'])
        self.assertTrue(discovery.admitted)
        self.assertEqual(self.current().native_credential_custody.requester_identity_id,self.f.actor.identity_id)
        artifact=self.input_artifact()
        started=self.owner.start(self.assignment.id,definition=self.fixture.reference,
            input_snapshot_id=artifact.id,actor=self.f.actor)
        self.assertIsNotNone(started.procedure_checkpoint)

    async def test_legacy_generic_secret_check_keeps_distinct_execution_worker_contract(self):
        from codex_web.execution_workers import ExecutionAssignmentCreate
        legacy=self.f.workers.create_assignment(ExecutionAssignmentCreate(
            subject={'kind':'thread','ref':'legacy-secret-custody'}, execution_id='legacy-provider-contract',
            project_id=self.f.project.id,resource_ids=(self.f.repo.id,),execution_contract_version='1.0',
            required_capabilities=('command_execution',),secret_refs=(self.provider_secret.id,)),actor=self.f.actor)
        self.owner.worker_actor=self.f.identity.bootstrap_service_actor(identity_id='different-provider-executor',
            name='Distinct legacy ActionIntent executor',scope=self.f.actor.tenant,service_scopes=('action-intent:worker',))
        with patch.object(self.secrets,'use',side_effect=AssertionError('material delivery forbidden')):
            self.owner._secret(self.provider_secret.id,self.f.actor,legacy)
        self.assertIsNone(legacy.native_credential_custody)

    async def test_marked_native_custody_refuses_different_configured_worker_actor(self):
        self.bootstrap()
        await self.admit()
        before=self.current().model_dump(mode='json')
        self.owner.worker_actor=self.f.identity.bootstrap_service_actor(identity_id='wrong-native-custody-worker',
            name='Different authenticated worker',scope=self.f.actor.tenant,
            service_scopes=('execution-worker:run','secret:use'))
        with self.assertRaisesRegex(Exception,'procedure_credential_worker_unavailable'):
            self.owner._secret(self.provider_secret.id,self.f.actor,self.current())
        self.assertEqual(self.current().model_dump(mode='json'),before)

    async def test_seeded_delegate_ignores_same_purpose_provider_reference(self):
        from codex_web.services.codex_auth_delegation import CodexAuthDelegationService
        self.bootstrap(seeded=True)
        provider_ref=self.secrets.create(SecretCreate(name='Coincident provider metadata',
            value='never-deliver-provider-material',provider='openai',purpose='codex_access_token',
            allowed_identity_ids=[self.worker_actor.identity_id],expires_at=time.time()+200),actor=self.f.actor)
        self.f.registry.store.update(lambda state:state.model_copy(update={'bindings':[
            b.model_copy(update={'credential_ref':provider_ref.id}) if b.id==self.binding.id else b
            for b in state.bindings]}))
        await self.admit()
        current=self.current()
        self.assertEqual(set(current.secret_refs),{self.runtime_ref,provider_ref.id})
        with patch.object(self.secrets,'use',side_effect=AssertionError('no material delivery')):
            selected=CodexAuthDelegationService(self.secrets)._reference(current,actor=self.worker_actor,now=time.time())
        self.assertEqual(selected.id,self.runtime_ref)

    async def test_agent_owned_delegate_never_substitutes_same_purpose_provider_reference(self):
        from codex_web.services.codex_auth_delegation import CodexAuthDelegationService, CodexAuthDelegationUnavailableError
        self.bootstrap()
        provider_ref=self.secrets.create(SecretCreate(name='Coincident provider metadata',
            value='never-deliver-provider-material',provider='openai',purpose='codex_access_token',
            allowed_identity_ids=[self.worker_actor.identity_id],expires_at=time.time()+200),actor=self.f.actor)
        self.f.registry.store.update(lambda state:state.model_copy(update={'bindings':[
            b.model_copy(update={'credential_ref':provider_ref.id}) if b.id==self.binding.id else b
            for b in state.bindings]}))
        await self.admit()
        self.assertEqual(self.current().secret_refs,(provider_ref.id,))
        with patch.object(self.secrets,'use',side_effect=AssertionError('no provider material delivery')):
            with self.assertRaises(CodexAuthDelegationUnavailableError):
                CodexAuthDelegationService(self.secrets)._reference(self.current(),actor=self.worker_actor,now=time.time())

    async def test_new_published_procedure_cannot_borrow_original_credential_custody(self):
        from codex_web.skills import SkillCreate, SkillPublish
        from codex_web.definitions import DefinitionReference
        self.bootstrap()
        await self.admit()
        before=self.current().model_dump(mode='json')
        record=self.f.skills.definitions.get_record(self.fixture.reference.record_id)
        other_content=copy.deepcopy(record.payload['operational_procedure'])
        other_content['conflict_group']='different-private-procedure-group'
        created=self.f.skills.create(SkillCreate(skill_id='different-native-procedure',name='Different published procedure',
            instructions='Use canonical provider boundary.',operational_procedure=other_content),actor=self.f.actor)
        published=self.f.skills.publish(created['skillId'],created['recordId'],SkillPublish(),actor=self.f.actor)
        reference=DefinitionReference.model_validate(published['definitionReference'])
        artifact=self.input_artifact()
        with self.assertRaisesRegex(Exception,'procedure_credential_procedure_not_admitted'):
            self.owner.start(self.assignment.id,definition=reference,input_snapshot_id=artifact.id,actor=self.f.actor)
        self.assertEqual(self.current().model_dump(mode='json'),before)
        # Original exact pin remains usable; publication did not rewrite it.
        started=self.owner.start(self.assignment.id,definition=self.fixture.reference,
            input_snapshot_id=artifact.id,actor=self.f.actor)
        repeated=self.owner.start(self.assignment.id,definition=self.fixture.reference,
            input_snapshot_id=artifact.id,actor=self.f.actor)
        self.assertEqual(repeated.procedure_checkpoint.id,started.procedure_checkpoint.id)

    async def test_alternate_binding_cannot_borrow_same_admitted_reference(self):
        from codex_web.skills import SkillDefinition
        self.bootstrap()
        await self.admit()
        current=self.current()
        provider=copy.copy(self.f.ops);provider.provider_instance='other'
        self.f.registry.register(provider,tenant_scope=self.f.actor.tenant)
        alternative=self.f.registry.bind(ActionProviderBindingCreate(provider_type='ops',provider_instance='other',
            project_id=self.f.project.id,resource_ids=(self.f.repo.id,),credential_ref=self.provider_secret.id),
            actor=self.f.actor,resources=self.f.resources)
        procedure=SkillDefinition.model_validate(self.f.skills.definitions.get_record(self.fixture.reference.record_id).payload).operational_procedure
        step=procedure.steps[0].model_copy(update={'action':procedure.steps[0].action.model_copy(update={'provider_instance':'other'})})
        before=current.model_dump(mode='json')
        self.assertNotIn(alternative.id,current.native_credential_custody.provider_bindings)
        with patch.object(self.secrets,'use',side_effect=AssertionError('no material delivery')):
            with self.assertRaisesRegex(Exception,'procedure_credential_binding_not_admitted'):
                self.owner._request(step,current,self.f.actor,procedure,{})
        self.assertEqual(self.current().model_dump(mode='json'),before)
        self.assertEqual(self.f.ops.calls,[])
