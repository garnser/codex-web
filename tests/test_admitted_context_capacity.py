"""Actual adapter append on a context produced by ordinary native admission.

Private fixture only: no host capacity PASS, live data, application startup or
provider credentials. A (original producer/requester), B (authenticated worker)
and C (internal allocation controller) remain distinct actual canonical actors.
"""
import hashlib
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.agent_profiles import AgentProfileAccessPolicy,AgentProfileExecutionBinding,AgentProfileUpdate
from codex_web.services.agent_profiles import AgentProfileAccessDenied
from codex_web.artifact_evidence import ArtifactCreate,EvidenceCreate
from codex_web.execution_workers import AssignmentCancelRequest,AssignmentCompleteRequest,ExecutionRuntimeBinding,WorkerCapability
from codex_web.execution_workspaces import ExecutionWorkspaceRelease
from codex_web.models import Project
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.services.read_only_resource_context import ReadOnlyResourceContextAdmission
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.services.turn_execution_binding import TurnExecutionBindingService
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from tests import test_worker_capacity_verifications as capacities
from tests import test_turn_execution_binding as binding_fixture
from tests.read_only_resource_context_harness import NativeContextHarness,create_requester
from tests.test_recovery_scheduled_postgres import _PostgresFixture


class AdmittedContextCapacityTests(unittest.IsolatedAsyncioTestCase):
    _assignment=staticmethod(capacities.CapacityWorkerVerificationTests._assignment)
    _publish_worker_authority=capacities.CapacityWorkerVerificationTests._publish_worker_authority
    _request=capacities.CapacityWorkerVerificationTests._request
    _publish_profile=capacities.CapacityWorkerVerificationTests._publish_profile
    _stop_services=capacities.CapacityWorkerVerificationTests._stop_services
    verification_resources=capacities.CapacityWorkerVerificationTests.verification_resources
    capacity_evidence=capacities.CapacityWorkerVerificationTests.capacity_evidence
    post=capacities.CapacityWorkerVerificationTests.post

    async def asyncSetUp(self):
        await capacities.CapacityWorkerVerificationTests.asyncSetUp(self)
        await self.broker.stop()
        # The inherited verification assignment is a generic WorkItem subject,
        # not a thread bootstrap. Settle its actual worker lease through the
        # generic lifecycle, then release only its owned workspace, keeping files.
        previous=self.assignment
        terminal=self.workers.complete(self.worker.id,previous.id,AssignmentCompleteRequest(
            fence=previous.fence,lease_token=previous.lease.lease_token,succeeded=False,
            failure_code='cancelled',failure_message='Private fixture ends prior verification custody'),
            actor=self.worker_actor)
        self.assertEqual(terminal.status,'failed')
        self.assertEqual(terminal.fence,previous.fence)
        self.assertIsNone(terminal.lease)
        self.assertIsNotNone(terminal.completed_at)
        workspace=self.workspaces.release(previous.execution_workspace_id,ExecutionWorkspaceRelease(
            reason='Private terminal verification custody'),actor=self.admin,preserve_files=True)
        self.assertEqual(workspace.status,'released')
        self.assertIsNotNone(self.workspaces.store.lease(workspace.lease_id).released_at)
        self.assertEqual(self.workers.store.assignment(previous.id),terminal)
        root=Path(self.temp.name)
        project=Project(id='project-a',name='Private native context',path=str(root/'repo'))
        self.resources.bind_project(project=project,resource_id=self.target.id,actor=self.admin)
        # Explicitly absent in the imported flat representation throughout.
        item=self.items.get(self.ref)
        self.items.put(self.ref,item.model_copy(update={'resource_ids':[self.target.id]}))
        self.worker=self.workers.ensure_local_worker(service_identity_id=self.worker_actor.identity_id,
            version=self.worker.version,capabilities=(WorkerCapability.GIT,WorkerCapability.COMMAND_EXECUTION,
                WorkerCapability.NETWORK,WorkerCapability.ARTIFACT_UPLOAD),actor=self.admin,
            supported_execution_contract_versions=('1.0','thread-bootstrap/1.0','thread-bootstrap/1.1',
                'thread-turn/1.0','thread-turn/1.1'),max_concurrency=1)
        binding_service=TurnExecutionBindingService(self.configuration,binding_fixture._Projects(project),self.resources,
            self.workspaces,self.workers,control_actor=self.admin,
            runtime_binding=ExecutionRuntimeBinding(provider_id='openai',runtime_id='codex',capability_revision=1),
            execution_profiles=self.profiles)
        binding_service.resource_context_admission=ReadOnlyResourceContextAdmission(self.identity,self.authority,
            self.resources,self.items.get,self.agent_profiles)
        self.workers.resource_context_validator=binding_service.validate_assignment_resource_context
        f=SimpleNamespace(project=project,projects=binding_fixture._Projects(project),repository=self.target,database=self.database,
            workers=self.workers,workspaces=self.workspaces,actor=self.admin,identity=self.identity,
            temp=self.temp,sqlite=self.sqlite,worker=self.worker,service=binding_service)
        bindings=ThreadBootstrapBindingService(ThreadBootstrapBindingStore(self.sqlite));bindings.assignment_store=self.workers.store
        self.requester=create_requester(self,self.identity,self.registry,self.admin,project,(self.target.id,self.database.id))
        # The inherited profile intentionally requires local-admin for the
        # older fixture. A is not that principal. Publish a private, explicitly
        # A-allowlisted invocation policy instead of elevating A or spoofing C.
        inherited_pin=self.assignment.agent_profile
        roles_before=self.authority.role_ids_for_actor(self.requester,project_id=project.id)
        self.assertIn('private-context-requester',roles_before)
        self.assertNotIn('local-admin',roles_before)
        with self.assertRaises(AgentProfileAccessDenied):
            self.agent_profiles.resolve_for_execution(inherited_pin.profile_id,
                actor=self.requester,project_id=project.id)
        profile=self.agent_profiles.update(inherited_pin.profile_id,AgentProfileUpdate(
            access=AgentProfileAccessPolicy(mode='allowlist',identity_ids=(self.requester.identity_id,)),
            authority_role_id=None,authority_definition_ref=None,
            reason='Private bounded requester invocation; retain independent worker authority'),actor=self.admin)
        resolved,decision=self.agent_profiles.resolve_for_execution(profile.profile_id,
            actor=self.requester,project_id=project.id)
        self.assertTrue(decision.allowed)
        self.assertEqual(resolved.record_id,profile.record_id)
        self.assertEqual(self.authority.role_ids_for_actor(self.requester,project_id=project.id),roles_before)
        self.assertEqual(profile.access.identity_ids,(self.requester.identity_id,))
        self.assertIsNone(profile.authority_role_id)
        self.assertGreater(profile.revision,inherited_pin.profile_revision)
        self.profile_pin=AgentProfileExecutionBinding(profile_id=profile.profile_id,
            profile_revision=profile.revision,profile_record_id=profile.record_id,
            role_id=profile.role_id,role_definition_ref=profile.role_definition_ref,
            authority_role_id=profile.authority_role_id,authority_definition_ref=profile.authority_definition_ref,
            execution_profile_id=profile.execution_profile_id)
        self.assertEqual(self.items.get(self.ref).current_owner,self.profile_pin.profile_id)
        # Retain a real repo-only same-thread binding to be normally superseded;
        # no assignment containing the DB is prebuilt for the positive path.
        # Harness publishes a real private credential reference first.
        self.harness=NativeContextHarness(self,f,items=self.items,profiles=self.agent_profiles,pin=self.profile_pin,
            bindings=bindings,requester=self.requester,worker_actor=self.worker_actor,ref=self.ref)
        prior=binding_service.prepare_bootstrap(bootstrap_id='capacity-repo-only',execution_id='capacity-repo-only-exec',
            project_id=project.id,sandbox='workspace-write',approval_policy='on-request',
            explicit_repository_id=self.target.id,agent_profile=self.profile_pin)
        bindings.bind(bootstrap_id='capacity-repo-only',thread_id='same-thread',execution_id=prior.execution_id,
            assignment_id=prior.assignment_id,execution_workspace_id=prior.workspace_id,actor=self.admin)
        self.workers.cancel_bootstrap(prior.assignment_id,AssignmentCancelRequest(expected_fence=0,
            reason='Private terminal repo-only bootstrap'),actor=self.admin)
        self.assignment,self.native_session=await self.harness.finish_one()
        self.current_assignment=self.assignment
        self.service.execution_workspaces=self.workspaces
        self.intents.execution.secret_broker=self.harness.secrets
        # Use the actual native validator and the SAME canonical credential
        # store read set, not a caller-selected or foreign metadata facsimile.
        self.broker=AssignmentBoundControlPlaneBroker(self.service,assignment=self.assignment,worker_id=self.worker.id,
            worker_actor=self.worker_actor,fence=self.assignment.fence,validator=self.native_session.validate_current)
        await self.broker.start()
        status,_,body=await self._request('POST','/api/repository-workspace/publication-recovery',
            payload={'work_item_ref':self.ref})
        self.assertEqual(status,200,body)
        self.artifact=self.evidence.create_artifact(ArtifactCreate(artifact_type='report',
            name='Original synthetic capacity observation',project_id=project.id,work_item_ref=self.ref,
            resource_ids=(self.database.id,)),actor=self.requester)
        self.original=self.evidence.create_evidence(EvidenceCreate(work_item_ref=self.ref,project_id=project.id,
            resource_ids=(self.database.id,),artifact_ids=(self.artifact.id,),evidence_type='artifact_verification',
            result='pass',observed_at=time.time(),metadata=self.capacity_metadata),actor=self.requester)
        self.request=dict(self.request,evidence_ids=[self.original.id],
            source='sha256:'+hashlib.sha256(b'actual admitted context independent B review').hexdigest())
        self.assertNotIn(self.database.id,self.items.get(self.ref).resource_ids)
        self.assertEqual(len({self.admin.identity_id,self.requester.identity_id,self.worker_actor.identity_id}),3)
        self.assertEqual(self.assignment.resource_context.requester_identity_id,self.requester.identity_id)

    def writer_store(self):return self.sqlite

    async def assert_no_append(self):
        before=self.evidence.store.load().verifications
        status,_,body=await self.post()
        self.assertEqual(status,403,body)
        self.assertEqual(self.evidence.store.load().verifications,before)
        self.assertEqual(self.evidence.get_evidence(self.original.id,actor=self.admin),self.original)
        self.assertEqual(self.evidence.get_artifact(self.artifact.id,actor=self.admin),self.artifact)
        self.assertEqual(self.recovery.store.load(),self.original_state)

    async def test_real_admitted_context_appends_original_independent_verification(self):
        status,_,body=await self.post()
        self.assertEqual(status,200,body)
        record=body['item']
        self.assertTrue(record['independent'])
        self.assertEqual(record['verifier_identity_id'],self.worker_actor.identity_id)
        self.assertEqual(body['attribution']['authorizer_identity_id'],self.requester.identity_id)
        self.assertEqual(record['evidence_ids'],[self.original.id])
        self.assertEqual(record['artifact_ids'],[self.artifact.id])
        self.assertNotIn(self.database.id,self.items.get(self.ref).resource_ids)
        self.assertEqual(self.evidence.get_evidence(self.original.id,actor=self.admin),self.original)
        self.assertEqual(self.evidence.get_artifact(self.artifact.id,actor=self.admin),self.artifact)
        self.assertEqual(self.recovery.store.load(),self.original_state)

    async def revoke_at_append(self,change):
        original=self.evidence.create_verification;observed=[]
        def hook(payload,*,actor,_commit=None):
            # The adapter already observed the original proof and calls the
            # real ArtifactEvidence append. Commit a canonical separate writer
            # immediately before the actual shared transaction acquires locks.
            observed.append(payload.evidence_ids)
            def commit(updater):
                change(self.writer_store())
                return _commit(updater)
            return original(payload,actor=actor,_commit=commit)
        with patch.object(self.evidence,'create_verification',side_effect=hook):
            await self.assert_no_append()
        self.assertEqual(observed,[(self.original.id,)])

    async def test_project_binding_revoked_after_observation_no_append(self):
        from codex_web.storage.resource_catalog import ResourceCatalogStore
        def revoke(store):
            def apply(state):
                state.project_bindings=[b for b in state.project_bindings if b.resource_id!=self.database.id]
                return state
            ResourceCatalogStore(store).update(apply)
        await self.revoke_at_append(revoke)

    async def test_original_requester_read_revoked_after_observation_no_append(self):
        from codex_web.authority import AUTHORITY_ROLE_CATALOG_ID,AUTHORITY_ROLE_CATALOG_KIND,AuthorityRoleCatalogDefinition
        from codex_web.definitions import DefinitionDraftCreate,DefinitionPublishRequest
        from codex_web.services.definitions import DefinitionRegistryService
        from codex_web.storage.definition_registry import DefinitionRegistryStore
        def revoke(store):
            registry=DefinitionRegistryService(DefinitionRegistryStore(store),
                schemas=self.registry.schemas,engine_version=self.registry.engine_version)
            active=registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID,kind=AUTHORITY_ROLE_CATALOG_KIND)
            catalog=AuthorityRoleCatalogDefinition.model_validate(active.payload)
            roles=tuple(role.model_copy(update={'grants':tuple(g for g in role.grants if g.capability!='resource.read')})
                if role.id=='private-context-requester' else role for role in catalog.roles)
            draft=registry.create_draft(DefinitionDraftCreate(definition_id=AUTHORITY_ROLE_CATALOG_ID,
                kind=AUTHORITY_ROLE_CATALOG_KIND,definition_schema_version='1.0',
                payload=catalog.model_copy(update={'roles':roles}).model_dump(mode='json'),actor=self.admin.identity_id,
                reason='Private current READ revocation'))
            registry.approve_publication(draft.record_id,actor='fixture-approver',reference='synthetic-only',reason='Private lifecycle')
            registry.publish(draft.record_id,DefinitionPublishRequest(actor=self.admin.identity_id,
                reason='Private current READ revocation',expected_active_revision=active.revision))
        await self.revoke_at_append(revoke)

    async def test_current_work_item_owner_revoked_after_observation_no_append(self):
        from codex_web.storage.runtime_state import ModelMapRepository
        from codex_web.models import WorkItemState
        def revoke(store):
            items=ModelMapRepository(store,namespace=self.items.namespace,
                legacy_path=Path(self.temp.name)/'never-created-mirror.json',model=WorkItemState)
            item=items.get(self.ref);items.put(self.ref,item.model_copy(update={'current_owner':'another-owner'}))
        await self.revoke_at_append(revoke)

    async def test_current_read_lease_mode_drift_after_observation_no_append(self):
        from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
        workspace=self.workspaces.get(self.assignment.execution_workspace_id,self.admin)
        def revoke(store):
            def apply(state):
                state.leases=[lease.model_copy(update={'resource_modes':{**lease.resource_modes,self.database.id:'write'}})
                    if lease.id==workspace.lease_id else lease for lease in state.leases]
                return state
            ExecutionWorkspaceStateStore(store).update(apply)
        await self.revoke_at_append(revoke)

    async def test_workspace_context_mismatch_after_observation_no_append(self):
        from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
        workspace_id=self.assignment.execution_workspace_id
        def revoke(store):
            def apply(state):
                state.workspaces=[workspace.model_copy(update={'resource_context':
                    workspace.resource_context.model_copy(update={'work_item_ref':'unrelated/repo#1'})})
                    if workspace.id==workspace_id else workspace for workspace in state.workspaces]
                return state
            ExecutionWorkspaceStateStore(store).update(apply)
        await self.revoke_at_append(revoke)

    async def test_exact_fence_cancellation_after_observation_no_append(self):
        await self.revoke_at_append(lambda store:self.workers.cancel_bootstrap(self.assignment.id,
            AssignmentCancelRequest(expected_fence=self.assignment.fence,reason='Private current fence revocation'),actor=self.admin))

    async def test_mismatched_reference_cannot_use_admitted_context(self):
        self.request=dict(self.request,work_item_ref='unrelated/repo#1')
        await self.assert_no_append()


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'),'isolated PostgreSQL DSN required')
class PostgresAdmittedContextCapacityTests(_PostgresFixture,AdmittedContextCapacityTests):
    async def asyncSetUp(self):
        from tests import test_control_plane_broker as broker_fixtures
        with patch.object(broker_fixtures,'SQLiteStateStore',self.state_store_factory):
            await super().asyncSetUp()

    def writer_store(self):
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        dsn=os.environ['CODEX_WEB_TEST_POSTGRES_DSN']
        pool=ConnectionPool(dsn,min_size=1,max_size=1,timeout=3,
            kwargs={'options':f'-c search_path={self._test_schema}'})
        self.addCleanup(pool.close)
        store=PostgresStateStore(dsn,connection_pool=pool);self.addCleanup(store.close)
        self.assertIsNot(store._pool,self.sqlite._pool)
        with store._read_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT current_schema()')
                self.assertEqual(cursor.fetchone()[0],self._test_schema)
        return store
