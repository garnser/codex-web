"""#2108: complete scoped Goal roots without repeated full Work histories."""
import tempfile
import time
import unittest
from pathlib import Path

from codex_web.goals import GoalCreate, GoalWorkGraphBinding
from codex_web.identity import TenantScope
from codex_web.models import WorkItemState
from codex_web.services.goals import GoalService
from codex_web.services.work_graph import WorkGraphService, WorkGraphNotFoundError
from codex_web.storage.goals import GoalStore
from codex_web.storage.runtime_state import ModelMapRepository
from codex_web.storage.work_graph import WorkGraphStore
from codex_web.work_graph import WorkGraphState, WorkGraphEdge, WorkGraphRelation
from tests import test_goals as goals_fixture
from tests import test_work_graph_projection as graph_fixture


class GoalRootProjectionCostTests(unittest.TestCase):
    def test_many_root_goal_uses_projected_work_without_full_corpus_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            storage = graph_fixture.MeasuredStore(path / 'state.db')
            repository = ModelMapRepository(storage, namespace='work_item_states',
                legacy_path=path / 'absent.json', model=WorkItemState)
            scope = TenantScope(organization_id='org-a', workspace_id='ws-a')
            roots = tuple('project-a!'+str(index) for index in range(1,27))
            now = time.time()
            rows = {}
            for ref in (*roots, 'project-a!child', 'project-a!unbound', 'project-b!foreign'):
                rows[ref] = WorkItemState(ref=ref, organization_id=scope.organization_id,
                    workspace_id=scope.workspace_id,
                    project_id='project-b' if ref.startswith('project-b') else 'project-a',
                    title=ref, current_stage='implementation_active',
                    notes=['Retained unrelated history, excluded from graph headers.'],
                    created_at=now, updated_at=now, last_meaningful_update_at=now).model_dump(mode='json')
            storage.record_replace('work_item_states', rows)
            graph_store = WorkGraphStore(storage)
            graph_store.update(lambda _: WorkGraphState(edges=[WorkGraphEdge(
                organization_id=scope.organization_id, workspace_id=scope.workspace_id,
                project_id='project-a', relation=WorkGraphRelation.PARENT,
                source_ref=roots[0], target_ref='project-a!child', created_by='owner')]))
            projected = WorkGraphService(graph_store, repository.load,
                snapshot_items=lambda project, tenant:WorkGraphService.projected_snapshot_items(repository, project, tenant))
            goal_store = GoalStore(storage)
            goals = GoalService(goal_store, goals_fixture._Projects(), projected)
            goal = goals.create(GoalCreate(title='Current multi-root mission',
                description='Inspect the existing scoped roots and their actual child.', owner_identity_id='owner',
                work_graph_bindings=(GoalWorkGraphBinding(project_id='project-a',root_work_item_refs=roots),)),
                scope=scope, actor_id='owner')
            reference = GoalService(goal_store, goals_fixture._Projects(), WorkGraphService(graph_store, repository.load))
            expected = reference.snapshot(goal.id, scope=scope, now=now)
            storage.reset_cost()
            actual = goals.snapshot(goal.id, scope=scope, now=now)
            self.assertEqual(actual, expected)
            self.assertEqual(actual.progress.work_item_count,27)
            self.assertGreater(storage.projected_reads,0)
            self.assertEqual(storage.full_reads,0,
                'Projected Goal snapshot still reloaded the full Work corpus once per root')
            self.assertEqual(storage.projected_reads,1, 'Progress must reuse the same complete Project snapshot')


class GoalRootScopeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        self.storage = graph_fixture.MeasuredStore(path / 'state.db')
        self.repository = ModelMapRepository(self.storage, namespace='work_item_states',
            legacy_path=path / 'absent.json', model=WorkItemState)
        self.scope = TenantScope(organization_id='org-a', workspace_id='ws-a')
        self.rows = {ref:self.row(ref) for ref in ('A','B','C','D','E')}
        self.rows.update({ref:self.row(ref,project_id='project-b') for ref in ('project-b!1','project-b!2')})
        self.rows['foreign-org'] = self.row('foreign-org',organization_id='other')
        self.rows['foreign-workspace'] = self.row('foreign-workspace',workspace_id='other')
        self.storage.record_replace('work_item_states',self.rows)
        self.graph_store = WorkGraphStore(self.storage)
        self.graph = WorkGraphService(self.graph_store,self.repository.load,
            snapshot_items=lambda project,scope:WorkGraphService.projected_snapshot_items(self.repository,project,scope))
        self.goals = GoalService(GoalStore(self.storage),goals_fixture._Projects(),self.graph)

    def row(self,ref,**changes):
        now=time.time()
        return WorkItemState(ref=ref,organization_id='org-a',workspace_id='ws-a',project_id='project-a',
            title=ref,current_stage='implementation_active',created_at=now,updated_at=now,
            last_meaningful_update_at=now).model_copy(update=changes).model_dump(mode='json')

    def edges(self,*pairs):
        self.graph_store.update(lambda _:WorkGraphState(edges=[WorkGraphEdge(
            organization_id=self.scope.organization_id,workspace_id=self.scope.workspace_id,
            project_id='project-a',source_ref=source,target_ref=target,relation=relation,created_by='owner')
            for source,target,relation in pairs]))

    def create(self,*bindings):
        return self.goals.create(GoalCreate(title='Scoped existing Work',description='Read only this bound graph.',
            owner_identity_id='owner',work_graph_bindings=bindings),scope=self.scope,actor_id='owner')

    def test_parent_downstream_overlap_matches_canonical_traversal(self):
        self.edges(('A','B',WorkGraphRelation.PARENT),('B','C',WorkGraphRelation.PARENT),
            ('A','D',WorkGraphRelation.BLOCKS),('E','A',WorkGraphRelation.PARENT))
        roots=('A','B')
        goal=self.create(GoalWorkGraphBinding(project_id='project-a',root_work_item_refs=roots))
        expected=set(roots)
        for root in roots:
            expected.update(self.graph.traverse(root,scope=self.scope,project_id='project-a',
                relation=WorkGraphRelation.PARENT,direction='downstream'))
        self.storage.reset_cost()
        actual=self.goals._bound_refs(goal,scope=self.scope)
        self.assertEqual(actual,tuple(('project-a',ref) for ref in sorted(expected)))
        self.assertEqual(expected,{'A','B','C'})
        self.assertEqual(self.storage.full_reads,0)
        self.assertEqual(self.storage.projected_reads,1)

    def test_unrooted_second_project_and_tenant_scope_are_complete(self):
        self.edges(('A','B',WorkGraphRelation.PARENT))
        goal=self.create(GoalWorkGraphBinding(project_id='project-a',root_work_item_refs=('A',)),
            GoalWorkGraphBinding(project_id='project-b'))
        self.storage.reset_cost()
        snapshot=self.goals.snapshot(goal.id,scope=self.scope)
        self.assertEqual(snapshot.progress.work_item_count,4)
        self.assertEqual(self.storage.full_reads,0)
        self.assertEqual(self.storage.projected_reads,2)
        self.assertEqual(self.goals._bound_refs(goal,scope=self.scope),
            (('project-a','A'),('project-a','B'),('project-b','project-b!1'),('project-b','project-b!2')))

    def test_missing_or_moved_root_refuses_without_shrinking_scope(self):
        goal=self.create(GoalWorkGraphBinding(project_id='project-a',root_work_item_refs=('A',)))
        for changed in (None,{'organization_id':'other'},{'workspace_id':'other'},{'project_id':'project-b'}):
            with self.subTest(changed=changed):
                self.storage.record_apply('work_item_states',upserts={'A':self.rows['A']})
                if changed is None:
                    self.storage.record_apply('work_item_states',upserts={},deletes=('A',))
                else:
                    self.storage.record_apply('work_item_states',upserts={'A':{**self.rows['A'],**changed}})
                self.storage.reset_cost()
                with self.assertRaises(WorkGraphNotFoundError):
                    self.goals.snapshot(goal.id,scope=self.scope)
                self.assertEqual(self.storage.full_reads,0)

    def test_dangling_parent_descendant_remains_missing_completion_blocker(self):
        self.edges(('A','C',WorkGraphRelation.PARENT))
        self.storage.record_apply('work_item_states',upserts={'A':self.row('A',
            current_stage='closed',terminal_outcome='completed')})
        goal=self.create(GoalWorkGraphBinding(project_id='project-a',root_work_item_refs=('A',)))
        self.storage.record_apply('work_item_states',upserts={},deletes=('C',))
        self.storage.reset_cost()
        rows=self.goals._completion_work_items(goal,scope=self.scope)
        self.assertEqual(self.storage.full_reads,0)
        self.assertEqual(self.storage.projected_reads,1)
        missing=next(row for row in rows if row.work_item_ref=='C')
        self.assertFalse(missing.passed)
        self.assertEqual(missing.findings,('bound Work Item is missing from canonical graph',))
        progress=self.goals.progress(goal.id,scope=self.scope)
        self.assertEqual((progress.work_item_count,progress.completed,progress.completion_fraction),(2,1,0.5))

    def test_next_read_observes_current_headers_and_edges_without_cache(self):
        self.edges(('A','B',WorkGraphRelation.PARENT))
        goal=self.create(GoalWorkGraphBinding(project_id='project-a',root_work_item_refs=('A',)))
        before=self.goals.snapshot(goal.id,scope=self.scope)
        self.storage.record_apply('work_item_states',upserts={'B':self.row('B',
            current_stage='closed',terminal_outcome='completed')})
        self.edges(('A','B',WorkGraphRelation.PARENT),('B','C',WorkGraphRelation.PARENT))
        self.storage.reset_cost()
        after=self.goals.snapshot(goal.id,scope=self.scope)
        self.assertEqual((before.progress.work_item_count,before.progress.completed),(2,0))
        self.assertEqual((after.progress.work_item_count,after.progress.completed),(3,1))
        self.assertEqual(self.storage.full_reads,0)
        self.assertEqual(self.storage.projected_reads,1)

    def test_descendant_beyond_first_projection_page_is_not_truncated(self):
        self.storage.record_apply('work_item_states',upserts={ref:self.row(ref) for ref in
            (*('fill-'+str(i).zfill(4) for i in range(1020)),'zz-last')})
        self.edges(('A','zz-last',WorkGraphRelation.PARENT))
        goal=self.create(GoalWorkGraphBinding(project_id='project-a',root_work_item_refs=('A',)))
        self.storage.reset_cost()
        snapshot=self.goals.snapshot(goal.id,scope=self.scope)
        self.assertEqual(snapshot.progress.work_item_count,2)
        self.assertEqual(self.storage.full_reads,0)
        self.assertEqual(self.storage.projected_reads,2)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(loader.loadTestsFromTestCase(case) for case in
        (GoalRootProjectionCostTests,GoalRootScopeTests))
