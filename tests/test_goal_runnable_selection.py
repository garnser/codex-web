"""Canonical graph selection, public read compatibility and bounded broker reads."""
import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from codex_web.api.goals import build_goals_router
from codex_web.goals import GoalCreate, GoalWorkGraphBinding, GoalHealth, GoalUpdate
from codex_web.work_graph import WorkGraphEdgeCreate, WorkGraphRelation
from codex_web.identity import AuthenticationActor, PrincipalKind, MembershipRole, AuthenticationAssurance
from codex_web.services.goals import GoalConflictError
from codex_web.services.goal_continuation import GoalContinuationService
from codex_web.services.work_graph import WorkGraphService
from codex_web.storage.work_graph import WorkGraphStore
import tests.test_goals as goals_fixture
import tests.test_work_graph as graph_fixture
import tests.test_control_plane_broker as broker_fixture
import tests.test_assignment_reasoning_broker as reasoning_fixture


class GoalRunnableSelectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = goals_fixture.GoalServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service, self.scope = self.fixture.service, self.fixture.scope
        self.items = {f'run-{i}':graph_fixture.item(f'run-{i}') for i in range(2)}
        for i in range(20):
            node = graph_fixture.item(f'blocked-{i:02d}')
            node.blocker = 'supported prerequisite needs current authority'
            self.items[node.ref] = node
        for i in range(5):
            node = graph_fixture.item(f'done-{i}')
            node.terminal_outcome = 'completed'
            self.items[node.ref] = node
        self.items['unrelated'] = graph_fixture.item('unrelated')
        self.graph = WorkGraphService(WorkGraphStore(self.service.store.store), lambda:self.items)
        self.service.work_graph = self.graph
        roots = tuple(ref for ref in self.items if ref != 'unrelated')
        self.goal = self.service.create(GoalCreate(title='Genuine bounded migration',
            description='Resolve current scoped work and prerequisites', owner_identity_id='owner-a',
            work_graph_bindings=(GoalWorkGraphBinding(project_id='project-a', root_work_item_refs=roots),)),
            scope=self.scope, actor_id='owner-a')

    def test_canonical_counts_two_runnable_and_blocked_prerequisites_without_work_fanout(self):
        with patch.object(self.graph, 'snapshot', wraps=self.graph.snapshot) as snapshot:
            first = self.service.runnable_selection(self.goal.id, scope=self.scope)
        snapshot.assert_called_once()
        self.assertEqual((first.progress.work_item_count, first.progress.runnable,
            first.progress.blocked, first.progress.completed), (27,2,20,5))
        self.assertEqual({node.ref for node in first.runnable}, {'run-0','run-1'})
        self.assertEqual(len(first.blocked), 10)
        self.assertEqual(first.next_offset, 10)
        self.assertFalse(first.execution_authorized)
        self.assertFalse(first.complete)
        second = self.service.runnable_selection(self.goal.id, scope=self.scope, offset=first.next_offset)
        self.assertEqual(len(second.blocked), 10)
        self.assertFalse({node.ref for node in first.blocked} & {node.ref for node in second.blocked})
        self.assertEqual(first.source_snapshot_sha256, second.source_snapshot_sha256)
        self.assertIsNone(second.next_offset)
        self.assertNotIn('unrelated', {node.ref for node in (*first.runnable,*first.blocked,*second.blocked)})

    def test_large_display_text_keeps_exact_ref_and_fresh_changed_readiness(self):
        self.items['run-0'].title = 'large private display ' * 3000
        first = self.service.runnable_selection(self.goal.id, scope=self.scope)
        header = next(node for node in first.runnable if node.ref == 'run-0')
        self.assertTrue(header.title_truncated)
        self.assertEqual(header.ref, 'run-0')
        self.assertLessEqual(len(header.display_title), 200)
        self.items['run-0'].blocker = 'new current authority prerequisite'
        second = self.service.runnable_selection(self.goal.id, scope=self.scope)
        self.assertEqual(second.progress.runnable, 1)
        self.assertNotEqual(first.source_snapshot_sha256, second.source_snapshot_sha256)
        self.assertFalse(second.execution_authorized)
        self.items['blocked-00'].blocker = 'long reason ' * 1000
        third = self.service.runnable_selection(self.goal.id, scope=self.scope)
        blocked = next(node for node in third.blocked if node.ref == 'blocked-00')
        self.assertEqual(blocked.truncated_reason_count, 1)
        self.assertLessEqual(len(blocked.reasons[0]), 240)

    def test_parent_descendant_closure_and_missing_descendant_are_preserved(self):
        self.graph.add_edge(WorkGraphEdgeCreate(relation=WorkGraphRelation.PARENT,
            source_ref='run-0',target_ref='blocked-00'), scope=self.scope, actor_id='owner-a')
        goal = self.service.create(GoalCreate(title='Rooted work', description='Only canonical descendants',
            owner_identity_id='owner-a',work_graph_bindings=(GoalWorkGraphBinding(
                project_id='project-a',root_work_item_refs=('run-0',)),)),scope=self.scope,actor_id='owner-a')
        first = self.service.runnable_selection(goal.id, scope=self.scope)
        self.assertEqual(first.progress.work_item_count, 2)
        self.assertEqual([node.ref for node in first.blocked], ['blocked-00'])
        self.items.pop('blocked-00')
        second = self.service.runnable_selection(goal.id, scope=self.scope)
        self.assertEqual(second.missing_work, (('project-a','blocked-00'),))
        self.assertFalse(second.complete)

    def test_revision_changed_during_graph_read_refuses_mixed_goal_context(self):
        original = self.graph.snapshot
        revised = False
        def changed(*args, **options):
            nonlocal revised
            result = original(*args, **options)
            if not revised:
                revised = True
                self.service.revise(self.goal.id, GoalUpdate(title='Changed current Goal',reason='current operator revision'),
                    scope=self.scope,actor_id='owner-a')
            return result
        with patch.object(self.graph,'snapshot',side_effect=changed):
            with self.assertRaisesRegex(GoalConflictError,'Goal changed'):
                self.service.runnable_selection(self.goal.id,scope=self.scope)

    def test_project_scoped_selection_never_reads_foreign_graph(self):
        self.items['foreign'] = graph_fixture.item('foreign', project_id='project-b')
        self.goal = self.service.create(GoalCreate(title='Two project Goal', description='Scoped observations',
            owner_identity_id='owner-a', work_graph_bindings=(
                GoalWorkGraphBinding(project_id='project-a', root_work_item_refs=('run-0',)),
                GoalWorkGraphBinding(project_id='project-b', root_work_item_refs=('foreign',)))),
            scope=self.scope, actor_id='owner-a')
        original = self.graph.snapshot
        def scoped(project_id, **options):
            self.assertEqual(project_id, 'project-a', 'foreign graph must not be read')
            return original(project_id, **options)
        with patch.object(self.graph, 'snapshot', side_effect=scoped):
            selection = self.service.runnable_selection(self.goal.id, scope=self.scope, project_id='project-a')
        self.assertEqual(selection.progress.work_item_count, 1)
        self.assertEqual(selection.progress.project_count, 1)
        self.assertEqual(selection.goal_project_count, 2)
        self.assertFalse(selection.includes_all_goal_projects)
        self.assertFalse(selection.complete)
        self.assertEqual(selection.health.health, GoalHealth.UNKNOWN)

    def test_missing_bound_work_and_invalid_offset_never_become_all_blocked(self):
        self.items.pop('run-0')
        selection = self.service.runnable_selection(self.goal.id, scope=self.scope)
        self.assertEqual(selection.missing_work_count, 1)
        self.assertEqual(selection.missing_work, (('project-a','run-0'),))
        self.assertFalse(selection.complete)
        self.assertEqual(selection.health.health, GoalHealth.UNKNOWN)
        for offset in (-1,100001,True):
            with self.assertRaises(GoalConflictError):
                self.service.runnable_selection(self.goal.id, scope=self.scope, offset=offset)

    def test_public_api_same_typed_view_and_default_snapshot_remain_distinct(self):
        app = FastAPI()
        actor = AuthenticationActor(identity_id='owner-a', principal_kind=PrincipalKind.HUMAN,
            organization_id='org-a', workspace_id='ws-a', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.PRIMARY)
        @app.middleware('http')
        async def identity(request:Request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)
        app.include_router(build_goals_router(self.service))
        with TestClient(app) as client:
            path = '/api/goals/' + self.goal.id
            self.assertIn('snapshot', client.get(path).json())
            response = client.get(path, params={'view':'runnable','project_id':'project-a'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['selection']['progress']['runnable'], 2)
            self.assertEqual(client.get(path, params={'view':'other'}).status_code, 422)
            self.assertNotEqual(client.get(path, params={'offset':10}).status_code, 200)
            self.assertNotEqual(client.get(path, params={'view':'runnable','project_id':'project-b'}).status_code, 200)

    def test_goal_prompt_distinguishes_prerequisites_and_incomplete_reads(self):
        prompt = GoalContinuationService._prompt(NS(work_item_refs=('run-0',),goal_id=self.goal.id,
            goal_revision=1,project_id='project-a'))
        self.assertIn('A blocked Work is not a blocked Goal', prompt)
        self.assertIn('blocked-Work prerequisite', prompt)
        self.assertIn('rate-limited read leaves that information unknown', prompt)
        self.assertIn('does not authorize replay', prompt)


class BrokerBackpressureTests(broker_fixture.ControlPlaneBrokerTests):
    async def test_join_accepted_reads_then_fetch_needed_work_without_changing_broker_limit(self):
        entered = asyncio.Event(); release = asyncio.Event(); count = 0
        original = self.work_items.get
        before = self.assignment.model_dump(mode='json')
        async def held(*args, **options):
            nonlocal count
            count += 1
            if count <= 2:
                if count == 2: entered.set()
                await release.wait()
            return await original(*args, **options)
        path = '/api/work-items/' + quote(self.ref, safe='')
        accepted = []
        with patch.object(self.work_items, 'get', side_effect=held), patch.object(
                self.work_items,'progress',side_effect=AssertionError('no mutation retry')) as progress:
            try:
                accepted = [asyncio.create_task(self._request('GET', path)) for _ in range(2)]
                await entered.wait()
                status, _, body = await self._request('GET', path)
                self.assertEqual(status, 429)
                self.assertIn('concurrency', body['error']['message'])
                self.assertEqual(count, 2, 'refused request must not dispatch Work read')
            finally:
                release.set()
                results = await asyncio.gather(*accepted)
            self.assertTrue(all(result[0] == 200 for result in results))
            self.assertEqual((await self._request('GET', path))[0], 200)
        self.assertEqual(count, 3)
        self.assertEqual(self.current_assignment.model_dump(mode='json'), before)
        progress.assert_not_called()
        self.assertTrue(all(event.method == 'GET' for event in self.service.audit.load().events))


class GoalSelectionBrokerTests(reasoning_fixture.AssignmentReasoningTests):
    async def test_real_goal_selection_preserves_declared_scope_and_final_fence_check(self):
        from codex_web.services.goals import GoalService
        from codex_web.storage.goals import GoalStore
        items = {'current':graph_fixture.item('current',organization_id='local',workspace_id='default')}
        graph = WorkGraphService(WorkGraphStore(self.fixture.store.store),lambda:items)
        service = GoalService(GoalStore(self.fixture.store.store), NS(get=lambda *a,**k:None), graph)
        goal = service.create(GoalCreate(title='Current assignment Goal',description='Bounded read',
            owner_identity_id=self.actor.identity_id,work_graph_bindings=(GoalWorkGraphBinding(
                project_id='project-a',root_work_item_refs=('current',)),)),scope=self.actor.tenant,
            actor_id=self.actor.identity_id)
        self.adapter.executives.goals = service
        result = await self.call('goal.read',target=goal.id,query={'view':['runnable']})
        self.assertEqual(result['selection']['runnable'][0]['ref'],'current')
        self.assertFalse(result['selection']['execution_authorized'])
        self.assertIn('item',await self.call('goal.read',target=goal.id))
        for query in ({'view':['runnable'],'offset':['-1']},{'view':['runnable'],'other':['x']},
                      {'view':['runnable','runnable']}):
            with self.assertRaises(HTTPException):
                await self.call('goal.read',target=goal.id,query=query)
        original = graph.snapshot
        def changed(*args, **options):
            result = original(*args,**options)
            self.live.fence += 1
            return result
        with patch.object(graph,'snapshot',side_effect=changed):
            with self.assertRaises(HTTPException):
                await self.call('goal.read',target=goal.id,query={'view':['runnable']})


def load_tests(loader, standard_tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (GoalRunnableSelectionTests,BrokerBackpressureTests,GoalSelectionBrokerTests)
        for name,method in cls.__dict__.items() if name.startswith('test_') and callable(method))
