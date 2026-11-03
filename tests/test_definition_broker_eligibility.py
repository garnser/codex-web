"""Private canonical catalog/identity + real UDS; final procedure owner controlled."""
import asyncio
from copy import deepcopy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.models import Project
from codex_web.resources import RepositoryExecutionScope, RepositoryExecutionTarget, ResourceCreate, ResourceType
from codex_web.services.control_plane_broker import DeferredControlPlaneBrokerFactory
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from codex_web.services.resources import ResourceCatalogService
from codex_web.storage.resource_catalog import ResourceCatalogStore
from tests import test_control_plane_broker as broker_fixtures


class DefinitionBrokerEligibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = broker_fixtures.ControlPlaneBrokerTests()
        await self.f.asyncSetUp(start_broker=False)
        self.addCleanup(self.f.temp.cleanup)
        self.addAsyncCleanup(self.cleanup_brokers)
        self.brokers = []
        self.admin = self.f.identity.local_trusted_actor()
        self.profiles = install_execution_profile_definitions(self.f.registry)
        self.f.service.execution_profiles = self.profiles
        self.profile_id = 'custom-readonly-procedure-supervisor'
        self.operations = ('procedure.read', 'procedure.start', 'procedure.advance')
        self.reference = self.publish(self.operations)
        resources = ResourceCatalogService(ResourceCatalogStore(self.f.sqlite))
        self.repository = resources.create(ResourceCreate(resource_type=ResourceType.REPOSITORY,
            name='Explicit private READ context'), actor=self.admin)
        resources.bind_project(project=Project(id='project-a', name='Private', path='/fixture',
            organization_id='local', workspace_id='default'), resource_id=self.repository.id, actor=self.admin)
        self.f.authority.resources = resources
        target = RepositoryExecutionTarget(organization_id='local', workspace_id='default',
            project_id='project-a', read_only_primary_repository_id=self.repository.id,
            read_only_repository_ids=(self.repository.id,), source='explicit')
        self.assignment = self.f.assignment.model_copy(update={
            'execution_profile_id': self.profile_id, 'execution_profile_definition': self.reference,
            'sandbox': 'read-only', 'resource_ids': (self.repository.id,),
            'repository_target': target, 'repository_scope': RepositoryExecutionScope.from_target(target)})
        self.f.current_assignment = self.assignment
        self.final_owner = AsyncMock(return_value={'item': {'id': 'same-owned-checkpoint'}})
        self.f.service.assignment_reasoning = SimpleNamespace(dispatch=self.final_owner)
        self.factory = DeferredControlPlaneBrokerFactory()
        self.factory.configure(self.f.service)

    async def cleanup_brokers(self):
        for broker in self.brokers:
            await broker.stop()
            await asyncio.gather(*tuple(broker._handler_tasks), return_exceptions=True)

    def publish(self, operations):
        active = self.profiles.record(project_id='project-a', organization_id='local', workspace_id='default')
        payload = deepcopy(active.payload)
        payload['profiles'] = [p for p in payload['profiles'] if p['id'] != self.profile_id]
        payload['profiles'].append({'id': self.profile_id, 'name': 'Private procedure supervisor',
            'description': 'Private exact-declaration READ supervisor fixture.',
            'workspace_mode': 'repository', 'repository_access': 'read-only',
            'required_worker_capabilities': ['git', 'command_execution'],
            'control_plane_operations': list(operations), 'network_enabled': False, 'host_mutation': False})
        draft = self.f.registry.create_draft(DefinitionDraftCreate(
            definition_id=active.definition_id, kind=active.kind, definition_schema_version='1.0',
            scope_type='project', scope_id='project-a', payload=payload,
            actor=self.admin.identity_id, reason='Private exact custom broker declaration'))
        self.f.registry.publish(draft.record_id, DefinitionPublishRequest(actor=self.admin.identity_id,
            reason='Private fixture', expected_active_revision=active.revision if active.scope_type == 'project' else None))
        return self.profiles.resolve(self.profile_id, organization_id='local', workspace_id='default',
            project_id='project-a')[1]

    async def start(self, assignment=None):
        assignment = assignment or self.assignment
        broker = await self.factory.start(assignment=assignment, worker_id='worker-1',
            worker_actor=self.f.worker_actor, fence=assignment.fence,
            validator=lambda: self.f.current_assignment)
        if broker is not None:
            self.brokers.append(broker)
            self.f.broker = broker
        return broker

    async def request(self, method='GET', target=None, payload=None):
        return await self.f._request(method, target or
            '/api/execution-workers/assignments/' + self.assignment.id + '/procedure', payload=payload)

    async def test_custom_profile_factory_starts_declared_readonly_broker(self):
        broker = await self.start()
        self.assertIsNotNone(broker, 'Current canonical custom declaration must start the ordinary broker')
        self.assertTrue(broker.socket_path.exists())
        self.assertEqual(broker.worker_id, 'worker-1')
        self.assertEqual(broker.fence, self.assignment.fence)

    async def test_custom_descriptor_exposes_only_exact_three_declared_operations(self):
        public = self.f.service.public_assignment_capability(self.assignment)
        self.assertTrue(public['enabled'])
        self.assertEqual({op['id'] for op in public['operations']}, set(self.operations))
        self.assertEqual(public['assignment_context']['execution_profile_definition'],
                         self.reference.model_dump(mode='json'))
        self.assertFalse(public['credential_exposed'])
        self.assertNotIn(self.assignment.lease.lease_token, str(public))

    async def test_declared_procedure_read_reaches_existing_adapter_and_audit(self):
        self.assertIsNotNone(await self.start())
        status, _, body = await self.request()
        self.assertEqual(status, 200, body)
        self.final_owner.assert_awaited_once()
        args = self.final_owner.await_args.args
        self.assertEqual(args[0].id, self.assignment.id)
        self.assertEqual(args[1].identity_id, self.f.worker_actor.identity_id)
        self.assertEqual(args[2].id, 'procedure.read')
        self.assertEqual(args[3], self.assignment.id)
        event = self.f.service.audit.load().events[-1]
        self.assertEqual(event.operation_id, 'procedure.read')

    async def test_custom_contract_does_not_gain_catalog_or_work_mutation(self):
        self.assertIsNotNone(await self.start())
        status, _, _ = await self.request(target='/api/control-plane-broker/operations')
        self.assertEqual(status, 403)
        status, _, _ = await self.request('POST', '/api/work-items/group%2Fapp%2342/handoff',
            {'from_agent': 'sender', 'to_agent': 'recipient'})
        self.assertEqual(status, 403)
        self.final_owner.assert_not_awaited()
        self.assertEqual(self.f.work_items.handoffs, [])

    async def test_superseded_custom_pin_disables_factory_descriptor_and_current_dispatch(self):
        self.assertIsNotNone(await self.start())
        self.publish(self.operations)
        public = self.f.service.public_assignment_capability(self.assignment)
        self.assertFalse(public['enabled'])
        self.assertEqual(public['operations'], [])
        self.assertIsNone(await self.start())
        status, _, _ = await self.request()
        self.assertEqual(status, 403)
        self.final_owner.assert_not_awaited()

    async def test_missing_pin_or_unresolved_profile_never_gets_transport(self):
        for changes in ({'execution_profile_definition': None},
                        {'execution_profile_id': 'never-published'}, {'project_id': 'other-project'}):
            with self.subTest(changes=changes):
                assignment = self.assignment.model_copy(update=changes)
                self.assertIsNone(await self.start(assignment))
                self.assertFalse(self.f.service.public_assignment_capability(assignment)['enabled'])

    async def test_empty_or_unrecognized_operations_are_not_eligible(self):
        for operations in ((), ('not-a-registered-broker-operation',)):
            with self.subTest(operations=operations):
                reference = self.publish(operations)
                assignment = self.assignment.model_copy(update={'execution_profile_definition': reference})
                self.assertIsNone(await self.start(assignment))
                self.assertEqual(self.f.service.public_assignment_capability(assignment)['operations'], [])

    async def test_missing_resolver_cannot_use_pinned_custom_name(self):
        self.f.service.execution_profiles = None
        self.assertIsNone(await self.start())
        self.assertFalse(self.f.service.public_assignment_capability(self.assignment)['enabled'])

    async def test_custom_dispatch_preserves_fence_and_live_lease_refusal(self):
        self.assertIsNotNone(await self.start())
        for changes in ({'fence': self.assignment.fence + 1},
                        {'lease': self.assignment.lease.model_copy(update={'expires_at': 0})}):
            with self.subTest(changes=changes):
                self.f.current_assignment = self.assignment.model_copy(update=changes)
                status, _, _ = await self.request()
                self.assertEqual(status, 403)
        self.final_owner.assert_not_awaited()

    async def test_custom_dispatch_preserves_revoked_worker_identity_refusal(self):
        self.assertIsNotNone(await self.start())
        def disable(state):
            state.services = [row.model_copy(update={'disabled_at': 1}) if row.id == self.f.worker_actor.identity_id
                              else row for row in state.services]
            return state
        self.f.identity.store.update(disable)
        status, _, _ = await self.request()
        self.assertEqual(status, 403)
        self.final_owner.assert_not_awaited()

    async def test_recognized_subset_never_advertises_unknown_declared_names(self):
        reference = self.publish((*self.operations, 'unknown-operation'))
        assignment = self.assignment.model_copy(update={'execution_profile_definition': reference})
        public = self.f.service.public_assignment_capability(assignment)
        self.assertTrue(public['enabled'])
        self.assertEqual({op['id'] for op in public['operations']}, set(self.operations))

    async def test_released_legacy_metadata_behavior_does_not_require_retroactive_migration(self):
        self.f.service.execution_profiles = None
        for profile in ('orchestration-only', 'repository-write', 'orchestration-admin', 'project-governor'):
            with self.subTest(profile=profile):
                legacy = self.f.assignment.model_copy(update={'execution_profile_id': profile})
                public = self.f.service.public_assignment_capability(legacy)
                self.assertTrue(public['enabled'])

    async def test_existing_custom_transport_cannot_switch_to_legacy_contract_at_same_fence(self):
        self.assertIsNotNone(await self.start())
        self.f.current_assignment = self.assignment.model_copy(update={'execution_profile_id': 'repository-write'})
        status, _, _ = await self.request('POST', '/api/work-items/group%2Fapp%2342/handoff',
            {'from_agent': 'sender', 'to_agent': 'recipient'})
        self.assertEqual(status, 403)
        self.assertEqual(self.f.work_items.handoffs, [])
        self.final_owner.assert_not_awaited()
