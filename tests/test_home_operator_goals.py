import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.agent_runtime import AgentSession
from codex_web.goal_execution_bindings import GoalExecutionBinding
from codex_web.goals import GoalRecord
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.services.goal_execution_bindings import GoalExecutionBindingService
from codex_web.services.goals import GoalNotFoundError
from codex_web.services.home_operator import HomeOperatorConflict
from codex_web.services.home_operator_goals import HomeOperatorGoalInspection
from codex_web.services.identity import AuthorizationError
from codex_web.storage.agent_sessions import AgentSessionNotFoundError
from codex_web.storage.goal_execution_bindings import GoalExecutionBindingStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class HomeOperatorGoalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.actor = AuthenticationActor(identity_id='admin', principal_kind=PrincipalKind.HUMAN,
            organization_id='local', workspace_id='default', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED)
        self.selection = {'status': 'configured', 'binding': {'thread_id': 'operator', 'revision': 4},
                          'thread': {'id': 'operator', 'execution_profile_id': 'orchestration-only'}}
        self.goal = GoalRecord(id='goal', organization_id='local', workspace_id='default',
            title='Retained work', description='Never sent to a model by inspection',
            owner_identity_id='admin', created_by='admin', updated_by='admin')
        self.session = AgentSession(id='session', organization_id='local', workspace_id='default',
            provider_id='openai', runtime_id='codex-app-server', runtime_type='app-server',
            project_id='home', provider_native_session_id='operator')
        self.binding = GoalExecutionBinding(id='binding', organization_id='local', workspace_id='default',
            goal_id='goal', goal_revision=1, project_id='home', provider_id='openai',
            runtime_id='codex-app-server', agent_session_id='session', thread_id='operator',
            execution_owner_id='admin', created_by='admin', updated_by='admin', change_reason='attach',
            checkpoint_ref='artifact-retained', status='blocked', stop_reason='Required CI pending')
        self.sql = SQLiteStateStore(Path(temp.name) / 'state.db')
        self.store = GoalExecutionBindingStore(self.sql)
        self.replace([self.binding])
        bindings = GoalExecutionBindingService(self.store, SimpleNamespace(get_many=self.get_goals))
        self.operator = SimpleNamespace(status=lambda actor: copy.deepcopy(self.selection), clock=lambda: 123.)
        self.service = HomeOperatorGoalInspection(self.operator, bindings, SimpleNamespace(get=self.get_session))

    def get_goal(self, key, *, scope, project_id):
        self.assertEqual((scope, project_id), (self.actor.tenant, 'home'))
        if self.goal is None:
            raise GoalNotFoundError('missing')
        return self.goal

    def get_goals(self, keys, *, scope, project_id):
        return tuple(self.get_goal(key, scope=scope, project_id=project_id) for key in dict.fromkeys(keys))

    def get_session(self, key, actor):
        self.assertEqual((key, actor), ('session', self.actor))
        if self.session is None:
            raise AgentSessionNotFoundError('missing')
        return self.session

    def replace(self, rows):
        def apply(state):
            state.bindings = rows
            return state
        self.store.update(apply)

    def inspect(self):
        return self.service.inspect(self.actor, expected_revision=4)

    def test_checkpoint_and_blocker_survive_reopen_with_no_state_mutation(self):
        before = self.store.load().model_dump()
        result = self.inspect()
        reopened = GoalExecutionBindingService(GoalExecutionBindingStore(self.sql), self.service.bindings.goals)
        self.service.bindings = reopened
        self.assertEqual(self.inspect(), result)
        self.assertEqual(result['items'][0]['checkpoint_ref'], 'artifact-retained')
        self.assertEqual(result['items'][0]['stop_reason'], 'Required CI pending')
        self.assertEqual(self.store.load().model_dump(), before)
        self.assertNotIn('description', result['items'][0])
        self.assertNotIn('lease_token', str(result))

    def test_member_refused_before_reading_bindings(self):
        self.service.bindings = None
        member = self.actor.model_copy(update={'roles': (MembershipRole.MEMBER,)})
        with self.assertRaises(AuthorizationError):
            self.service.inspect(member, expected_revision=4)

    def test_other_thread_project_and_tenant_never_surface(self):
        rows = [self.binding.model_copy(update=change) for change in (
            {'id': 'thread-other', 'thread_id': 'other'}, {'id': 'project-other', 'project_id': 'other'},
            {'id': 'tenant-other', 'organization_id': 'other'})]
        self.replace(rows)
        self.assertEqual(self.inspect()['items'], [])

    def test_unavailable_or_changed_selection_refuses_before_binding_read(self):
        self.service.bindings = None
        for change in ({'status': 'unavailable'}, {'status': 'profile_unavailable'},
                       {'binding': {'revision': 5}}):
            original = copy.deepcopy(self.selection)
            self.selection.update(change)
            with self.subTest(change=change), self.assertRaises(HomeOperatorConflict):
                self.inspect()
            self.selection = original

    def test_moved_session_and_missing_records_fail_explicitly(self):
        original = self.session
        for session in (None, original.model_copy(update={'project_id': 'other'}),
                        original.model_copy(update={'provider_native_session_id': 'replacement'}),
                        original.model_copy(update={'runtime_id': 'different'})):
            self.session = session
            with self.subTest(session=session), self.assertRaises(HomeOperatorConflict):
                self.inspect()
        self.session = original
        self.goal = None
        with self.assertRaises(HomeOperatorConflict):
            self.inspect()

    def test_reselection_during_read_suppresses_old_result(self):
        def get(key, actor):
            self.selection['binding']['revision'] = 5
            return self.session
        self.service.sessions.get = get
        with self.assertRaisesRegex(HomeOperatorConflict, 'during inspection'):
            self.inspect()

    def test_goal_revision_mismatch_remains_visible_without_rebinding(self):
        self.goal = self.goal.model_copy(update={'revision': 3})
        row = self.inspect()['items'][0]
        self.assertEqual((row['goal_revision'], row['binding_goal_revision']), (3, 1))
        self.assertEqual(self.store.load().bindings[0].goal_revision, 1)

    def test_latest_fifty_are_bounded_without_discarding_history(self):
        self.replace([self.binding.model_copy(update={'id': f'binding-{i:02}', 'updated_at': i + 1.})
                      for i in range(55)])
        result = self.inspect()
        self.assertTrue(result['has_more'])
        self.assertEqual(len(result['items']), 50)
        self.assertEqual(result['items'][0]['binding_id'], 'binding-54')
        self.assertEqual(len(self.store.load().bindings), 55)

    def test_http_uses_authenticated_actor_and_revision_without_model_calls(self):
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from codex_web.api.home import build_home_router
        app = FastAPI()
        actor = self.actor
        @app.middleware('http')
        async def identity(request: Request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)
        app.include_router(build_home_router(None, None, self.service))
        with TestClient(app) as client:
            path = '/api/home/operator/goals'
            self.assertEqual(client.get(path).status_code, 422)
            self.assertEqual(client.get(path + '?expected_revision=0').status_code, 422)
            self.assertEqual(client.get(path + '?expected_revision=5').status_code, 409)
            result = client.get(path + '?expected_revision=4')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['items'][0]['checkpoint_ref'], 'artifact-retained')
            actor = self.actor.model_copy(update={'roles': (MembershipRole.MEMBER,)})
            self.assertEqual(client.get(path + '?expected_revision=4').status_code, 403)

    def test_real_goal_service_checks_home_scope_and_tenant(self):
        from codex_web.goals import GoalWorkGraphBinding
        from codex_web.services.goals import GoalService
        from codex_web.storage.goals import GoalStore
        store = GoalStore(self.sql)
        self.service.bindings.goals = GoalService(store, SimpleNamespace(get=lambda *a: None), None)
        original_load = store.load
        loads = []
        def count_load():
            loads.append(True)
            return original_load()
        store.load = count_load
        for change, accepted in (({'work_graph_bindings': (GoalWorkGraphBinding(project_id='home'),)}, True),
                                 ({'work_graph_bindings': (GoalWorkGraphBinding(project_id='other'),)}, False),
                                 ({'organization_id': 'other'}, False)):
            def apply(state):
                state.goals = [self.goal.model_copy(update=change)]
                return state
            store.update(apply)
            before = store.load().model_dump()
            with self.subTest(change=change):
                if accepted:
                    self.replace([self.binding.model_copy(update={'id': f'binding-{i}'}) for i in range(50)])
                    loads.clear()
                    self.assertEqual(self.inspect()['items'][0]['goal_id'], 'goal')
                    self.assertEqual(len(loads), 1)
                else:
                    with self.assertRaises(HomeOperatorConflict):
                        self.inspect()
                self.assertEqual(store.load().model_dump(), before)

    def test_goal_batch_limit_refuses_before_history_load(self):
        from codex_web.services.goals import GoalService
        service = GoalService(None, None, None)
        with self.assertRaisesRegex(ValueError, 'at most 50'):
            service.get_many(tuple(str(i) for i in range(51)), scope=self.actor.tenant)
        self.assertEqual(service.get_many((), scope=self.actor.tenant), ())
