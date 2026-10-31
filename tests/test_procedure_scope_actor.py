"""Procedure metadata scope resolution preserves canonical tenant boundaries."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_web.definitions import DefinitionRecord, DefinitionReference, DefinitionScope, definition_checksum
from codex_web.identity import (
    AuthenticationAssurance, PrincipalKind, TenantScope, Workspace,
)
from codex_web.models import ProjectCreate
from codex_web.resources import ResourceCreate, ResourceType
from codex_web.services.identity import IdentityError, IdentityService
from codex_web.services.procedure_scope_actor import ProcedureDefinitionScopeActor
from codex_web.services.projects import ProjectService
from codex_web.services.skills import SkillConflict
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.workspaces import WorkspaceMapper
from tests import test_skill_procedures as skill_fixture


class ProcedureScopeActorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backend = SQLiteStateStore(self.root / 'state.sqlite3')
        self.identity = IdentityService(IdentityStateStore(self.backend))
        self.identity.bootstrap_local()
        self.projects = ProjectService(ProjectRepository(self.root / 'projects.json',
            store=self.backend, workspace_mapper=WorkspaceMapper(root=self.root)))
        self.project = self.projects.create(ProjectCreate(name='Test', path=str(self.root)))
        self.resolver = ProcedureDefinitionScopeActor(self.identity, self.projects)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, scope_type, scope_id=None):
        return DefinitionRecord(definition_id='test-scope', kind='agent.skill',
            definition_schema_version='1.0', revision=1, scope_type=scope_type,
            scope_id=scope_id, payload={}, checksum=definition_checksum(
                definition_id='test-scope', kind='agent.skill',
                definition_schema_version='1.0', payload={}), created_by='test')

    def test_workspace_and_project_resolve_same_scope_only_principal(self):
        self.assertTrue(self.resolver.can_observe_reads(self.identity))
        workspace = self.record(DefinitionScope.WORKSPACE, 'default')
        project = self.record(DefinitionScope.PROJECT, self.project.id)
        for record in (workspace, project):
            with self.subTest(scope=record.scope_type):
                actor = self.resolver(record)
                self.assertEqual(actor.identity_id, 'procedure-contract-metadata')
                self.assertEqual(actor.principal_kind, PrincipalKind.SERVICE)
                self.assertEqual(actor.assurance, AuthenticationAssurance.SERVICE_TOKEN)
                self.assertEqual((actor.organization_id, actor.workspace_id), ('local', 'default'))
                self.assertEqual(actor.service_scopes, ())
                self.assertEqual(actor.roles, ())

    def test_missing_scope_project_workspace_or_wrong_project_organization_refuses(self):
        for record in (self.record(DefinitionScope.GLOBAL),
                       self.record(DefinitionScope.ORGANIZATION, 'local'),
                       self.record(DefinitionScope.WORKSPACE, 'missing'),
                       self.record(DefinitionScope.PROJECT, 'missing')):
            self.assertIsNone(self.resolver(record))
        foreign = self.projects.create(ProjectCreate(name='Foreign', path=str(self.root)),
            scope=TenantScope(organization_id='foreign', workspace_id='default'))
        self.assertIsNone(self.resolver(self.record(DefinitionScope.PROJECT, foreign.id)))

    def test_project_failure_and_unscoped_record_do_not_load_identity(self):
        with patch.object(self.backend, 'get', wraps=self.backend.get) as get:
            self.identity.observe_reads(lambda view:
                self.assertIsNone(self.resolver(self.record(DefinitionScope.GLOBAL), _identity=view)))
            self.identity.observe_reads(lambda view:
                self.assertIsNone(self.resolver(self.record(DefinitionScope.PROJECT, 'missing'), _identity=view)))
        self.assertFalse(any(call.args[0] == 'identity_state' for call in get.call_args_list))

    def test_shared_lazy_state_is_used_by_canonical_resolver_without_authentication(self):
        record = self.record(DefinitionScope.WORKSPACE, 'default')
        original = self.backend.get
        reads = []
        def get(namespace):
            reads.append(namespace)
            return original(namespace)
        def inspect(view):
            first = self.resolver(record, _identity=view)
            second = self.resolver(record, _identity=view)
            self.assertEqual(first.model_dump(exclude={'authenticated_at'}),
                             second.model_dump(exclude={'authenticated_at'}))
            self.assertGreaterEqual(second.authenticated_at, first.authenticated_at)
            state = view.state()
            state.workspaces.clear()
            self.assertIsNotNone(self.resolver(record, _identity=view))
        with patch.object(self.backend, 'get', get):
            self.identity.observe_reads(inspect)
        self.assertEqual(reads, ['identity_state'])

    def test_next_callback_observes_removed_workspace(self):
        record = self.record(DefinitionScope.WORKSPACE, 'default')
        self.assertIsNotNone(self.identity.observe_reads(lambda view: self.resolver(record, _identity=view)))
        def remove(state):
            state.workspaces.clear()
            return state
        self.identity.store.update(remove)
        self.assertIsNone(self.identity.observe_reads(lambda view: self.resolver(record, _identity=view)))

    def test_foreign_and_closed_identity_views_refuse(self):
        record = self.record(DefinitionScope.WORKSPACE, 'default')
        foreign = IdentityService(self.identity.store)
        with self.assertRaises(IdentityError):
            foreign.observe_reads(lambda view: self.resolver(record, _identity=view))
        escaped = []
        self.identity.observe_reads(lambda view: escaped.append(view))
        with self.assertRaises(IdentityError):
            self.resolver(record, _identity=escaped[0])

    def test_custom_state_project_lookup_and_callable_decline_shared_eligibility(self):
        with patch.object(self.identity, 'state', lambda: None):
            self.assertFalse(self.resolver.can_observe_reads(self.identity))
        with patch.object(self.projects, 'get', lambda project_id: None):
            self.assertFalse(self.resolver.can_observe_reads(self.identity))
        with patch.object(ProcedureDefinitionScopeActor, '__call__', lambda owner, record: None):
            self.assertFalse(self.resolver.can_observe_reads(self.identity))
        self.assertFalse(self.resolver.can_observe_reads(IdentityService(self.identity.store)))

    def test_custom_project_repository_or_mapper_helper_declines_without_effects(self):
        repository = self.projects.repository
        mapper = repository.workspace_mapper
        calls = []
        def forbidden(*args, **kwargs):
            calls.append('custom')
            raise AssertionError('custom project helper entered during eligibility')
        for owner, names in (
            (repository, ('load', '_legacy_or_default_payload', '_validate_portable_paths',
                          '_runtime_project', '_storage_payload', '_write_payload')),
            (mapper, ('runtime_path', 'storage_path', 'default_project_path',
                      '_under_runtime_root', '_relative_to')),
        ):
            for name in names:
                with self.subTest(name=name), patch.object(owner, name, forbidden):
                    self.assertFalse(self.resolver.can_observe_reads(self.identity))
        self.assertEqual(calls, [])
        self.assertTrue(self.resolver.can_observe_reads(self.identity))

    def skill_fixture(self):
        fixture = skill_fixture.ProcedureTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        identity = IdentityService(IdentityStateStore(fixture.state))
        def add(state):
            state.workspaces.append(Workspace(id=fixture.admin.workspace_id,
                organization_id=fixture.admin.organization_id, name='Procedure workspace'))
            return state
        identity.store.update(add)
        projects = ProjectService(ProjectRepository(Path(fixture.temp.name) / 'projects.json',
            store=fixture.state, workspace_mapper=WorkspaceMapper(root=Path(fixture.temp.name))))
        fixture.skills.procedure_scope_actor = ProcedureDefinitionScopeActor(identity, projects)
        resource = fixture.resources.create(ResourceCreate(resource_type=ResourceType.SERVICE,
            name='Scoped service'), actor=fixture.admin)
        content = skill_fixture.procedure()
        content['resource_selectors'][0]['resource_ids'] = [resource.id]
        published = fixture.publish(fixture.draft(content=content))
        reference = DefinitionReference.model_validate(published['definitionReference'])
        return fixture, identity, reference

    def test_real_required_skill_reuses_readers_for_scope_and_reference_resources(self):
        fixture, identity, reference = self.skill_fixture()
        self.assertTrue(fixture.skills.can_observe_metadata_reads(identity, fixture.resources))
        original = fixture.state.get
        reads = []
        def get(namespace):
            reads.append(namespace)
            return original(namespace)
        def identities(identity_view):
            def resources(resource_view):
                def definitions(definition_view):
                    for _ in range(2):
                        self.assertEqual(fixture.skills.validate_required_procedure(reference,
                            actor=fixture.admin, _definitions=definition_view,
                            _identity=identity_view, _resources=resource_view), reference)
                return fixture.skills.observe_definition_reads((reference,), definitions)
            return fixture.resources.observe_reads(resources)
        with patch.object(fixture.state, 'get', get):
            identity.observe_reads(identities)
        self.assertEqual(reads.count('identity_state'), 1)
        self.assertEqual(reads.count('resource_catalog'), 1)

    def test_shared_skill_reads_require_stock_project_and_validation_helpers(self):
        fixture, identity, _ = self.skill_fixture()
        resolver = fixture.skills.procedure_scope_actor
        fixture.skills.projects = resolver.projects
        self.assertTrue(fixture.skills.can_observe_metadata_reads(identity, fixture.resources))
        calls = []
        def forbidden(*args, **kwargs):
            calls.append('custom')
            raise AssertionError('custom helper entered during eligibility')
        for name in ('_project', '_assert_visible_scope', '_assert_procedure_fresh',
                     '_definition_reader', '_context'):
            with self.subTest(name=name), patch.object(fixture.skills, name, forbidden):
                self.assertFalse(fixture.skills.can_observe_metadata_reads(identity, fixture.resources))
        class CustomProjects:
            @property
            def get(self):
                calls.append('projects.get')
                raise AssertionError('custom project lookup inspected')
        fixture.skills.projects = CustomProjects()
        self.assertFalse(fixture.skills.can_observe_metadata_reads(identity, fixture.resources))
        self.assertEqual(calls, [])
        fixture.skills.projects = None
        self.assertTrue(fixture.skills.can_observe_metadata_reads(identity, fixture.resources))

    def test_custom_skill_scope_callback_keeps_original_record_only_signature(self):
        fixture, identity, reference = self.skill_fixture()
        calls = []
        def custom(record):
            calls.append(record.record_id)
            return fixture.admin
        fixture.skills.procedure_scope_actor = custom
        self.assertFalse(fixture.skills.can_observe_metadata_reads(identity, fixture.resources))
        self.assertEqual(fixture.skills.validate_required_procedure(reference, actor=fixture.admin), reference)
        self.assertTrue(calls)
        count = len(calls)
        record = fixture.definitions.get_record(reference.record_id)
        with self.assertRaises(SkillConflict):
            identity.observe_reads(lambda view:
                fixture.skills._validate_procedure_references(record, _identity=view))
        self.assertEqual(len(calls), count)

    def test_outcome_ancestry_never_enters_managed_source_with_metadata_reader(self):
        fixture, identity, reference = self.skill_fixture()
        record = fixture.definitions.get_record(reference.record_id)
        record.payload['provenance']['source_type'] = 'procedure_outcome'
        class Source:
            def assert_source(self, *args, **kwargs):
                raise AssertionError('managed source content entered')
        fixture.skills.outcome_drafts = Source()
        with self.assertRaisesRegex(SkillConflict, 'content I/O'):
            identity.observe_reads(lambda view: fixture.skills._outcome_guard(record, _identity=view))


if __name__ == '__main__':
    unittest.main()
