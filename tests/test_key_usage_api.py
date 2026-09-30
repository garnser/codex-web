from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from fastapi import FastAPI
from fastapi.testclient import TestClient

from codex_web.api.crypto_keys import build_crypto_keys_router
from codex_web.crypto import ManagedKeyCreate
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, MembershipRole, PrincipalKind
from codex_web.key_backends import LocalFileKeyBackend
from codex_web.models import Project
from codex_web.recovery import RecoveryPolicy
from codex_web.services.crypto_keys import CryptoKeyService
from codex_web.services.projects import ProjectService
from codex_web.storage.crypto_keys import CryptoKeyStore
from codex_web.storage.recovery import RecoveryStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class KeyUsageApiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.store = SQLiteStateStore(root / 'state.sqlite3')
        self.service = CryptoKeyService(CryptoKeyStore(self.store), {'local': LocalFileKeyBackend(root / 'keys')})
        self.actor = AuthenticationActor(identity_id='admin', principal_kind=PrincipalKind.HUMAN,
            organization_id='org', workspace_id='ws', roles=(MembershipRole.ADMIN,), assurance=AuthenticationAssurance.MFA)
        projects = [Project(id=id, name=id, path='/' + id, organization_id='foreign' if id == 'foreign' else 'org', workspace_id='ws') for id in ['a', 'b', 'foreign']]
        self.keys = {scope: self.service.create_key(ManagedKeyCreate(project_id=scope, purpose='backup'), actor=self.actor) for scope in [None, 'a', 'b']}
        resources = NS(project_resources=lambda project, **_: [NS(id='resource-' + project.id)])
        app = FastAPI()
        @app.middleware('http')
        async def identity(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        app.include_router(build_crypto_keys_router(self.service, ProjectService(NS(load=lambda: projects)), resources))
        self.client = TestClient(app); self.addCleanup(self.client.close)

    def test_project_lists_inherited_keys_and_filters_metadata_manifest_and_audit(self):
        for path in ['/keys', '/manifest', '/events']:
            response = self.client.get('/api/crypto' + path + '?project_id=a')
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(self.keys['b'].id, response.text)
        self.assertEqual({row['id'] for row in self.client.get('/api/crypto/keys?project_id=a').json()['items']}, {self.keys[None].id, self.keys['a'].id})
        self.assertEqual(self.client.get(f'/api/crypto/keys/{self.keys[None].id}/usage?project_id=a').status_code, 200)

    def test_project_and_key_scope_fail_before_mutation(self):
        for project in ['b', 'foreign', 'missing', '']:
            base = f'/api/crypto/keys/{self.keys["a"].id}'
            query = '?project_id=' + project
            self.assertEqual(self.client.get(base + '/usage' + query).status_code, 404)
            for suffix, payload in [('/rotate', None), ('/revoke', {'reason': 'deny'}), ('/versions/1/revoke', {'reason': 'deny'})]:
                self.assertEqual(self.client.post(base + suffix + query, json=payload).status_code, 404)
        self.assertEqual(self.service.get_key(self.keys['a'].id, self.actor).current_version, 1)
        for payload in [{'project_id': 'b'}, {'resource_id': 'resource-b'}]:
            self.assertEqual(self.client.post('/api/crypto/keys?project_id=a', json=payload).status_code, 404)

    def test_usage_is_reference_only_and_server_blocks_retirement_of_current_policy_key(self):
        key = self.keys[None]
        RecoveryStore(self.store).update(lambda state: state.model_copy(update={'policy': RecoveryPolicy(backup_key_id=key.id)}))
        response = self.client.get(f'/api/crypto/keys/{key.id}/usage?project_id=a')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['blocking_count'], 1)
        self.assertNotIn(key.versions[0].backend_ref, response.text)
        self.assertNotIn('ciphertext', response.text)
        self.assertEqual(self.client.post(f'/api/crypto/keys/{key.id}/revoke?project_id=a', json={'reason': 'retire'}).status_code, 409)
        self.assertEqual(self.client.post(f'/api/crypto/keys/{key.id}/rotate?project_id=a').status_code, 200)
        self.assertEqual(self.client.get(f'/api/crypto/keys/{key.id}/usage?version=1&project_id=a').json()['count'], 0)

    def test_scan_failure_and_corrupt_state_never_report_empty_impact(self):
        key = self.keys[None]
        self.store.update('recovery', lambda _: {'invalid': 'do-not-echo'}, default={})
        for suffix, method in [('/usage', 'get'), ('/revoke', 'post')]:
            options = {'json': {'reason': 'retire'}} if method == 'post' else {}
            response = getattr(self.client, method)(f'/api/crypto/keys/{key.id}' + suffix, **options)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('do-not-echo', response.text)

    def test_admin_and_tenant_authority_remain_required(self):
        key_id = self.keys[None].id
        self.actor = self.actor.model_copy(update={'roles': (MembershipRole.MEMBER,)})
        self.assertEqual(self.client.get(f'/api/crypto/keys/{key_id}/usage').status_code, 403)
        self.actor = self.actor.model_copy(update={'roles': (MembershipRole.ADMIN,), 'organization_id': 'other'})
        self.assertEqual(self.client.get(f'/api/crypto/keys/{key_id}/usage').status_code, 404)

    def test_unscoped_manifest_validation_preserves_missing_reference_result(self):
        response = self.client.post('/api/crypto/manifest/validate', json={'entries': [{'key_id': 'missing', 'versions': [1]}]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['valid'])
        self.assertTrue(response.json()['missing_refs'])
