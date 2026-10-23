from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_web.definitions import DefinitionReference
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.models import Project
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.projects import ProjectService
from codex_web.services.skill_scanners import SkillScannerRegistry
from codex_web.services.skill_security import SkillSecurityBlocked, SkillSecurityService
from codex_web.services.skills import SkillConflict, SkillNotFound, SkillService
from codex_web.skill_security import SkillScanProviderResult, SkillScanRequest
from codex_web.skills import SkillCreate, SkillOrigin, SkillProvenance, SkillPublish, SkillUpdate
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.projects import ProjectRepository
from codex_web.storage.skill_security import SkillSecurityStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.workspaces import WorkspaceMapper


class PassingScanner:
    provider_id = 'nvidia-skillspector'

    def scan(self, *, content, mode):
        return SkillScanProviderResult(scanner_version='fixture-1', risk_score=0)


class ProjectScopedSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = SQLiteStateStore(self.root / 'state.sqlite3')
        self.definitions = DefinitionRegistryService(DefinitionRegistryStore(self.state))
        repository = ProjectRepository(self.root / 'projects.json', store=self.state,
            workspace_mapper=WorkspaceMapper(root=self.root))
        repository.save([
            Project(id='vdo', name='VDO', path=str(self.root), organization_id='org-a', workspace_id='workspace-a'),
            Project(id='home', name='Home', path=str(self.root), organization_id='org-a', workspace_id='workspace-a'),
            Project(id='foreign', name='Foreign', path=str(self.root), organization_id='org-a', workspace_id='workspace-b'),
        ])
        self.projects = ProjectService(repository)
        self.skills = SkillService(self.definitions, projects=self.projects, clock=lambda: 100.0)
        registry = SkillScannerRegistry()
        registry.register(PassingScanner())
        self.security = SkillSecurityService(SkillSecurityStore(self.state), self.definitions, registry, clock=lambda: 100.0)
        self.skills.bind_security(self.security)
        self.actor = AuthenticationActor(identity_id='admin', principal_kind=PrincipalKind.HUMAN,
            organization_id='org-a', workspace_id='workspace-a', roles=(MembershipRole.ADMIN,),
            assurance=AuthenticationAssurance.MFA)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, project_id='vdo', skill_id='closure'):
        return self.skills.create(SkillCreate(skill_id=skill_id, project_id=project_id,
            name='MR closure', instructions='Review the exact existing MR.',
            provenance=SkillProvenance(origin=SkillOrigin.IMPORTED, source_type='fixture')),
            actor=self.actor)

    def publish(self, draft):
        self.security.scan(draft, SkillScanRequest(), actor=self.actor)
        return self.skills.publish(draft['skillId'], draft['recordId'], SkillPublish(), actor=self.actor)

    def test_project_lifecycle_security_and_exact_runtime_context(self):
        draft = self.create()
        self.assertEqual((draft['scopeType'], draft['scopeId']), ('project', 'vdo'))
        self.definitions.validate(draft['recordId'], actor=self.actor.identity_id)
        with self.assertRaisesRegex(SkillSecurityBlocked, 'current_revision_not_scanned'):
            self.skills.publish(draft['skillId'], draft['recordId'], SkillPublish(), actor=self.actor)
        published = self.publish(draft)
        ref = DefinitionReference.model_validate(published['definitionReference'])
        self.assertEqual(self.skills.validate_reference(ref, actor=self.actor, project_id='vdo'), ref)
        self.assertEqual(self.skills.validate_reference_for_scope(ref, organization_id='org-a',
            workspace_id='workspace-a', project_id='vdo'), ref)
        self.assertEqual(self.skills.requirements_for_refs_scoped((ref,), organization_id='org-a',
            workspace_id='workspace-a', project_id='vdo'), ((), ()))
        context = self.skills.context_for_refs_scoped((ref,), organization_id='org-a',
            workspace_id='workspace-a', project_id='vdo', objective='Review MR')
        self.assertIsNotNone(context)
        for project_id in (None, 'home', 'foreign', 'missing'):
            with self.subTest(project_id=project_id), self.assertRaises(SkillNotFound):
                self.skills.validate_reference(ref, actor=self.actor, project_id=project_id)
        with self.assertRaises(SkillNotFound):
            self.skills.validate_reference_for_scope(ref, organization_id='org-a',
                workspace_id='workspace-b', project_id='vdo')
        updated = self.skills.update('closure', SkillUpdate(instructions='Updated review.', reason='Revise instructions'), actor=self.actor)
        self.assertEqual((updated['scopeType'], updated['scopeId']), ('project', 'vdo'))
        with self.assertRaises(SkillSecurityBlocked):
            self.skills.publish('closure', updated['recordId'], SkillPublish(), actor=self.actor)

    def test_unknown_foreign_and_removed_projects_never_grant_visibility(self):
        for project_id in ('missing', 'foreign'):
            with self.subTest(project_id=project_id), self.assertRaises(SkillNotFound):
                self.create(project_id)
        published = self.publish(self.create())
        ref = DefinitionReference.model_validate(published['definitionReference'])
        self.state.put('projects', [p.model_dump(mode='json') for p in self.projects.list() if p.id != 'vdo'])
        self.assertEqual(self.skills.list(actor=self.actor), [])
        with self.assertRaises(SkillNotFound):
            self.skills.get('closure', actor=self.actor)
        with self.assertRaises(SkillNotFound):
            self.skills.validate_reference(ref, actor=self.actor, project_id='vdo')

    def test_project_browse_excludes_siblings_and_preserves_workspace_skill(self):
        self.create('vdo')
        self.create('home', 'home-only')
        self.create(None, 'shared')
        self.assertEqual({s['skillId'] for s in self.skills.list(actor=self.actor, project_id='vdo')}, {'closure', 'shared'})
        self.assertEqual({s['skillId'] for s in self.skills.list(actor=self.actor, project_id='home')}, {'home-only', 'shared'})
        self.assertEqual(self.skills.get('closure', actor=self.actor, project_id='vdo')['scopeId'], 'vdo')
        with self.assertRaises(SkillNotFound):
            self.skills.get('closure', actor=self.actor, project_id='home')
        for project_id in ('missing', 'foreign', ''):
            with self.subTest(project_id=project_id), self.assertRaises(SkillNotFound):
                self.skills.list(actor=self.actor, project_id=project_id)

    def test_same_name_in_different_scope_requires_explicit_selection(self):
        self.create('vdo')
        self.create('home')
        with self.assertRaises(SkillConflict):
            self.skills.get('closure', actor=self.actor)
        self.assertEqual(self.skills.get('closure', actor=self.actor, project_id='vdo')['scopeId'], 'vdo')
        self.assertEqual(self.skills.get('closure', actor=self.actor, project_id='home')['scopeId'], 'home')
        with self.assertRaises(SkillConflict):
            self.skills.get('closure', actor=self.actor, revision=1)
        self.assertEqual(self.skills.get('closure', actor=self.actor, project_id='vdo', revision=1)['scopeId'], 'vdo')
        self.create(None)
        self.assertEqual(self.skills.get('closure', actor=self.actor, project_id='vdo', revision=1)['scopeId'], 'vdo')
        self.assertEqual(self.skills.export_bundle('closure', actor=self.actor, project_id='vdo', revision=1)['manifest']['project_id'], None)

    def test_http_project_authoring_and_scope_denials(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from codex_web.api.skills import build_skills_router
        from codex_web.api.skill_security import build_skill_security_router
        app = FastAPI()

        @app.middleware('http')
        async def current_actor(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)

        app.include_router(build_skills_router(self.skills))
        app.include_router(build_skill_security_router(self.security, self.skills))
        with TestClient(app) as client:
            response = client.post('/api/skills', json={'skill_id': 'http-closure',
                'project_id': 'vdo', 'name': 'Closure', 'instructions': 'Review MR'})
            self.assertEqual(response.status_code, 200, response.text)
            draft = response.json()['item']
            record_id = draft['recordId']
            self.assertEqual(client.get('/api/skills?project_id=home').json()['count'], 0)
            self.assertEqual(client.get('/api/skills/http-closure?project_id=home').status_code, 404)
            self.assertEqual(client.get('/api/skills/http-closure/export?project_id=home').status_code, 404)
            self.assertEqual(client.get('/api/skills/http-closure/export?project_id=home&revision=1').status_code, 404)
            self.assertEqual(client.post(f'/api/skills/http-closure/revisions/{record_id}/scan', json={}).status_code, 200)
            published = client.post(f'/api/skills/http-closure/revisions/{record_id}/publish?project_id=vdo', json={})
            self.assertEqual(published.status_code, 200, published.text)
            denied = client.patch('/api/skills/http-closure?project_id=home', json={
                'instructions': 'Wrong Project', 'reason': 'Must refuse'})
            self.assertEqual(denied.status_code, 404, denied.text)
            changed = client.patch('/api/skills/http-closure?project_id=vdo', json={
                'instructions': 'Current Project', 'reason': 'Revise'})
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(changed.json()['item']['scopeId'], 'vdo')
