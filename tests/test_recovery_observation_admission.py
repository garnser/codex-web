"""Actual guarded writes distinguish observations from credential changes."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_web.identity import MembershipRole, MembershipUpdate
from codex_web.secret_backends import LocalFileSecretBackend
from codex_web.secrets import SecretCreate, SecretRotate
from codex_web.services.identity import IdentityService
from codex_web.services.secrets import SecretBroker
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.secret_state import SecretStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_recovery_storage_operator as fixture


class LocalBootstrapReadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SQLiteStateStore(Path(self.temporary.name) / 'identity.sqlite')
        self.identity = IdentityService(IdentityStateStore(self.store))

    def test_complete_bootstrap_does_not_advance_authorization_revision(self):
        original = self.identity.bootstrap_local()
        revision = self.store.namespace_revision('identity_state')
        self.identity.local_trusted_actor()
        self.identity.bootstrap_local()
        self.assertEqual(self.identity.state(), original)
        self.assertEqual(self.store.namespace_revision('identity_state'), revision)

    def test_existing_restricted_membership_is_not_elevated_or_rewritten(self):
        actor = self.identity.local_trusted_actor()
        membership = self.identity.state().memberships[0]
        self.identity.update_membership(membership.id, MembershipUpdate(roles=[MembershipRole.MEMBER]), actor=actor)
        revision = self.store.namespace_revision('identity_state')
        self.assertEqual(self.identity.local_trusted_actor().roles, (MembershipRole.MEMBER,))
        self.assertEqual(self.store.namespace_revision('identity_state'), revision)

    def test_missing_defaults_still_initialize_atomically(self):
        self.identity.bootstrap_local()
        self.identity.store.update(lambda state: state.model_copy(update={'humans': [], 'memberships': []}))
        actor = self.identity.local_trusted_actor()
        state = self.identity.state()
        self.assertEqual(len(state.humans), 1)
        self.assertEqual(len(state.memberships), 1)
        self.assertIn(MembershipRole.ADMIN, actor.roles)


class ServiceBootstrapReadTests(unittest.TestCase):
    setUp = LocalBootstrapReadTests.setUp

    def bootstrap(self):
        scope = self.identity.local_trusted_actor().tenant
        return self.identity.bootstrap_service_actor(identity_id='fixture-internal',
            name='Fixture internal service', scope=scope, service_scopes=('secret:use',))

    def test_complete_service_bootstrap_does_not_rewrite_identity(self):
        actor = self.bootstrap()
        original = self.identity.state()
        revision = self.store.namespace_revision('identity_state')
        self.assertEqual(self.bootstrap().model_dump(exclude={'authenticated_at'}),
            actor.model_dump(exclude={'authenticated_at'}))
        self.assertEqual(self.identity.state(), original)
        self.assertEqual(self.store.namespace_revision('identity_state'), revision)

    def test_existing_service_membership_roles_are_preserved(self):
        actor = self.bootstrap()
        admin = self.identity.local_trusted_actor()
        membership = next(m for m in self.identity.state().memberships if m.identity_id == actor.identity_id)
        self.identity.update_membership(membership.id, MembershipUpdate(roles=[MembershipRole.APPROVER]), actor=admin)
        revision = self.store.namespace_revision('identity_state')
        self.assertEqual(self.bootstrap().roles, (MembershipRole.APPROVER,))
        self.assertEqual(self.store.namespace_revision('identity_state'), revision)

    def test_missing_service_or_membership_still_initializes_atomically(self):
        for missing in ('services', 'memberships'):
            with self.subTest(missing=missing):
                actor = self.bootstrap()
                self.identity.store.update(lambda state: state.model_copy(update={missing: [v for v in getattr(state, missing) if (v.id if missing == 'services' else v.identity_id) != actor.identity_id]}))
                restored = self.bootstrap()
                state = self.identity.state()
                self.assertEqual(restored.identity_id, actor.identity_id)
                self.assertEqual(sum(v.id == actor.identity_id for v in state.services), 1)
                self.assertEqual(sum(v.identity_id == actor.identity_id for v in state.memberships), 1)


class RecoveryObservationAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixture.StorageOperatorTests.asyncTearDown
    publish = fixture.StorageOperatorTests.publish
    insert_opaque = fixture.StorageOperatorTests.insert_opaque
    client = fixture.StorageOperatorTests.client
    body = fixture.StorageOperatorTests.body
    committed = fixture.StorageOperatorTests.committed
    capture = fixture.StorageOperatorTests.capture
    assert_original = fixture.StorageOperatorTests.assert_original

    async def asyncSetUp(self):
        await fixture.StorageOperatorTests.asyncSetUp(self)
        self.broker = SecretBroker(SecretStateStore(self.sqlite), {'local': LocalFileSecretBackend(self.root / 'secrets')})
        self.execution.secret_broker = self.broker
        self.secret = self.broker.create(SecretCreate(name='Synthetic observer credential', value='fixture-private-value'), actor=self.actor)

    def observe(self):
        self.identity.local_trusted_actor()
        return self.broker.use(self.secret.id, actor=self.actor, operation='synthetic-background-observation', consumer=lambda value: len(value))

    async def test_internal_service_bootstrap_allows_capture_and_same_artifact_verify(self):
        def bootstrap():
            return self.identity.bootstrap_service_actor(identity_id='fixture-readiness',
                name='Synthetic readiness observer', scope=self.actor.tenant,
                service_scopes=('secret:use',))
        bootstrap()
        operations = self.operator.storage_provider.operations
        inventory, assess = operations.inventory, operations._assess
        def observed_inventory(*args, **kwargs):
            bootstrap()
            return inventory(*args, **kwargs)
        def observed_assessment(*args, **kwargs):
            report = assess(*args, **kwargs)
            bootstrap()
            return report
        with patch.object(operations, 'inventory', side_effect=observed_inventory), patch.object(operations, '_assess', side_effect=observed_assessment):
            await fixture.StorageOperatorTests.test_capture_verify_same_backend_once_preserves_invalid_rows_without_health_pass(self)

    async def test_audit_appends_and_bootstrap_reads_allow_capture_and_same_artifact_verify(self):
        references = self.broker.store.load().references
        before = len(self.broker.store.load().audit)
        operations = self.operator.storage_provider.operations
        inventory, assess = operations.inventory, operations._assess
        def observed_inventory(*args, **kwargs):
            self.observe()
            return inventory(*args, **kwargs)
        def observed_assessment(*args, **kwargs):
            report = assess(*args, **kwargs)
            self.observe()
            return report
        with patch.object(operations, 'inventory', side_effect=observed_inventory), patch.object(operations, '_assess', side_effect=observed_assessment):
            await fixture.StorageOperatorTests.test_capture_verify_same_backend_once_preserves_invalid_rows_without_health_pass(self)
        state = self.broker.store.load()
        self.assertEqual(state.references, references)
        self.assertGreater(len(state.audit), before)
        self.assertTrue(any(event.action == 'use' for event in state.audit[before:]))

    async def assert_change_denied(self, change, *, secret=True, message=None):
        operations = self.operator.storage_provider.operations
        inventory = operations.inventory
        def changed_inventory():
            change()
            return inventory()
        with patch.object(operations, 'inventory', side_effect=changed_inventory):
            async with self.client() as client:
                response = await client.post('/api/recovery/storage-preservation/operations', json=self.body())
        self.assertEqual(response.status_code, 200, response.text)
        intent = response.json()['item']
        self.assertEqual(intent['status'], 'failed', response.text)
        self.assertEqual(intent['failure']['outcome'], 'known')
        self.assertIn(message or ('Recovery secret authority changed' if secret else 'Recovery admission changed'), intent['last_error'])
        self.assertFalse(self.committed())
        self.assertFalse([a for a in self.artifacts.store.load().artifacts if a.source == 'recovery-storage-preservation.v1'])
        self.assert_original()

    async def test_rotation_still_refuses_before_capture(self):
        await self.assert_change_denied(lambda: self.broker.rotate(self.secret.id, SecretRotate(value='fixture-new-value'), actor=self.actor))

    async def test_rotate_back_to_original_material_still_refuses(self):
        def rotate_back():
            self.broker.rotate(self.secret.id, SecretRotate(value='fixture-new-value'), actor=self.actor)
            self.broker.rotate(self.secret.id, SecretRotate(value='fixture-private-value'), actor=self.actor)
        await self.assert_change_denied(rotate_back)

    async def test_revocation_still_refuses_before_capture(self):
        await self.assert_change_denied(lambda: self.broker.revoke(self.secret.id, actor=self.actor, reason='synthetic revocation'))

    async def test_new_secret_reference_still_refuses_before_capture(self):
        await self.assert_change_denied(lambda: self.broker.create(SecretCreate(name='Second synthetic credential', value='fixture-second-value'), actor=self.actor))

    async def test_acl_change_still_refuses_before_capture(self):
        def change():
            return self.broker.store.update(lambda state: state.model_copy(update={'references': [r.model_copy(update={'allowed_identity_ids': ['changed-actor']}) if r.id == self.secret.id else r for r in state.references]}))
        await self.assert_change_denied(change)

    async def test_expiry_change_still_refuses_before_capture(self):
        def change():
            return self.broker.store.update(lambda state: state.model_copy(update={'references': [r.model_copy(update={'expires_at': 1.0}) if r.id == self.secret.id else r for r in state.references]}))
        await self.assert_change_denied(change)

    async def test_membership_change_still_refuses_before_capture(self):
        membership = next(m for m in self.identity.state().memberships if m.identity_id == self.actor.identity_id)
        await self.assert_change_denied(lambda: self.identity.update_membership(membership.id, MembershipUpdate(roles=[MembershipRole.MEMBER]), actor=self.actor), secret=False)

    async def test_replacing_broker_configuration_still_refuses(self):
        def replace():
            self.execution.secret_broker = SecretBroker(SecretStateStore(self.sqlite), self.broker.backends)
        await self.assert_change_denied(replace, message='Recovery runtime admission configuration changed')

    async def test_rotation_after_ciphertext_keeps_unknown_custody_without_success(self):
        operations = self.operator.storage_provider.operations
        assess = operations._assess
        def rotated_assessment(*args, **kwargs):
            report = assess(*args, **kwargs)
            self.broker.rotate(self.secret.id, SecretRotate(value='fixture-new-value'), actor=self.actor)
            return report
        with patch.object(operations, '_assess', side_effect=rotated_assessment):
            async with self.client() as client:
                response = await client.post('/api/recovery/storage-preservation/operations', json=self.body())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(response.json()['item']['status'], 'succeeded')
        self.assertFalse(self.committed())
        reserved = [a for a in self.artifacts.store.load().artifacts if a.source == 'recovery-storage-preservation.v1']
        self.assertEqual(len(reserved), 1)
        self.assertIsNone(reserved[0].content)
        self.assertTrue(reserved[0].metadata['pending_content_sha256'])
        self.assert_original()
