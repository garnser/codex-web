"""Current Secret metadata does not require decoding unrelated audit history."""
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from codex_web.compatibility import ContractCompatibilityError
from codex_web.secrets import SecretAuditEvent, SecretCreate, SecretStatus
from codex_web.services.identity import TenantIsolationError
from codex_web.services.secrets import SecretBroker, SecretNotFoundError, SecretUseDeniedError
from codex_web.storage.secret_state import SecretStateStore
from tests import test_secrets_broker as fixture


class SecretMetadataPointReadTests(unittest.TestCase):
    setUp = fixture.SecretBrokerTests.setUp
    tearDown = fixture.SecretBrokerTests.tearDown

    def reference(self):
        self.worker = self.identity.bootstrap_service_actor(
            identity_id='metadata-worker', name='Metadata worker',
            scope=self.admin.tenant, service_scopes=('secret:use',))
        return self.broker.create(SecretCreate(name='Synthetic metadata reference',
            value='public-synthetic-fixture-value', allowed_identity_ids=[self.worker.identity_id]), actor=self.admin)

    def test_repeated_current_metadata_checks_do_not_decode_audit_catalog(self):
        reference = self.reference()
        event = SecretAuditEvent(secret_id=reference.id,
            organization_id=self.admin.organization_id, workspace_id=self.admin.workspace_id,
            actor_identity_id=self.worker.identity_id, actor_kind='service', action='use', outcome='succeeded',
            context={'operation': 'synthetic-metadata-fixture', 'padding': 'x' * 256})
        self.store.update(lambda state: state.model_copy(update={'audit': [
            event.model_copy(update={'id': 'secret-audit-synthetic-%04d' % i}) for i in range(2000)]}))
        decoded, full_reads = [], []
        decode, get = self.store._decode, self.sqlite.get
        def measured_decode(payload):
            if isinstance(payload, dict):
                decoded.append((len(payload.get('references', [])), len(payload.get('audit', []))))
            return decode(payload)
        def measured_get(namespace):
            if namespace == self.store.namespace:
                full_reads.append(namespace)
            return get(namespace)
        with patch.object(self.store, '_decode', side_effect=measured_decode), \
                patch.object(self.sqlite, 'get', side_effect=measured_get), \
                patch.object(self.backend, 'get', side_effect=AssertionError('metadata must not resolve material')):
            for actor in (self.admin, self.worker) * 3:
                self.assertEqual(self.broker.metadata(reference.id, actor=actor, require_use=True), reference)
        # This is an owner/cost regression, not an arbitrary timing threshold.
        self.assertEqual(decoded, [(1, 0)] * 6,
            'current metadata lookup must not decode the full unrelated audit catalog')
        self.assertEqual(full_reads, [])

    def test_next_call_observes_rotation_revocation_expiry_and_acl_changes(self):
        reference = self.reference()
        self.assertEqual(self.broker.metadata(reference.id, actor=self.worker, require_use=True).rotation, 0)
        def replace(**changes):
            def update(state):
                state.references = [r.model_copy(update=changes) if r.id == reference.id else r for r in state.references]
                return state
            self.store.update(update)
        replace(rotation=1)
        self.assertEqual(self.broker.metadata(reference.id, actor=self.worker, require_use=True).rotation, 1)
        replace(revoked_at=1)
        self.assertEqual(self.broker.metadata(reference.id, actor=self.worker, require_use=True).status(), SecretStatus.REVOKED)
        replace(revoked_at=None, expires_at=1)
        self.assertEqual(self.broker.metadata(reference.id, actor=self.worker, require_use=True).status(), SecretStatus.EXPIRED)
        replace(expires_at=None, allowed_identity_ids=[])
        with self.assertRaises(SecretUseDeniedError):
            self.broker.metadata(reference.id, actor=self.worker, require_use=True)

    def test_worker_use_scope_is_independent_of_reference_acl(self):
        reference = self.reference()
        no_scope = self.identity.bootstrap_service_actor(identity_id='metadata-no-scope', name='No Use scope',
            scope=self.admin.tenant, service_scopes=())
        self.store.update(lambda state: state.model_copy(update={'references': [
            reference.model_copy(update={'allowed_identity_ids': [no_scope.identity_id]})]}))
        with self.assertRaises(SecretUseDeniedError):
            self.broker.metadata(reference.id, actor=no_scope, require_use=True)

    def test_metadata_and_use_acl_denials_remain_independent(self):
        reference = self.reference()
        outsider = self.identity.bootstrap_service_actor(identity_id='metadata-outsider', name='No reference ACL',
            scope=self.admin.tenant, service_scopes=('secret:use',))
        with self.assertRaises(SecretUseDeniedError):
            self.broker.metadata(reference.id, actor=outsider)
        with self.assertRaises(SecretUseDeniedError):
            self.broker.metadata(reference.id, actor=outsider, require_use=True)

    def test_first_global_id_preserves_foreign_shadow_denial(self):
        reference = self.reference()
        foreign = reference.model_copy(update={'organization_id': 'foreign-org', 'workspace_id': 'foreign-space'})
        self.store.update(lambda state: state.model_copy(update={'references': [foreign, reference]}))
        with self.assertRaises(TenantIsolationError):
            self.broker.metadata(reference.id, actor=self.admin, require_use=True)

    def test_same_scope_duplicates_keep_first_reference_and_missing_keeps_error(self):
        reference = self.reference()
        first = reference.model_copy(update={'rotation': 9, 'revoked_at': 1})
        self.store.update(lambda state: state.model_copy(update={'references': [first, reference]}))
        self.assertEqual(self.broker.metadata(reference.id, actor=self.admin), first)
        with self.assertRaises(SecretNotFoundError):
            self.broker.metadata('secret-not-present', actor=self.admin)

    def test_unavailable_projection_and_custom_load_keep_original_owner(self):
        reference = self.reference()
        with patch.object(self.sqlite, 'document_array_items_envelope', None):
            self.assertEqual(self.broker.metadata(reference.id, actor=self.admin), reference)
        altered = reference.model_copy(update={'rotation': 7})
        original_load = self.store.load
        state = original_load().model_copy(update={'references': [altered]})
        with patch.object(self.store, 'load', return_value=state) as load:
            self.assertEqual(self.broker.metadata(reference.id, actor=self.admin), altered)
            load.assert_called_once()

    def test_backend_without_explicit_scope_order_capability_uses_legacy_read(self):
        reference = self.reference()
        with patch.object(self.sqlite, 'supports_document_array_scope_after_first', False), \
                patch.object(self.sqlite, 'document_array_items_envelope',
                    side_effect=AssertionError('legacy backend must not receive new projection arguments')) as project:
            self.assertEqual(self.broker.metadata(reference.id, actor=self.admin), reference)
            project.assert_not_called()

    def test_nonstring_secret_id_preserves_original_lookup_behavior(self):
        reference = self.reference()
        with patch.object(self.sqlite, 'document_array_items_envelope',
                side_effect=AssertionError('non-string IDs must retain the original getter')) as project:
            for secret_id in (None, 42, ['unhashable'], {'unhashable': True}):
                with self.subTest(secret_id=type(secret_id).__name__):
                    with self.assertRaises(SecretNotFoundError):
                        self.broker.metadata(secret_id, actor=self.admin)
            class StringSubclass(str):
                pass
            self.assertEqual(self.broker.metadata(StringSubclass(reference.id), actor=self.admin), reference)
            project.assert_not_called()

    def test_custom_broker_reference_and_custom_store_keep_original_owner(self):
        reference = self.reference()
        altered = reference.model_copy(update={'rotation': 8})
        with patch.object(self.broker, '_reference', return_value=altered) as lookup:
            self.assertEqual(self.broker.metadata(reference.id, actor=self.admin), altered)
            lookup.assert_called_once_with(reference.id)
        class CustomStore(SecretStateStore):
            def load(inner):
                return self.store.load().model_copy(update={'references': [altered]})
        custom = SecretBroker(CustomStore(self.sqlite), {'local': self.backend})
        self.assertEqual(custom.metadata(reference.id, actor=self.admin), altered)

    def test_class_level_reference_override_keeps_original_owner(self):
        reference = self.reference()
        altered = reference.model_copy(update={'rotation': 6})
        with patch.object(SecretBroker, '_reference', lambda owner, secret_id: altered):
            self.assertEqual(self.broker.metadata(reference.id, actor=self.admin), altered)

    def test_namespace_shape_and_future_schema_do_not_bypass_decoder(self):
        reference = self.reference()
        original = self.sqlite.get(self.store.namespace)
        for changes in ({'audit': {}}, {'references': {}}, {'extra': True}, {'schema_version': '9.0'}):
            with self.subTest(changes=tuple(changes)):
                self.sqlite.put(self.store.namespace, {**original, **changes})
                try:
                    with self.assertRaises((ValidationError, ContractCompatibilityError, ValueError)):
                        self.broker.metadata(reference.id, actor=self.admin)
                finally:
                    self.sqlite.put(self.store.namespace, original)

    def test_selected_malformed_reference_fails_closed(self):
        reference = self.reference()
        original = self.sqlite.get(self.store.namespace)
        malformed = reference.model_dump(mode='json')
        malformed['rotation'] = -1
        self.sqlite.put(self.store.namespace, {**original, 'references': [malformed]})
        with self.assertRaises(ValidationError):
            self.broker.metadata(reference.id, actor=self.admin)
