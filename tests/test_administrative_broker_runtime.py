"""Canonical administrative execution qualification with a synthetic provider.

The provider has no external effects; assignment/profile/intent state and receipts
use the production services. Original-work live acceptance remains separate.
"""
from copy import deepcopy
import json
import subprocess
import sys
from unittest.mock import patch
import unittest

from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.agent_profiles import AgentProfileCreate, AgentProfileExecutionBinding, AgentProfileUpdate, AgentProfileLifecycle
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.execution_workers import (ExecutionWorkerRegister, ExecutionAssignmentCreate,
    AssignmentClaimRequest, AssignmentStartRequest, WorkerCapability)
from codex_web.resources import ResourceCreate, ResourceType
from codex_web.services.action_intents import ActionIntentService
from codex_web.services.action_providers import ActionExecutionService, ActionProviderRegistry
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from codex_web.services.execution_role_definitions import install_execution_role_definitions
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.reference_action_provider import ReferenceActionProvider
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.storage.action_intents import ActionIntentStore
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from tests import test_control_plane_broker as broker_fixture


class _AdministrativeProvider(ReferenceActionProvider):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.unknown = False

    def actions(self):
        return (super().actions()[0].model_copy(update={
            'action_id': 'code-host.issue.create',
            'required_resource_types': (ResourceType.REPOSITORY,),
            'required_authority': ('repository.issue.create',),
        }),)

    async def execute(self, request, **kwargs):
        self.calls += 1
        result = await super().execute(request, **kwargs)
        if self.unknown:
            raise RuntimeError('Synthetic response lost after provider write')
        return result


class AdministrativeBrokerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only transport/identity fixture helpers, not inherited test cases.
    _assignment = staticmethod(broker_fixture.ControlPlaneBrokerTests._assignment)
    _publish_worker_authority = broker_fixture.ControlPlaneBrokerTests._publish_worker_authority
    _request = broker_fixture.ControlPlaneBrokerTests._request
    asyncTearDown = broker_fixture.ControlPlaneBrokerTests.asyncTearDown

    async def asyncSetUp(self):
        await broker_fixture.ControlPlaneBrokerTests.asyncSetUp(self)
        await self.broker.stop()
        self.admin = self.identity.local_trusted_actor()
        self.resources = ResourceCatalogService(ResourceCatalogStore(self.sqlite))
        self.target = self.resources.create(ResourceCreate(name='Fixture prerequisite repository',
            resource_type=ResourceType.REPOSITORY), actor=self.admin)
        self.authority.resources = self.resources
        self.profiles = install_execution_profile_definitions(self.registry)
        # This fixture resolves its own catalog and must not replace the
        # process-wide provider used by application and later test modules.
        with patch('codex_web.services.execution_role_definitions.install_execution_role_catalog_provider'):
            self.roles = install_execution_role_definitions(self.registry)
        payload = execution_profile_catalog_seed_payload()
        payload['profiles'].append({'id':'orchestration-admin','name':'Fixture administration',
            'description':'Synthetic qualification only','workspace_mode':'scratch',
            'repository_access':'none','required_worker_capabilities':['command_execution'],
            'control_plane_operations':['repository.issue.create']})
        # Reusable profile selection has a global template without authority.
        # Only the exact Project override supplies administrative target scope.
        self._publish_profile(payload, '1.0')
        project_payload = deepcopy(payload)
        project_payload['profiles'][-1]['administrative_repository_ids'] = [self.target.id]
        self._publish_profile(project_payload, '1.1', project='project-a')
        self.agent_profiles = AgentProfileService(AgentProfileStore(self.sqlite),
            definitions=self.registry, authority=self.authority, execution_profiles=self.profiles,
            role_resolver=lambda role, actor:self.roles.reference(
                organization_id=actor.organization_id,workspace_id=actor.workspace_id))
        self.profile = self.agent_profiles.create(AgentProfileCreate(profile_id='orchestrator',
            name='Fixture Orchestrator',role_id='orchestrator',authority_role_id='local-admin',
            execution_profile_id='orchestration-admin'),actor=self.admin)
        self.profiles.agent_profiles = self.agent_profiles
        self.workers = ExecutionWorkerService(ExecutionWorkerStore(self.sqlite),identity=self.identity)
        self.worker = self.workers.register(ExecutionWorkerRegister(
            service_identity_id=self.worker_actor.identity_id,pool='fixture',version='1.0',
            capabilities=(WorkerCapability.COMMAND_EXECUTION,),supported_execution_contract_versions=('thread-bootstrap/1.0',),max_concurrency=1),actor=self.admin)
        _, definition = self.profiles.resolve('orchestration-admin',project_id='project-a')
        binding = AgentProfileExecutionBinding(profile_id=self.profile.profile_id,
            profile_revision=self.profile.revision,profile_record_id=self.profile.record_id,
            role_id=self.profile.role_id,authority_role_id=self.profile.authority_role_id,
            execution_profile_id='orchestration-admin',authority_definition_ref=self.profile.authority_definition_ref)
        assignment = self.workers.create_assignment(ExecutionAssignmentCreate(
            subject=self.assignment.subject,execution_id=self.assignment.execution_id,
            project_id='project-a',resource_ids=(),execution_contract_version='thread-bootstrap/1.0',
            required_capabilities=(WorkerCapability.COMMAND_EXECUTION,),sandbox='workspace-write',
            approval_policy='on-request',execution_profile_id='orchestration-admin',
            execution_profile_definition=definition,agent_profile=binding),actor=self.admin)
        claimed = self.workers.claim(self.worker.id,AssignmentClaimRequest(),
            actor=self.worker_actor,assignment_id=assignment.id)
        self.current_assignment = self.workers.start(self.worker.id,claimed.id,AssignmentStartRequest(
            fence=claimed.fence,lease_token=claimed.lease.lease_token),actor=self.worker_actor)
        self.assignment = self.current_assignment
        self.provider = _AdministrativeProvider()
        action_registry = ActionProviderRegistry(ActionProviderStateStore(self.sqlite))
        action_registry.register(self.provider)
        action_registry.bind(ActionProviderBindingCreate(provider_type=self.provider.provider_type,
            provider_instance=self.provider.provider_instance,project_id='project-a',
            resource_ids=(self.target.id,)),actor=self.admin,resources=self.resources)
        self.intents = ActionIntentService(ActionIntentStore(self.sqlite),
            ActionExecutionService(action_registry,self.resources),authority=self.authority,
            identity=self.identity,execution_workers=self.workers,execution_profiles=self.profiles)
        self.service.execution_profiles = self.profiles
        self.service.action_intents = self.intents
        self.broker = AssignmentBoundControlPlaneBroker(self.service,assignment=self.assignment,
            worker_id=self.worker.id,worker_actor=self.worker_actor,fence=self.assignment.fence,
            validator=lambda:self.workers.assignment_for_execution(self.assignment.execution_id,actor=self.admin))
        await self.broker.start()

    def _publish_profile(self,payload,version,project=None):
        record = self.registry.create_draft(DefinitionDraftCreate(kind='execution-profile-catalog',
            definition_id='execution.profiles.default',definition_schema_version=version,
            payload=payload,scope_type='project' if project else 'global',scope_id=project,actor='fixture'))
        self.registry.publish(record.record_id,DefinitionPublishRequest(actor='fixture'))
        return record

    async def submit(self,key='fixture-prerequisite'):
        return await self._request('POST','/api/repository-actions/issue/create?repository_resource_id='+self.target.id,
            payload={'parameters':{'key':'prerequisite','value':'fixture issue'},'idempotency_key':key})

    async def test_canonical_receipt_verification_and_idempotent_reopen(self):
        status,_,body = await self.submit()
        self.assertEqual(status,200,body)
        intent = body['item']
        self.assertEqual(intent['status'],'succeeded')
        self.assertEqual(intent['execution_id'],self.assignment.execution_id)
        history = self.intents.history(intent['id'],self.admin)
        self.assertEqual(len(history['receipts']),1)
        self.assertEqual(len(history['verifications']),1)
        self.assertTrue(history['verifications'][0]['verified'])
        self.assertEqual(self.provider.calls,1)
        self.assertEqual(intent['requested_by'], self.admin.identity_id)
        self.assertEqual(intent['request']['resource_ids'], [self.target.id])
        self.assertNotIn(self.assignment.lease.lease_token, json.dumps(history))
        self.assertIsNone(self.assignment.repository_scope)
        self.assertEqual(self.assignment.resource_ids,())
        status,_,duplicate = await self.submit()
        self.assertEqual(status,200,duplicate)
        self.assertEqual(duplicate['item']['id'],intent['id'])
        self.assertEqual(self.provider.calls,1)
        reopened = ActionIntentStore(self.sqlite).get(intent['id'])
        self.assertEqual(reopened.last_receipt_id,intent['last_receipt_id'])

    async def test_profile_changed_after_persistence_prevents_provider_execution(self):
        original = self.intents.create
        def create_then_revoke(*args,**kwargs):
            intent = original(*args,**kwargs)
            self.agent_profiles.update('orchestrator',AgentProfileUpdate(
                description='Changed after intent creation',reason='synthetic execution-boundary change'),actor=self.admin)
            return intent
        self.intents.create = create_then_revoke
        status,_,body = await self.submit()
        self.assertEqual(status,200,body)
        self.assertEqual(body['item']['status'],'cancelled')
        self.assertEqual(self.provider.calls,0)
        history = self.intents.history(body['item']['id'],self.admin)
        self.assertEqual(history['receipts'],[])
        self.assertEqual(history['verifications'],[])


    async def _deny_after_assignment_change(self, change):
        original = self.intents.create
        def create_then_change(*args, **kwargs):
            intent = original(*args, **kwargs)
            def apply(state):
                state.assignments = [change(item) if item.id == self.assignment.id else item
                                     for item in state.assignments]
                return state
            self.workers.store.update(apply)
            return intent
        self.intents.create = create_then_change
        status, _, body = await self.submit()
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['status'], 'cancelled')
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.intents.history(body['item']['id'], self.admin)['receipts'], [])

    async def test_expired_canonical_lease_after_creation_blocks_execution(self):
        await self._deny_after_assignment_change(lambda item:item.model_copy(update={
            'lease':item.lease.model_copy(update={'expires_at':0})}))

    async def test_changed_canonical_fence_after_creation_blocks_execution(self):
        await self._deny_after_assignment_change(lambda item:item.model_copy(update={'fence':item.fence+1}))

    async def test_expired_assignment_deadline_after_creation_blocks_execution(self):
        await self._deny_after_assignment_change(lambda item:item.model_copy(update={'deadline_at':0}))

    async def test_unknown_provider_outcome_survives_reopen_without_replay(self):
        self.provider.unknown = True
        status, _, body = await self.submit()
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['status'], 'uncertain')
        self.assertEqual(self.provider.values['prerequisite'], 'fixture issue')
        self.assertEqual(self.provider.calls, 1)
        before = self.intents.history(body['item']['id'], self.admin)
        self.assertEqual(len(before['receipts']), 1)
        self.assertEqual(before['receipts'][0]['outcome'], 'unknown')
        self.intents = ActionIntentService(ActionIntentStore(self.sqlite), self.intents.execution,
            authority=self.authority, identity=self.identity, execution_workers=self.workers,
            execution_profiles=self.profiles)
        self.service.action_intents = self.intents
        status, _, duplicate = await self.submit()
        self.assertEqual(status, 200, duplicate)
        self.assertEqual(duplicate['item']['id'], body['item']['id'])
        self.assertEqual(duplicate['item']['status'], 'uncertain')
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.intents.history(body['item']['id'], self.admin), before)

    async def test_broker_replacement_retains_unknown_outcome_without_provider_replay(self):
        self.provider.unknown = True
        status, _, body = await self.submit()
        self.assertEqual(status, 200, body)
        intent_id = body['item']['id']
        self.assertEqual(body['item']['status'], 'uncertain')
        history = self.intents.history(intent_id, self.admin)
        await self.broker.stop()
        self.intents = ActionIntentService(ActionIntentStore(self.sqlite), self.intents.execution,
            authority=self.authority, identity=self.identity, execution_workers=self.workers,
            execution_profiles=self.profiles)
        self.service.action_intents = self.intents
        self.broker = AssignmentBoundControlPlaneBroker(self.service, assignment=self.assignment,
            worker_id=self.worker.id, worker_actor=self.worker_actor, fence=self.assignment.fence,
            validator=lambda:self.workers.assignment_for_execution(self.assignment.execution_id, actor=self.admin))
        await self.broker.start()
        status, _, duplicate = await self.submit()
        self.assertEqual(status, 200, duplicate)
        self.assertEqual(duplicate['item']['id'], intent_id)
        self.assertEqual(duplicate['item']['status'], 'uncertain')
        self.assertEqual(self.intents.history(intent_id, self.admin), history)
        self.assertEqual(self.provider.calls, 1)

    async def test_role_binding_revoked_after_creation_blocks_execution(self):
        original = self.intents.create
        def create_then_revoke(*args, **kwargs):
            intent = original(*args, **kwargs)
            fixture = broker_fixture
            active = self.registry.resolve(definition_id=fixture.AUTHORITY_ROLE_CATALOG_ID,
                                           kind=fixture.AUTHORITY_ROLE_CATALOG_KIND)
            catalog = fixture.AuthorityRoleCatalogDefinition.model_validate(active.payload)
            revoked = catalog.model_copy(update={'bindings':tuple(
                binding for binding in catalog.bindings if binding.role_id != 'local-admin')})
            record = self.registry.create_draft(DefinitionDraftCreate(
                definition_id=active.definition_id, kind=active.kind,
                definition_schema_version=active.definition_schema_version,
                payload=revoked.model_dump(mode='json'), actor='synthetic-fixture'))
            self.registry.approve_publication(record.record_id, actor='synthetic-fixture-reviewer',
                reference='SYNTHETIC-QUALIFICATION', reason='Test execution-time revocation')
            self.registry.publish(record.record_id, DefinitionPublishRequest(actor='synthetic-fixture',
                expected_active_revision=active.revision))
            return intent
        self.intents.create = create_then_revoke
        status, _, body = await self.submit()
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['status'], 'cancelled')
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.intents.history(body['item']['id'], self.admin)['receipts'], [])


    async def test_foreign_resource_is_denied_without_intent_or_provider_call(self):
        foreign = self.resources.create(ResourceCreate(name='Undeclared fixture repository',
            resource_type=ResourceType.REPOSITORY), actor=self.admin)
        status, _, body = await self._request('POST',
            '/api/repository-actions/issue/create?repository_resource_id='+foreign.id,
            payload={'parameters':{'key':'foreign','value':'forbidden'}})
        self.assertEqual(status, 403, body)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.intents.store.load().intents, [])

    async def test_changed_project_after_creation_blocks_execution(self):
        await self._deny_after_assignment_change(lambda item:item.model_copy(update={'project_id':'project-b'}))

    async def test_superseded_project_definition_after_creation_blocks_execution(self):
        original = self.intents.create
        def create_then_supersede(*args, **kwargs):
            intent = original(*args, **kwargs)
            active = self.profiles.record(project_id='project-a')
            self._publish_profile(deepcopy(active.payload), '1.1', project='project-a')
            return intent
        self.intents.create = create_then_supersede
        status, _, body = await self.submit()
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['status'], 'cancelled')
        self.assertEqual(self.provider.calls, 0)

    async def test_global_template_cannot_supply_administrative_targets(self):
        template, _ = self.profiles.resolve('orchestration-admin')
        self.assertEqual(template.administrative_repository_ids, ())
        assignment = self.assignment.model_copy(update={'project_id':'project-b'})
        with self.assertRaises(ValueError):
            self.profiles.authorize_administrative_target(assignment,
                operation_id='repository.issue.create',repository_id=self.target.id,
                actor=self.admin,authority=self.authority)


    async def test_cross_organization_after_creation_blocks_execution(self):
        await self._deny_after_assignment_change(lambda item:item.model_copy(update={'organization_id':'foreign'}))

    async def test_cross_workspace_after_creation_blocks_execution(self):
        await self._deny_after_assignment_change(lambda item:item.model_copy(update={'workspace_id':'foreign'}))

    async def test_disabled_persisted_profile_denies_admission(self):
        # Fault injection represents externally revoked/corrupt configuration;
        # it does not weaken the normal lifecycle API's consumer safeguards.
        self.agent_profiles.store.append(self.profile.model_copy(update={
            'revision':self.profile.revision+1,'record_id':self.profile.record_id+'-fault',
            'lifecycle':AgentProfileLifecycle.DISABLED,'updated_by':'synthetic-fault-injection'}))
        status, _, body = await self.submit()
        self.assertEqual(status, 403, body)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.intents.store.load().intents, [])

    async def test_unsupported_executable_operation_is_denied(self):
        status, _, body = await self._request('POST',
            '/api/repository-actions/branch/publish?repository_resource_id='+self.target.id,
            payload={'parameters':{'key':'execution','value':'forbidden'}})
        self.assertEqual(status, 403, body)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.intents.store.load().intents, [])


    async def test_missing_profile_denies_admission_without_intent(self):
        with patch.object(self.agent_profiles.store, 'latest', return_value=None):
            status, _, body = await self.submit()
        self.assertEqual(status, 403, body)
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(self.intents.store.load().intents, [])


    async def test_unknown_receipt_is_readable_from_a_fresh_interpreter(self):
        self.provider.unknown = True
        status, _, body = await self.submit()
        self.assertEqual(status, 200, body)
        self.assertEqual(body['item']['status'], 'uncertain')
        before = self.intents.history(body['item']['id'], self.admin)
        script = """
import json, sys
from pathlib import Path
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.action_intents import ActionIntentStore
state = ActionIntentStore(SQLiteStateStore(Path(sys.argv[1]))).load(intent_id=sys.argv[2])
intent = next(item for item in state.intents if item.id == sys.argv[2])
print(json.dumps({
    'intent': intent.model_dump(mode='json'),
    'receipts': [item.model_dump(mode='json') for item in state.receipts if item.intent_id == intent.id],
    'verifications': [item.model_dump(mode='json') for item in state.verifications if item.intent_id == intent.id],
    'inbox': [item.model_dump(mode='json') for item in state.inbox if item.intent_id == intent.id],
}))
"""
        child = subprocess.run([sys.executable, '-c', script, str(self.sqlite.path), body['item']['id']],
                               check=False, capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout), before)
        self.assertEqual(self.provider.calls, 1)
