"""Composed canonical admission, registry, credential and provider reads."""
import json
from types import SimpleNamespace
import unittest
import httpx

from codex_web.action_providers import ActionProviderBindingCreate
from codex_web.authority import AUTHORITY_ROLE_CATALOG_ID, AUTHORITY_ROLE_CATALOG_KIND
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.resources import RepositoryExecutionScope, RepositoryTargetSource
from codex_web.services.action_providers import ActionProviderRegistry
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.control_plane_broker import (
    AssignmentBoundControlPlaneBroker, ControlPlaneBrokerService,
    ControlPlaneBrokerDeniedError, ControlPlaneBrokerRequestError,
)
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from codex_web.services.github_action_provider import GitHubActionProvider
from codex_web.services.secrets import SecretUseDeniedError
from codex_web.storage.action_providers import ActionProviderStateStore
from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.execution_workers import ExecutionWorker
from tests import test_code_host_merge_reads as service_fixtures
from tests import test_control_plane_broker as broker_fixtures


OPERATION = 'repository.read.merge-requirements'
PATH = '/api/repository-facts/pull-requests/7/merge-requirements'
CAPABILITY = 'repository.pull-request.merge-requirements.read'


class BrokerMergeRequirementsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reuse setup only, not an inherited duplicate of the service suite.
        self.env = service_fixtures.MergeRequirementServiceTests()
        await self.env.asyncSetUp()
        self.addCleanup(self.env.doCleanups)
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(self.env.state))
        self.authority = install_authority_roles(self.registry)
        self.authority.resources = self.env.resources
        self.publish_authority()
        self.profiles = install_execution_profile_definitions(self.registry)
        self.reference = self.publish_profile()
        self.actions = ActionProviderRegistry(ActionProviderStateStore(self.env.state))
        self.actions.register(GitHubActionProvider(self.env.resources,
            provider_instance='github.example', api_base='https://api.github.example', web_base='https://github.example'))
        self.binding_payload = ActionProviderBindingCreate(provider_type='github', provider_instance='github.example',
            project_id='project-a', resource_ids=(self.env.resource.id,), credential_ref=self.env.reference.id)
        self.binding = self.actions.bind(self.binding_payload, actor=self.env.admin, resources=self.env.resources)
        self.service = ControlPlaneBrokerService(identity=self.env.identity, authority=self.authority,
            work_items=None, operator=object(), audit=ControlPlaneBrokerAuditStore(self.env.state),
            # This read uses the real registry only; no ActionIntent is created.
            action_intents=SimpleNamespace(execution=SimpleNamespace(registry=self.actions)),
            code_hosts=self.env.service, execution_profiles=self.profiles)
        self.assignment = broker_fixtures.ControlPlaneBrokerTests._assignment().model_copy(update={
            'execution_profile_id': 'repository-write', 'execution_profile_definition': self.reference,
            'resource_ids': (self.env.resource.id,),
            'repository_scope': RepositoryExecutionScope(organization_id='local', workspace_id='default',
                project_id='project-a', writable_repository_ids=(self.env.resource.id,),
                source=RepositoryTargetSource.SINGLE_REPOSITORY, source_ref=self.env.resource.id),
        })
        self.current = self.assignment

    def install_assignment_store(self):
        store = ExecutionWorkerStore(self.env.state)
        worker = ExecutionWorker(id='worker-1', organization_id='local', workspace_id='default',
            service_identity_id=self.env.worker.identity_id, version='fixture',
            capabilities=('command_execution',), registered_by=self.env.admin.identity_id)
        def seed(state):
            state.workers.append(worker)
            state.assignments.append(self.assignment)
            return state
        store.update(seed)
        self.service.execution_workers = ExecutionWorkerService(store, identity=self.env.identity)
        return store

    def publish(self, kind, definition_id, payload, version='1.0'):
        draft = self.registry.create_draft(DefinitionDraftCreate(kind=kind, definition_id=definition_id,
            definition_schema_version=version, payload=payload, actor='fixture'))
        self.registry.approve_publication(draft.record_id, actor='fixture-reviewer',
            reference='PRIVATE-TEST', reason='private canonical read fixture')
        return self.registry.publish(draft.record_id, DefinitionPublishRequest(actor='fixture'))

    def publish_authority(self, *, worker=True, requester=True):
        # Explicit independent grants: the requester has no wildcard Role.
        roles, bindings = [], []
        for principal, identity_id, allowed in (
            ('worker', self.env.worker.identity_id, worker), ('requester', self.env.admin.identity_id, requester)):
            roles.append(dict(id=f'merge-{principal}', name=principal, description='Private fixture',
                grants=[dict(id=f'merge.{principal}.read', capability=CAPABILITY, level='read',
                    project_ids=['project-a'], resource_ids=[self.env.resource.id])] if allowed else []))
            bindings.append(dict(id=f'merge-{principal}-binding', role_id=f'merge-{principal}',
                subject_kind='identity', subject_id=identity_id, organization_id='local',
                workspace_id='default', project_ids=['project-a']))
        self.publish(AUTHORITY_ROLE_CATALOG_KIND, AUTHORITY_ROLE_CATALOG_ID,
                     dict(roles=roles, bindings=bindings))

    def publish_profile(self, *, declared=True):
        payload = execution_profile_catalog_seed_payload()
        profile = next(item for item in payload['profiles'] if item['id'] == 'repository-write')
        if declared:
            profile['control_plane_operations'].append(OPERATION)
        self.publish('execution-profile-catalog', 'execution.profiles.default', payload)
        return self.profiles.resolve('repository-write', organization_id='local',
            workspace_id='default', project_id='project-a')[1]

    async def read(self, path=PATH, body=b'', validator=True):
        return await self.service.dispatch(assignment=self.assignment, worker_actor=self.env.worker,
            method='GET', raw_target=path, body=body,
            assignment_validator=(lambda: self.current) if validator else None)

    def change_binding(self, **values):
        def apply(state):
            state.bindings = [item.model_copy(update=values) if item.id == self.binding.id else item
                              for item in state.bindings]
            return state
        self.actions.store.update(apply)

    async def test_complete_read_uses_real_scope_roles_definition_registry_and_secret(self):
        status, result, *_ = await self.read()
        self.assertEqual(status, 200)
        self.assertTrue(result['item']['complete'])
        self.assertEqual(result['scope']['requester_identity_id'], self.env.admin.identity_id)
        self.assertEqual(result['scope']['inspecting_worker_identity_id'], self.env.worker.identity_id)
        self.assertEqual(result['scope']['definition'], self.reference.model_dump(mode='json'))
        self.assertNotIn('fixture-token', json.dumps(result))
        self.assertNotIn('lease-token-that-is-never-exposed', json.dumps(result))

    async def test_composed_effective_rules_require_reviews_conversations_and_attributed_checks(self):
        self.env.server.rules = [self.env.server.rule()]
        self.env.server.reviews = [self.env.server.review(1)]
        self.env.server.threads[0]['isResolved'] = False
        self.env.server.runs[0]['app']['id'] = 99
        _, result, *_ = await self.read()
        fact = result['item']
        self.assertTrue(fact['complete'])
        self.assertEqual(fact['reviews']['approving_count'], 2)
        self.assertTrue(fact['reviews']['code_owner_required'])
        self.assertTrue(fact['reviews']['last_push_approval_required'])
        self.assertEqual(fact['independent_approval_count'], 1)
        self.assertEqual(fact['unresolved_conversation_count'], 1)
        self.assertIn('required_checks_unmet_or_ambiguous', fact['blockers'])
        self.assertTrue(all(request.method == 'GET' or request.url.path == '/graphql'
                            for request in self.env.server.calls))
        self.assertTrue(all('mutation' not in request.content.decode().lower()
                            for request in self.env.server.calls))

    async def test_binding_policy_and_credential_reference_changes_cannot_return_old_facts(self):
        from codex_web.code_hosts import CodeHostError
        changes = ({'security_policy': self.binding.security_policy.model_copy(update={'sandbox': 'read-only'})},
                   {'credential_ref': 'another-reference'})
        for values in changes:
            with self.subTest(fields=tuple(values)):
                self.change_binding(**self.binding.model_dump(exclude={'id', 'created_at', 'updated_at'}))
                self.env.during_status(lambda values=values: self.change_binding(**values))
                with self.assertRaises((ControlPlaneBrokerDeniedError, CodeHostError)):
                    await self.read()

    async def test_requester_and_worker_each_need_their_own_current_read_grant(self):
        for worker, requester in ((False, True), (True, False)):
            self.publish_authority(worker=worker, requester=requester)
            with self.assertRaises(ControlPlaneBrokerDeniedError):
                await self.read()
        self.assertFalse(self.env.server.calls)

    async def test_no_implicit_declaration_or_unvalidated_assignment(self):
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await self.read(validator=False)
        reference = self.publish_profile(declared=False)
        self.assignment = self.assignment.model_copy(update={'execution_profile_definition': reference})
        self.current = self.assignment
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await self.read()
        self.assertFalse(self.env.server.calls)

    async def test_superseded_definition_or_role_revocation_during_read_blocks_result(self):
        self.env.during_status(lambda: self.publish_profile())
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await self.read()
        self.assignment = self.assignment.model_copy(update={'execution_profile_definition': self.profiles.resolve('repository-write')[1]})
        self.current = self.assignment
        self.env.during_status(lambda: self.publish_authority(requester=False))
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await self.read()

    async def test_expired_lease_fence_and_assignment_changes_block_return(self):
        original = self.assignment
        for values in ({'fence': 2}, {'created_by': 'other-requester'}, {'assigned_worker_id': 'worker-2'},
                       {'project_id': 'project-b'}, {'resource_ids': ()},
                       {'lease': original.lease.model_copy(update={'expires_at': 1})}):
            with self.subTest(values=tuple(values)):
                self.current = original
                self.env.during_status(lambda values=values: setattr(self, 'current', original.model_copy(update=values)))
                with self.assertRaises(ControlPlaneBrokerDeniedError):
                    await self.read()

    async def test_disabled_or_ambiguous_binding_is_not_selected(self):
        self.change_binding(enabled=False)
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await self.read()
        self.change_binding(enabled=True)
        self.actions.bind(self.binding_payload, actor=self.env.admin, resources=self.env.resources)
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await self.read()
        self.assertFalse(self.env.server.calls)

    async def test_binding_revocation_and_ambiguity_during_read_block_result(self):
        self.env.during_status(lambda: self.change_binding(enabled=False))
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await self.read()
        self.change_binding(enabled=True)
        self.env.during_status(lambda: self.actions.bind(self.binding_payload,
            actor=self.env.admin, resources=self.env.resources))
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await self.read()

    async def test_secret_rights_remain_the_workers_despite_admin_requester(self):
        def deny(state):
            state.references = [item.model_copy(update={'allowed_identity_ids': []}) for item in state.references]
            return state
        self.env.secret.store.update(deny)
        with self.assertRaises(SecretUseDeniedError):
            await self.read()
        self.assertFalse(self.env.server.calls)

    async def test_caller_cannot_select_provider_url_foreign_resource_or_body(self):
        for path, body in ((PATH + '?provider=github', b''), (PATH + '?url=https://other.example', b''),
                           (PATH + '?repository_resource_id=foreign', b''), (PATH, b'{"provider":"github"}')):
            with self.subTest(path=path):
                with self.assertRaises((ControlPlaneBrokerDeniedError, ControlPlaneBrokerRequestError)):
                    await self.read(path, body)
        self.assertFalse(self.env.server.calls)

    async def test_real_unix_broker_response_and_audit_contain_no_credentials(self):
        self.broker = AssignmentBoundControlPlaneBroker(self.service, assignment=self.assignment,
            worker_id='worker-1', worker_actor=self.env.worker, fence=1, validator=lambda: self.current)
        await self.broker.start()
        try:
            status, _, result = await broker_fixtures.ControlPlaneBrokerTests._request(self, 'GET', PATH)
            self.assertEqual(status, 200)
            self.assertTrue(result['item']['complete'])
            audit = self.service.audit.load().model_dump_json()
            self.assertNotIn('fixture-token', audit)
            self.assertNotIn('lease-token-that-is-never-exposed', audit)
            self.assertEqual(len(self.service.audit.load().events), 1)
        finally:
            await self.broker.stop()

    async def test_operator_read_uses_current_canonical_assignment_and_own_identity(self):
        from codex_web.services.broker_merge_requirements import inspect_merge_requirements
        self.install_assignment_store()
        result = await inspect_merge_requirements(self.service, assignment_id=self.assignment.id,
            project_id='project-a', repository_id=self.env.resource.id, external_id='7', actor=self.env.admin)
        self.assertTrue(result['item']['complete'])
        self.assertEqual(result['scope']['inspection_kind'], 'operator')
        self.assertEqual(result['scope']['inspecting_identity_id'], self.env.admin.identity_id)
        self.assertIsNone(result['scope']['inspecting_worker_identity_id'])
        events = self.env.secret.store.load().audit
        uses = [item for item in events if item.action == 'use']
        self.assertEqual({item.actor_identity_id for item in uses}, {self.env.admin.identity_id})

    async def test_operator_inspection_rechecks_project_lease_and_worker_custody(self):
        from codex_web.services.broker_merge_requirements import inspect_merge_requirements
        store = self.install_assignment_store()
        async def inspect(project='project-a'):
            return await inspect_merge_requirements(self.service, assignment_id=self.assignment.id,
                project_id=project, repository_id=self.env.resource.id, external_id='7', actor=self.env.admin)
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await inspect('project-b')
        self.assertFalse(self.env.server.calls)
        def revoke():
            def change(state):
                state.workers[0] = state.workers[0].model_copy(update={'lifecycle': 'revoked'})
                return state
            store.update_worker('worker-1', change)
        self.env.during_status(revoke)
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await inspect()

    async def test_operator_http_route_preserves_current_scope_and_attribution(self):
        from fastapi import FastAPI
        from codex_web.api.control_plane_broker import build_control_plane_broker_router
        from codex_web.identity import AuthenticationAssurance
        self.install_assignment_store()
        app = FastAPI()
        actor = self.env.admin.model_copy(update={'assurance': AuthenticationAssurance.MFA})
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)
        app.include_router(build_control_plane_broker_router(self.service))
        query = dict(assignment_id=self.assignment.id, project_id='project-a',
                     repository_resource_id=self.env.resource.id, external_id='7')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = await client.get('/api/control-plane-broker/merge-requirements', params=query)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['scope']['inspecting_identity_id'], actor.identity_id)
            observed = len(self.env.server.calls)
            denied = await client.get('/api/control-plane-broker/merge-requirements',
                params={**query, 'project_id': 'project-b'})
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(len(self.env.server.calls), observed)
            unsupported = await client.get('/api/control-plane-broker/merge-requirements',
                params={**query, 'provider': 'github'})
            self.assertEqual(unsupported.status_code, 422)
            self.assertEqual(len(self.env.server.calls), observed)
            self.assertNotIn('fixture-token', result.text + denied.text)

    async def test_independent_operator_cannot_borrow_requester_or_worker_rights(self):
        from fastapi import FastAPI
        from codex_web.api.control_plane_broker import build_control_plane_broker_router
        from codex_web.identity import TenantScope
        self.install_assignment_store()
        operator = self.env.identity.bootstrap_service_actor(identity_id='inspecting-operator', name='Operator',
            scope=TenantScope(), service_scopes=('identity:admin', 'secret:use'))
        app = FastAPI()
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = operator
            return await call_next(request)
        app.include_router(build_control_plane_broker_router(self.service))
        query = dict(assignment_id=self.assignment.id, project_id='project-a',
                     repository_resource_id=self.env.resource.id, external_id='7')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            async def read():
                return await client.get('/api/control-plane-broker/merge-requirements', params=query)
            self.assertEqual((await read()).status_code, 403)  # No operator read Role.
            active = self.registry.resolve(definition_id=AUTHORITY_ROLE_CATALOG_ID, kind=AUTHORITY_ROLE_CATALOG_KIND)
            payload = dict(active.payload)
            payload['bindings'] = [*payload['bindings'], dict(id='operator-inspection', role_id='merge-worker',
                subject_kind='identity', subject_id=operator.identity_id, organization_id='local',
                workspace_id='default', project_ids=['project-a'])]
            self.publish(AUTHORITY_ROLE_CATALOG_KIND, AUTHORITY_ROLE_CATALOG_ID, payload)
            self.assertEqual((await read()).status_code, 403)  # No operator credential-use rights.
            self.assertFalse(self.env.server.calls)
            def allow(state):
                state.references = [item.model_copy(update={'allowed_identity_ids':
                    [*item.allowed_identity_ids, operator.identity_id]}) for item in state.references]
                return state
            self.env.secret.store.update(allow)
            result = await read()
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['scope']['inspecting_identity_id'], operator.identity_id)
            self.assertEqual(result.json()['scope']['requester_identity_id'], self.env.admin.identity_id)
