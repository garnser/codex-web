from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_contract_seed import execution_role_catalog_seed_payload
from codex_web.identity import TenantScope
from codex_web.models import Project, WorkItemState
from codex_web.owner_routing import OwnerRoutingError
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_role_definitions import install_execution_role_definitions
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.workspaces import WorkspaceMapper


class OwnerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        catalog = patch('codex_web.execution_contracts._catalog_provider', None)
        catalog.start()
        self.addCleanup(catalog.stop)
        root = Path(self.temp.name)
        self.state = SQLiteStateStore(root / 'state.sqlite3')
        repository = ProjectRepository(root / 'projects.json', store=self.state,
                                       workspace_mapper=WorkspaceMapper(root=root))
        repository.save([Project(id=name, name=name, path=str(root), organization_id='org-a', workspace_id='workspace-a')
                         for name in ('project-a', 'project-b')])
        self.projects = ProjectService(repository)
        self.registry = DefinitionRegistryService(DefinitionRegistryStore(self.state))
        self.roles = install_execution_role_definitions(self.registry, projects=self.projects)
        self.scope = TenantScope(organization_id='org-a', workspace_id='workspace-a')

    def publish(self, mappings=None, *, project='project-a'):
        payload = execution_role_catalog_seed_payload()
        payload['owner_to_execution_role'].update(mappings or {'operator': 'orchestrator'})
        draft = self.registry.create_draft(DefinitionDraftCreate(definition_id='execution-roles.default',
            kind='execution-role-catalog', definition_schema_version='1.0', payload=payload,
            scope_type='project' if project else 'global', scope_id=project, actor='fixture'))
        self.registry.approve_publication(draft.record_id, actor='fixture-reviewer',
            reference='PRIVATE-ROUTING', reason='private canonical coordination alias fixture')
        return self.registry.publish(draft.record_id, DefinitionPublishRequest(actor='fixture'))

    def snapshot(self, *, project='project-a', scope=None):
        return self.roles.owner_routing(project_id=project, scope=scope or self.scope)

    def test_explicit_project_mapping_preserves_canonical_role_and_claim_names(self):
        record = self.publish()
        route = self.snapshot()
        self.assertEqual(route.owner(' Operator '), 'orchestrator')
        self.assertEqual(route.owner('Orchestrator'), 'orchestrator')
        for owner in ('Carl', 'James', 'Nora', 'Quinn', 'release manager'):
            self.assertEqual(route.owner(owner), owner.lower())
        observation = route.observation('Operator')
        self.assertEqual(observation['configured_owner'], 'operator')
        self.assertEqual(observation['definition']['record_id'], record.record_id)
        self.assertFalse(observation['grants_execution_authority'])

    def test_global_mapping_cannot_activate_project_owner_aliases(self):
        self.publish(project=None)
        route = self.snapshot()
        with self.assertRaisesRegex(OwnerRoutingError, 'requires_project'):
            route.owner('operator')
        self.assertEqual(route.owner('orchestrator'), 'orchestrator')
        self.assertEqual(route.observation('operator')['status'], 'blocked')

    def test_cross_project_and_foreign_tenant_cannot_borrow_mapping(self):
        self.publish()
        self.assertEqual(self.snapshot(project='project-b').owner('operator'), 'operator')
        with self.assertRaises(ProjectNotFoundError):
            self.snapshot(scope=TenantScope(organization_id='foreign', workspace_id='workspace-a'))

    def test_alias_cannot_rename_known_or_claimed_implementation_and_release_owners(self):
        self.publish({'carl': 'orchestrator', 'release manager': 'orchestrator', 'operator': 'orchestrator'})
        route = self.snapshot()
        for owner in ('carl', 'release manager'):
            with self.assertRaisesRegex(OwnerRoutingError, 'canonical_owner_remap'):
                route.owner(owner)
        state = WorkItemState(ref='acme/widgets#1', organization_id='org-a', workspace_id='workspace-a',
            project_id='project-a', implementation_owner='operator', created_at=1,
            updated_at=1, last_meaningful_update_at=1)
        original = state.model_dump(mode='json')
        with self.assertRaisesRegex(OwnerRoutingError, 'claimed_lane'):
            route.for_work_item(state)
        self.assertEqual(state.model_dump(mode='json'), original)

    def test_cycles_and_conflicting_normalized_aliases_never_select_a_recipient(self):
        with self.assertRaises(ValueError):
            self.publish({'operator': 'operator-two', 'operator-two': 'operator'})
        self.publish({'operator': 'orchestrator', ' Operator ': 'james'})
        with self.assertRaisesRegex(OwnerRoutingError, 'ambiguous_owner_mapping'):
            self.snapshot().owner('operator')

    def test_reopen_and_other_registry_publication_use_current_exact_definition(self):
        first = self.publish()
        self.assertEqual(self.snapshot().definition.record_id, first.record_id)
        reader = install_execution_role_definitions(DefinitionRegistryService(
            DefinitionRegistryStore(SQLiteStateStore(self.state.path))), projects=self.projects)
        self.assertEqual(reader.owner_routing(project_id='project-a', scope=self.scope).owner('operator'), 'orchestrator')
        second = self.publish({'operator': 'james'})
        current = reader.owner_routing(project_id='project-a', scope=self.scope)
        self.assertEqual(current.definition.record_id, second.record_id)
        self.assertEqual(current.owner('operator'), 'operator')  # Non-coordination mapping cannot rename.
