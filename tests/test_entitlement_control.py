from __future__ import annotations

import unittest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from codex_web.api.entitlements import build_entitlements_router
from codex_web.entitlements import (
    CapabilityEntitlementUpdate, EntitlementChangePreview, EntitlementControlUpdate,
    EntitlementMode, QuotaPolicyUpdate, UsageEventCreate,
)
from codex_web.identity import AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.services.entitlement_control import EntitlementAdministration
from codex_web.services.entitlements import EntitlementConflictError
from codex_web.services.identity import AuthorizationError


class EntitlementControlTests(unittest.TestCase):
    def setUp(self):
        from test_entitlements import EntitlementServiceTests
        EntitlementServiceTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.admin = EntitlementAdministration(self.service)
        self.controller = self.actor.model_copy(update={
            'identity_id': 'billing-service', 'principal_kind': PrincipalKind.SERVICE,
            'service_scopes': ('entitlements:admin', 'entitlements:control'),
        })

    def revision(self, actor=None):
        return self.admin.revision(self.service.store.load(), actor or self.actor)

    def take_control(self):
        self.admin.set_control(EntitlementControlUpdate(kind='external', guidance='Contact the plan administrator'), actor=self.controller, expected_revision=self.revision())

    def test_external_controller_blocks_all_human_and_unrelated_service_mutations(self):
        self.take_control()
        for actor in (self.actor, self.controller.model_copy(update={'identity_id': 'other'})):
            for action in (
                lambda: self.service.set_mode(EntitlementMode.SELF_HOSTED_UNLIMITED, actor=actor),
                lambda: self.service.set_capability('external_actions', CapabilityEntitlementUpdate(), actor=actor),
                lambda: self.service.set_quota('attempts', QuotaPolicyUpdate(limit=1), actor=actor),
                lambda: self.admin.retire_quota('attempts', actor=actor, expected_revision=self.revision()),
            ):
                with self.assertRaises(AuthorizationError): action()
        self.service.set_mode(EntitlementMode.ENFORCED, actor=self.controller)
        self.service.set_capability('external_actions', CapabilityEntitlementUpdate(), actor=self.controller)
        self.assertTrue(self.service.check('external_actions', actor=self.actor).allowed)
        self.assertFalse(self.admin.snapshot(self.actor)['can_manage'])

    def test_control_claim_requires_service_scope_and_release_requires_same_identity(self):
        payload = EntitlementControlUpdate(kind='external')
        for actor in (self.actor, self.controller.model_copy(update={'service_scopes': ('entitlements:admin',)})):
            with self.assertRaises(AuthorizationError):
                self.admin.set_control(payload, actor=actor, expected_revision=self.revision())
        self.take_control()
        with self.assertRaises(AuthorizationError):
            self.admin.set_control(EntitlementControlUpdate(kind='local'), actor=self.controller.model_copy(update={'identity_id': 'other'}), expected_revision=self.revision())
        self.admin.set_control(EntitlementControlUpdate(kind='local'), actor=self.controller, expected_revision=self.revision())
        self.assertTrue(self.admin.snapshot(self.actor)['can_manage'])

    def test_provenance_and_enforcement_mode_do_not_invent_external_control(self):
        self.service.set_capability('external_actions', CapabilityEntitlementUpdate(source='hosted-billing'), actor=self.actor)
        self.service.set_mode(EntitlementMode.ENFORCED, actor=self.actor)
        self.assertEqual(self.admin.snapshot(self.actor)['control']['kind'], 'local')
        self.assertTrue(self.admin.snapshot(self.actor)['can_manage'])

    def test_preview_and_stale_configuration_fail_without_lost_update(self):
        proposal = EntitlementChangePreview(kind='quota', key='attempts', quota=QuotaPolicyUpdate(limit=5))
        impact = self.admin.preview(proposal, actor=self.actor)
        self.service.set_quota('attempts', QuotaPolicyUpdate(limit=2), actor=self.actor)
        with self.assertRaises(EntitlementConflictError):
            self.service.set_quota('attempts', proposal.quota, actor=self.actor, expected_revision=impact['expected_revision'])
        self.assertEqual(self.service.quotas(self.actor)[0].limit, 2)
        with self.assertRaises(EntitlementConflictError):
            self.admin.set_control(EntitlementControlUpdate(kind='external'), actor=self.controller, expected_revision=impact['expected_revision'])

    def test_preview_reports_usage_and_retirement_preserves_ledger(self):
        self.service.record_usage(UsageEventCreate(idempotency_key='one', metric='attempts', amount=4), actor=self.actor)
        self.service.set_quota('attempts', QuotaPolicyUpdate(limit=8), actor=self.actor)
        impact = self.admin.preview(EntitlementChangePreview(kind='quota', key='attempts', quota=QuotaPolicyUpdate(limit=2)), actor=self.actor)
        self.assertEqual(impact['current_usage'], 4)
        self.assertEqual(impact['previous']['limit'], 8)
        self.assertEqual(impact['proposed']['limit'], 2)
        self.admin.retire_quota('attempts', actor=self.actor, expected_revision=self.revision())
        self.assertEqual(self.service.quotas(self.actor), [])
        self.assertEqual(len(self.service.usage(self.actor)), 1)

    def test_tenant_scoped_revision_and_ownership_do_not_leak_or_block_neighbors(self):
        neighbor = self.actor.model_copy(update={'workspace_id': 'other'})
        before = self.revision(neighbor)
        self.take_control()
        self.assertEqual(self.revision(neighbor), before)
        self.service.set_mode(EntitlementMode.ENFORCED, actor=neighbor)
        snapshot = self.admin.snapshot(neighbor)
        self.assertEqual(snapshot['control']['kind'], 'local')
        self.assertNotIn('billing-service', str(snapshot))

    def test_migration_preserves_legacy_and_future_version_fails_closed(self):
        raw = {'schema_version': '1.0', 'settings': [], 'capabilities': [], 'quotas': [], 'usage': []}
        self.service.store.store.put(self.service.store.namespace, raw)
        self.assertEqual(self.service.store.load().schema_version, '1.1')
        self.assertEqual(self.service.store.store.get(self.service.store.namespace)['schema_version'], '1.0')
        self.service.set_mode(EntitlementMode.ENFORCED, actor=self.actor)
        self.assertEqual(self.service.store.store.get(self.service.store.namespace)['schema_version'], '1.1')
        with self.assertRaises(Exception): self.service.store._decode({**raw, 'schema_version': '9.0'})

    def client(self, projects=None):
        app = FastAPI()
        @app.middleware('http')
        async def actor(request: Request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_entitlements_router(self.service, projects))
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def test_api_assurance_project_validation_and_external_read_only(self):
        client = self.client()
        self.actor = self.actor.model_copy(update={'assurance': AuthenticationAssurance.PRIMARY})
        self.assertFalse(client.get('/api/entitlements/administration').json()['can_manage'])
        self.assertEqual(client.post('/api/entitlements/administration/preview', json={'kind': 'mode', 'mode': {'mode': 'enforced'}}).status_code, 403)
        self.actor = self.actor.model_copy(update={'assurance': AuthenticationAssurance.MFA})
        self.assertEqual(client.get('/api/entitlements/administration?project_id=missing').status_code, 404)
        self.assertEqual(client.put('/api/entitlements/mode?project_id=missing', json={'mode': 'enforced'}).status_code, 404)
        self.take_control()
        self.assertFalse(client.get('/api/entitlements/administration').json()['can_manage'])
        self.assertEqual(client.put('/api/entitlements/mode', json={'mode': 'self_hosted_unlimited'}).status_code, 403)
        self.assertEqual(client.put('/api/entitlements/control', params={'expected_revision': self.revision()}, json={'kind': 'local'}).status_code, 403)

    def test_api_preview_validates_final_domain_and_cas_returns_conflict(self):
        client = self.client()
        self.assertEqual(client.post('/api/entitlements/administration/preview', json={'kind': 'capability', 'key': 'external_actions', 'capability': {'starts_at': 20, 'expires_at': 10}}).status_code, 422)
        impact = client.post('/api/entitlements/administration/preview', json={'kind': 'mode', 'mode': {'mode': 'enforced'}}).json()
        result = client.put('/api/entitlements/mode', params={'expected_revision': impact['expected_revision']}, json={'mode': 'enforced'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(client.put('/api/entitlements/mode', params={'expected_revision': impact['expected_revision']}, json={'mode': 'self_hosted_unlimited'}).status_code, 409)
        self.assertEqual(client.get('/api/entitlements/administration').json()['mode'], 'enforced')

    def test_member_and_controller_without_admin_scope_cannot_edit(self):
        member = self.actor.model_copy(update={'roles': (MembershipRole.MEMBER,)})
        self.assertFalse(self.admin.snapshot(member)['can_manage'])
        self.take_control()
        without_admin = self.controller.model_copy(update={'service_scopes': ('entitlements:control',)})
        with self.assertRaises(AuthorizationError): self.service.set_mode(EntitlementMode.ENFORCED, actor=without_admin)

    def test_project_views_inherit_workspace_values_and_foreign_projects_fail_closed(self):
        from types import SimpleNamespace
        from codex_web.models import Project
        from codex_web.services.projects import ProjectService
        records = [Project(id=key, name=key, path=self.temp.name,
                           organization_id=self.actor.organization_id,
                           workspace_id=workspace) for key, workspace in [
                               ('first', self.actor.workspace_id), ('second', self.actor.workspace_id), ('foreign', 'other')]]
        projects = ProjectService(SimpleNamespace(load=lambda: records))
        client = self.client(projects)
        first = client.get('/api/entitlements/administration?project_id=first').json()
        second = client.get('/api/entitlements/administration?project_id=second').json()
        self.assertEqual(first['revision'], second['revision'])
        self.assertEqual(first['scope'], 'workspace')
        response = client.put('/api/entitlements/mode', params={'project_id': 'first', 'expected_revision': first['revision']}, json={'mode': 'enforced'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get('/api/entitlements/administration?project_id=second').json()['mode'], 'enforced')
        for path in ['administration', 'mode', 'capabilities', 'quotas', 'usage']:
            self.assertEqual(client.get('/api/entitlements/' + path + '?project_id=foreign').status_code, 404)
        self.assertEqual(client.put('/api/entitlements/mode?project_id=foreign', json={'mode': 'self_hosted_unlimited'}).status_code, 404)
        self.assertEqual(self.service.mode(self.actor), EntitlementMode.ENFORCED)
