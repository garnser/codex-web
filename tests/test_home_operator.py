from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.models import HomeOperatorUpdate, IndexedThread, Project, ThreadRunSettings
from codex_web.services.home_operator import HomeOperatorConflict, HomeOperatorService
from codex_web.services.identity import AuthorizationError
from codex_web.services.projects import ProjectNotFoundError, ProjectService
from codex_web.services.thread_scope import ThreadScopeService
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_index import ThreadIndexRepository


class HomeOperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = SQLiteStateStore(root / 'state.db')
        self.repository = ProjectRepository(root / 'projects.json', store=self.store)
        self.repository.save([Project(id='home', name='Home', path=str(root)),
                              Project(id='other', name='Other', path=str(root))])
        self.projects = ProjectService(self.repository)
        self.index = ThreadIndexRepository(self.store, root / 'threads.json')
        self.index.save([IndexedThread(id='operator', name='My operator', project_id='home'),
                         IndexedThread(id='other-thread', name='Other', project_id='other')])
        self.owners = {'operator': ('local', 'default', 'home'),
                       'other-thread': ('local', 'default', 'other')}
        sessions = SimpleNamespace(store=SimpleNamespace(native_owner=lambda key: self.owners.get(key)))
        self.scope = ThreadScopeService(self.projects, sessions, self.index)
        self.settings = {'operator': ThreadRunSettings(execution_profile_id='orchestration-admin',
                                                     sandbox='workspace-write', approval_policy='on-request')}
        self.actor = AuthenticationActor(identity_id='admin', principal_kind=PrincipalKind.HUMAN,
            organization_id='local', workspace_id='default', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.LOCAL_TRUSTED)
        self.service = self.reopen()

    def reopen(self):
        return HomeOperatorService(projects=self.projects, thread_scope=self.scope, thread_index=self.index,
            settings=SimpleNamespace(get=lambda key: self.settings.get(key, ThreadRunSettings())), clock=lambda: 123.0)

    def select(self, thread='operator', revision=0, actor=None):
        return self.service.configure(HomeOperatorUpdate(thread_id=thread, expected_revision=revision), actor or self.actor)

    def test_reopen_preserves_exact_identity_and_current_profile_without_grant(self):
        before_settings = dict(self.settings)
        other = self.projects.get('other').model_dump()
        result = self.select()
        self.assertEqual(result['binding'], {'thread_id': 'operator', 'selected_by': 'admin',
                                            'revision': 1, 'updated_at': 123.0})
        self.assertEqual(self.reopen().status(self.actor), result)
        self.assertEqual(json.loads(self.repository.path.read_text())[0]['home_operator'], result['binding'])
        self.assertEqual(result['thread']['sandbox'], 'workspace-write')
        self.assertEqual(result['thread']['approval_policy'], 'on-request')
        self.assertEqual(self.settings, before_settings)
        self.assertEqual(self.projects.get('other').model_dump(), other)
        self.settings['operator'] = ThreadRunSettings(execution_profile_id='orchestration-only')
        self.assertEqual(self.service.status(self.actor)['thread']['execution_profile_id'], 'orchestration-only')

    def test_member_cannot_select_and_read_does_not_confer_admin(self):
        member = self.actor.model_copy(update={'roles': (MembershipRole.MEMBER,)})
        before = self.store.get('projects')
        with self.assertRaises(AuthorizationError):
            self.select(actor=member)
        self.assertEqual(self.store.get('projects'), before)
        self.assertFalse(self.service.status(member)['can_configure'])

    def test_foreign_or_non_home_thread_refused_without_retarget(self):
        before = self.store.get('projects')
        with self.assertRaises(HTTPException):
            self.select('other-thread')
        self.owners['operator'] = ('foreign', 'default', 'home')
        with self.assertRaises(HTTPException):
            self.select()
        self.assertEqual(self.store.get('projects'), before)

    def test_foreign_actor_cannot_modify_home(self):
        foreign = self.actor.model_copy(update={'organization_id': 'foreign'})
        with self.assertRaises(ProjectNotFoundError):
            self.select(actor=foreign)

    def test_profile_must_be_explicit_and_archived_threads_refused(self):
        self.settings.clear()
        with self.assertRaisesRegex(HomeOperatorConflict, 'profile'):
            self.select()
        self.settings['operator'] = ThreadRunSettings(execution_profile_id='orchestration-only')
        self.index.upsert(IndexedThread(id='operator', name='Old', project_id='home', archived=True))
        with self.assertRaises(HomeOperatorConflict):
            self.select()
        self.assertIsNone(self.projects.get('home').home_operator)

    def test_stale_revision_and_clear_aba_do_not_replace_current_selection(self):
        self.select()
        with self.assertRaisesRegex(HomeOperatorConflict, 'changed'):
            self.select(revision=0)
        result = self.select(None, 1)
        self.assertEqual(result['status'], 'unconfigured')
        self.assertEqual(result['binding']['revision'], 2)
        with self.assertRaises(HomeOperatorConflict):
            self.select(revision=0)
        self.assertEqual(self.select(revision=2)['binding']['revision'], 3)

    def test_unavailable_saved_identity_retained_without_metadata_or_replacement(self):
        self.select()
        self.owners['operator'] = ('foreign', 'default', 'home')
        result = self.service.status(self.actor)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['thread'])
        self.assertEqual(result['binding']['thread_id'], 'operator')
        self.assertEqual(self.projects.get('home').home_operator.revision, 1)

    def test_concurrent_other_project_fields_survive_atomic_selection(self):
        def add_configuration(current):
            return [{**row, 'model': 'current-model'} if row['id'] == 'home' else row for row in current]
        self.store.update('projects', add_configuration, default=[])
        self.select()
        self.assertEqual(self.projects.get('home').model, 'current-model')

    def test_legacy_project_serialization_unchanged_without_binding(self):
        self.assertNotIn('home_operator', self.projects.get('home').model_dump())

    def test_replaced_identity_requires_explicit_reselection(self):
        self.select()
        self.index.remember_replacement('operator', 'replacement', project_id='home',
            provenance={'organization_id': 'local', 'workspace_id': 'default'})
        self.assertEqual(self.service.status(self.actor)['status'], 'unavailable')
        self.assertEqual(self.projects.get('home').home_operator.thread_id, 'operator')
        with self.assertRaisesRegex(HomeOperatorConflict, 'current conversation'):
            self.select(revision=1)

    def test_missing_selected_profile_is_visible_without_changing_reference(self):
        self.select()
        self.settings.clear()
        result = self.service.status(self.actor)
        self.assertEqual(result['status'], 'profile_required')
        self.assertIsNone(result['thread']['execution_profile_id'])
        self.assertEqual(result['binding']['revision'], 1)

    def test_http_mutation_uses_same_admin_and_revision_checks(self):
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from codex_web.api.home import build_home_router
        app = FastAPI()
        actor = self.actor
        @app.middleware('http')
        async def identity(request: Request, call_next):
            request.state.identity_actor = actor
            return await call_next(request)
        app.include_router(build_home_router(None, self.service))
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/home/operator').json()['status'], 'unconfigured')
            self.assertEqual(client.put('/api/home/operator', json={
                'thread_id': 'operator', 'expected_revision': 0}).status_code, 200)
            self.assertEqual(client.put('/api/home/operator', json={
                'thread_id': 'operator', 'expected_revision': 0}).status_code, 409)
            actor = self.actor.model_copy(update={'roles': (MembershipRole.MEMBER,)})
            self.assertEqual(client.put('/api/home/operator', json={
                'thread_id': None, 'expected_revision': 1}).status_code, 403)
        self.assertEqual(self.projects.get('home').home_operator.revision, 1)
