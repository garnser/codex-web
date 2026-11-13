"""Published role selection data on the existing assignment CREATE descriptor.

Private canonical Registry/Executive/broker owners; no App, socket, model or
activation. This is discovery data and does not prove mutation authorization.
"""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest, DefinitionScope
from codex_web.services.assignment_reasoning_broker import AssignmentReasoningBroker
from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
from codex_web.services.executive_roles import install_executive_role_definitions
from codex_web.services.definitions import DefinitionNotFoundError
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from tests import test_control_plane_broker as broker_fixtures
from tests import test_executive_management as executive_fixtures


class ExecutiveRoleDiscoveryContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker_fixture = f = broker_fixtures.ControlPlaneBrokerTests()
        await f.asyncSetUp(start_broker=False)
        # No Unix broker was started; its normal asyncTearDown assumes one.
        self.addCleanup(f.temp.cleanup)
        self.executive_fixture = executives = executive_fixtures.ExecutiveManagementTests()
        await executives.asyncSetUp()
        self.addAsyncCleanup(executives.asyncTearDown)

        profiles = install_execution_profile_definitions(f.registry)
        current = profiles.record(organization_id='local', workspace_id='default', project_id='project-a')
        payload = copy.deepcopy(current.payload)
        profile = copy.deepcopy(next(item for item in payload['profiles'] if item['id'] == 'orchestration-only'))
        profile.update(id='private-executive-discovery', name='Private declared Executive coordinator',
            control_plane_operations=['executive.activation.read', 'executive.activation.create',
                                      'executive.activation.consult'])
        payload['profiles'].append(profile)
        draft = f.registry.create_draft(DefinitionDraftCreate(
            definition_id=current.definition_id, kind=current.kind,
            definition_schema_version=current.definition_schema_version, payload=payload,
            scope_type=DefinitionScope.PROJECT, scope_id='project-a', actor='private-fixture',
            reason='Explicit existing Executive operation declaration for this isolated fixture'))
        f.registry.approve_publication(draft.record_id, actor='private-approver',
            reference='PRIVATE-FIXTURE', reason='Private canonical declaration')
        f.registry.publish(draft.record_id, DefinitionPublishRequest(actor='private-fixture',
            expected_active_revision=0))
        self.profile, pin = profiles.resolve('private-executive-discovery',
            organization_id='local', workspace_id='default', project_id='project-a')
        self.assignment = f.assignment.model_copy(update={
            'execution_profile_id': self.profile.id, 'execution_profile_definition': pin})
        f.service.execution_profiles = profiles

        roles = install_executive_role_definitions(f.registry)
        original = roles.record(organization_id='local', workspace_id='default')
        payload = copy.deepcopy(original.payload)
        cto = next(item for item in payload['roles'] if item['id'] == 'cto')
        cto.update(title='Project-specific technical risk owner',
            description='Select this published Project role for the guarded metadata risk judgment.',
            responsibilities=['Assess fresh authority and immutable data reuse boundaries.'])
        draft = f.registry.create_draft(DefinitionDraftCreate(
            definition_id=original.definition_id, kind=original.kind,
            definition_schema_version=original.definition_schema_version, payload=payload,
            scope_type=DefinitionScope.PROJECT, scope_id='project-a', actor='private-fixture',
            reason='Distinguishable current Project role guidance, not global defaults'))
        f.registry.approve_publication(draft.record_id, actor='private-approver',
            reference='PRIVATE-ROLE-FIXTURE', reason='Private role selection context')
        f.registry.publish(draft.record_id, DefinitionPublishRequest(actor='private-fixture',
            expected_active_revision=0))
        self.catalog, self.reference = roles.resolve(organization_id='local',
            workspace_id='default', project_id='project-a')
        self.assertEqual(self.reference.record_id, draft.record_id)
        self.assertNotEqual(self.reference.record_id, original.record_id)
        executives.service.roles = roles
        f.service.assignment_reasoning = AssignmentReasoningBroker(f.service,
            procedures=SimpleNamespace(), executives=executives.service)

    async def test_declared_create_catalog_exposes_current_project_cto_selection_without_activation(self):
        f, executives = self.broker_fixture, self.executive_fixture
        before = executives.store.load().model_dump(mode='json')
        with patch.object(executives.service, 'prepare', wraps=executives.service.prepare) as prepare, \
                patch.object(executives.service, 'create', wraps=executives.service.create) as create, \
                patch.object(executives.service, 'consult', wraps=executives.service.consult) as consult:
            capability = f.service.public_assignment_capability(self.assignment)
        self.assertTrue(capability['enabled'])
        matches = [item for item in capability['operations'] if item['id'] == 'executive.activation.create']
        self.assertEqual(len(matches), 1, 'The existing explicitly declared CREATE must remain available')
        operation = matches[0]
        self.assertIn('request_body', operation)
        self.assertIn('requested_role_ids', operation['request_body']['properties'])
        self.assertIn('role_selection_context', operation,
            'Declared Executive CREATE provides no current published role ID/guidance to its worker')
        context = operation['role_selection_context']
        self.assertEqual(context.get('definition'), self.reference.model_dump(mode='json'))
        self.assertTrue(context.get('effective_authority_requires_operation_checks'))
        self.assertEqual(context.get('max_roles_per_activation'), self.catalog.max_roles_per_activation)
        self.assertIsInstance(context.get('roles'), list)
        selected = [item for item in context['roles'] if item.get('id') == 'cto']
        self.assertEqual(len(selected), 1)
        actual = selected[0]
        expected = self.catalog.role_map['cto']
        self.assertEqual({key: actual.get(key) for key in ('id', 'name', 'title', 'description', 'responsibilities')},
            {key: expected.model_dump(mode='json')[key]
             for key in ('id', 'name', 'title', 'description', 'responsibilities')})
        prepare.assert_not_called()
        create.assert_not_called()
        consult.assert_not_called()
        self.assertEqual(executives.gateway.requests, [])
        self.assertEqual(executives.store.load().model_dump(mode='json'), before)

    def publish_current_roles(self, payload):
        roles = self.executive_fixture.service.roles
        registry = self.broker_fixture.registry
        current = roles.record(organization_id='local', workspace_id='default', project_id='project-a')
        draft = registry.create_draft(DefinitionDraftCreate(
            definition_id=current.definition_id, kind=current.kind,
            definition_schema_version=current.definition_schema_version, payload=payload,
            scope_type=DefinitionScope.PROJECT, scope_id='project-a', actor='private-fixture'))
        registry.approve_publication(draft.record_id, actor='private-approver',
            reference='PRIVATE-UPDATE', reason='Current canonical role selection change')
        registry.publish(draft.record_id, DefinitionPublishRequest(actor='private-fixture',
            expected_active_revision=current.revision))
        return roles.resolve(organization_id='local', workspace_id='default', project_id='project-a')

    def selection(self):
        capability = self.broker_fixture.service.public_assignment_capability(self.assignment)
        operation = next(item for item in capability['operations'] if item['id'] == 'executive.activation.create')
        return operation['role_selection_context']

    async def test_role_projection_observes_new_publication_and_filters_inactive_roles(self):
        original = self.selection()
        payload = copy.deepcopy(self.executive_fixture.service.roles.record(
            organization_id='local', workspace_id='default', project_id='project-a').payload)
        cto = next(item for item in payload['roles'] if item['id'] == 'cto')
        cto['description'] = 'New current guidance, not the previous catalog snapshot'
        catalog, reference = self.publish_current_roles(payload)
        current = self.selection()
        self.assertNotEqual(current['definition'], original['definition'])
        self.assertEqual(current['definition'], reference.model_dump(mode='json'))
        self.assertEqual(next(item for item in current['roles'] if item['id'] == 'cto')['description'],
            catalog.role_map['cto'].description)
        for lifecycle in ('deprecated', 'disabled'):
            with self.subTest(lifecycle=lifecycle):
                cto['lifecycle'] = lifecycle
                _, reference = self.publish_current_roles(payload)
                current = self.selection()
                self.assertEqual(current['definition'], reference.model_dump(mode='json'))
                self.assertNotIn('cto', {item['id'] for item in current['roles']})
        self.assertEqual(self.executive_fixture.gateway.requests, [])
        self.assertEqual(self.executive_fixture.store.load().activations, [])

    async def test_undeclared_create_has_no_role_projection_or_catalog_lookup(self):
        f = self.broker_fixture
        profiles = f.service.execution_profiles
        current = profiles.record(organization_id='local', workspace_id='default', project_id='project-a')
        payload = copy.deepcopy(current.payload)
        next(item for item in payload['profiles'] if item['id'] == self.profile.id)[
            'control_plane_operations'] = ['executive.activation.read']
        draft = f.registry.create_draft(DefinitionDraftCreate(definition_id=current.definition_id,
            kind=current.kind, definition_schema_version=current.definition_schema_version, payload=payload,
            scope_type=DefinitionScope.PROJECT, scope_id='project-a', actor='private-fixture'))
        f.registry.approve_publication(draft.record_id, actor='private-approver',
            reference='PRIVATE-NARROW', reason='Remove CREATE declaration, not an authority grant')
        f.registry.publish(draft.record_id, DefinitionPublishRequest(actor='private-fixture',
            expected_active_revision=current.revision))
        # Before adopting the new pin, the old assignment must expose nothing.
        with patch.object(self.executive_fixture.service.roles, 'resolve',
                wraps=self.executive_fixture.service.roles.resolve) as resolve:
            stale = f.service.public_assignment_capability(self.assignment)
        self.assertFalse(stale['enabled'])
        self.assertEqual(stale['operations'], [])
        resolve.assert_not_called()
        _, pin = profiles.resolve(self.profile.id, organization_id='local', workspace_id='default', project_id='project-a')
        self.assignment = self.assignment.model_copy(update={'execution_profile_definition': pin})
        with patch.object(self.executive_fixture.service.roles, 'resolve',
                wraps=self.executive_fixture.service.roles.resolve) as resolve:
            capability = f.service.public_assignment_capability(self.assignment)
        self.assertNotIn('executive.activation.create', {item['id'] for item in capability['operations']})
        resolve.assert_not_called()
        self.assertEqual(self.executive_fixture.store.load().activations, [])

    async def test_unavailable_role_catalog_is_explicit_and_never_falls_back_to_default_ids(self):
        before = self.executive_fixture.store.load().model_dump(mode='json')
        with patch.object(self.executive_fixture.service.roles, 'resolve',
                side_effect=DefinitionNotFoundError('private missing catalog detail')):
            context = self.selection()
        self.assertEqual(context['status'], 'unavailable')
        self.assertEqual(context['reason'], 'current_published_executive_role_context_unavailable')
        self.assertTrue(context['effective_authority_requires_operation_checks'])
        self.assertFalse(context['authorizes_mutation'])
        self.assertNotIn('roles', context)
        self.assertNotIn('definition', context)
        self.assertNotIn('private missing', str(context))
        self.assertEqual(self.executive_fixture.gateway.requests, [])
        self.assertEqual(self.executive_fixture.store.load().model_dump(mode='json'), before)

    async def test_unavailable_current_requester_does_not_expose_role_data_or_private_error(self):
        with patch.object(self.broker_fixture.service, '_requester_actor',
                side_effect=ControlPlaneBrokerDeniedError('private retired requester detail')), \
                patch.object(self.executive_fixture.service.roles, 'resolve',
                    wraps=self.executive_fixture.service.roles.resolve) as resolve:
            context = self.selection()
        self.assertEqual(context['status'], 'unavailable')
        self.assertFalse(context['authorizes_mutation'])
        self.assertNotIn('roles', context)
        self.assertNotIn('private retired', str(context))
        resolve.assert_not_called()
        self.assertEqual(self.executive_fixture.gateway.requests, [])
        self.assertEqual(self.executive_fixture.store.load().activations, [])
