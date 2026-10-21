import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from codex_web.code_hosts import CodeHostCapability, CodeHostError, CodeHostProviderBinding, CodeHostUnsupportedCapabilityError
from codex_web.identity import TenantScope
from codex_web.integrations.github_client import GitHubClient
from codex_web.resources import ResourceAlias, ResourceCreate, ResourceProvenance, ResourceType, ResourceUpdate
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate, SecretRotate
from codex_web.services.code_hosts import CodeHostRegistry, CodeHostService
from codex_web.services.github_code_host import GitHubCodeHostProvider
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
from codex_web.services.identity import IdentityService, TenantIsolationError
from codex_web.services.resources import ResourceCatalogService
from codex_web.services.secrets import SecretBroker, SecretUseDeniedError
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.resource_catalog import ResourceCatalogStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_github_merge_requirements as provider_fixtures


class MergeRequirementServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = SQLiteStateStore(root / 'state.sqlite3')
        self.identity = IdentityService(IdentityStateStore(self.state))
        self.identity.bootstrap_local()
        self.admin = self.identity.local_trusted_actor()
        self.worker = self.identity.bootstrap_service_actor(identity_id='merge-reader', name='Merge reader',
            scope=TenantScope(), service_scopes=('secret:use',))
        self.resources = ResourceCatalogService(ResourceCatalogStore(self.state))
        self.server = provider_fixtures._Provider()
        self.resource = self.resources.create(ResourceCreate(name='acme/widgets', resource_type=ResourceType.REPOSITORY,
            aliases=[ResourceAlias(namespace='provider', provider='github', value='acme/widgets')],
            provenance=ResourceProvenance(provider='github', provider_instance='github.example',
                                          external_id=str(self.server.repository_id))), actor=self.admin)
        self.secret = SecretBroker(SecretStateStore(self.state), {'local': LocalFileSecretBackend(root / 'secrets')})
        self.reference = self.secret.create(SecretCreate(name='Read credential', value='fixture-token',
            provider='github', purpose='code-host', allowed_identity_ids=[self.worker.identity_id]), actor=self.admin)
        self.registry = CodeHostRegistry()
        self.registry.register_provider(GitHubCodeHostProvider(GitHubClient(transport=httpx.MockTransport(self.server.response))))
        self.binding = CodeHostProviderBinding(id='canonical-read', organization_id='local', workspace_id='default',
            provider_type='github', provider_instance='github.example', base_url='https://api.github.example',
            credential_ref=self.reference.id, capabilities=(CodeHostCapability.MERGE_REQUIREMENTS_READ,))
        self.registry.register_binding(self.binding)
        self.service = CodeHostService(self.registry, self.resources, secrets=self.secret, identity=self.identity)

    async def read(self, **changes):
        return await self.service.merge_requirements(self.binding.id, self.resource.id, '7',
            actor=changes.pop('actor', self.worker), **changes)

    def during_status(self, change):
        changed = False
        def hook(request, n):
            nonlocal changed
            if request.url.path.endswith('/status') and not changed:
                changed = True
                change()
        self.server.hook = hook

    async def test_real_canonical_secret_use_is_audited_without_material_in_result(self):
        fact = await self.read()
        self.assertTrue(fact.complete)
        self.assertEqual(fact.resource_id, self.resource.id)
        self.assertNotIn('fixture-token', fact.model_dump_json())
        events = self.secret.store.load().audit
        self.assertTrue(any(event.action == 'use' and event.outcome == 'succeeded' for event in events))
        self.assertNotIn('fixture-token', json.dumps([event.model_dump(mode='json') for event in events]))

    async def test_provider_secret_reflection_is_scrubbed_by_the_real_broker(self):
        self.server.runs[0]['name'] = 'fixture-token'
        result = await self.read()
        self.assertNotIn('fixture-token', result.model_dump_json())
        self.assertIn('[REDACTED]', result.model_dump_json())

    async def test_worker_cannot_borrow_requester_admin_secret_rights(self):
        def deny(state):
            state.references = [item.model_copy(update={'allowed_identity_ids': []}) for item in state.references]
            return state
        self.secret.store.update(deny)
        with self.assertRaises(SecretUseDeniedError):
            await self.read()
        self.assertFalse(self.server.calls)
        self.assertTrue((await self.read(actor=self.admin)).complete)

    async def test_revocation_and_rotation_during_reads_prevent_return(self):
        self.during_status(lambda: self.secret.revoke(self.reference.id, actor=self.admin))
        with self.assertRaises(SecretUseDeniedError):
            await self.read()
        self.secret.rotate(self.reference.id, SecretRotate(value='fixture-token'), actor=self.admin)
        self.during_status(lambda: self.secret.rotate(self.reference.id, SecretRotate(value='replacement-token'), actor=self.admin))
        with self.assertRaises(CodeHostError):
            await self.read()

    async def test_expiry_without_metadata_change_is_not_healthy_reuse(self):
        now = [time.time()]
        self.reference = self.secret.create(SecretCreate(name='Expiring', value='fixture-token',
            expires_at=now[0] + 10, allowed_identity_ids=[self.worker.identity_id]), actor=self.admin)
        self.registry.register_binding(self.binding.model_copy(update={'credential_ref': self.reference.id}))
        self.during_status(lambda: now.__setitem__(0, now[0] + 20))
        before = self.secret.metadata(self.reference.id, actor=self.admin)
        with patch('codex_web.secrets.time.time', side_effect=lambda: now[0]):
            with self.assertRaises(SecretUseDeniedError):
                await self.read()
        self.assertEqual(self.secret.metadata(self.reference.id, actor=self.admin), before)

    async def test_binding_and_resource_changes_cannot_return_old_scope_facts(self):
        self.during_status(lambda: self.registry.register_binding(self.binding.model_copy(update={'base_url': 'https://other.example'})))
        with self.assertRaises(CodeHostError):
            await self.read()
        self.registry.register_binding(self.binding)
        self.during_status(lambda: self.resources.update(self.resource.id,
            ResourceUpdate(provenance=self.resource.provenance.model_copy(update={'provider_instance': 'other.example'})), actor=self.admin))
        with self.assertRaises(CodeHostError):
            await self.read()

    async def test_foreign_tenant_and_provider_instance_fail_before_credential_use(self):
        with self.assertRaises(TenantIsolationError):
            await self.read(actor=self.worker.model_copy(update={'workspace_id': 'other'}))
        self.registry.register_binding(self.binding.model_copy(update={'provider_instance': 'other.example'}))
        with self.assertRaises(CodeHostError):
            await self.read()
        self.assertFalse(self.server.calls)

    async def test_missing_declaration_and_unsupported_provider_fail_before_material_resolution(self):
        self.registry.register_binding(self.binding.model_copy(update={'capabilities': ()}))
        with self.assertRaises(CodeHostUnsupportedCapabilityError):
            await self.read()
        self.registry.register_provider(GitLabCodeHostProvider())
        gitlab = self.registry.provider('gitlab')
        self.assertIn(CodeHostCapability.MERGE_REQUIREMENTS_READ, gitlab.capabilities())
        with self.assertRaises(CodeHostError):
            await gitlab.merge_requirements(self.binding, self.resource, '7', credential=None)
        self.assertFalse(self.server.calls)

    async def test_canonical_checkpoint_failure_prevents_provider_or_result_escape(self):
        calls = []
        async def denied():
            calls.append(True)
            raise PermissionError('current assignment denied')
        with self.assertRaises(PermissionError):
            await self.read(checkpoint=denied)
        self.assertEqual(calls, [True])
        self.assertFalse(self.server.calls)
