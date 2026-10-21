import unittest

from codex_web.secrets import SecretAuditEvent, SecretReference, SecretState
from codex_web.services.code_host_readiness import ReadySecretObservation


class ReadySecretAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.state = SecretState(references=[SecretReference(id='credential', name='Fixture',
            organization_id='org', workspace_id='workspace', created_at=0, expires_at=200,
            allowed_identity_ids=['worker'])])
        self.pin = ReadySecretObservation(self.state, 100)

    def test_audit_only_changes_preserve_authority(self):
        current = self.state.model_copy(deep=True)
        current.audit.append(SecretAuditEvent(secret_id='credential', organization_id='org',
            workspace_id='workspace', actor_identity_id='worker', actor_kind='service',
            action='slack.post_message', outcome='allowed'))
        self.assertTrue(self.pin.matches(current, now=110))

    def test_every_reference_field_remains_exact(self):
        changes = dict(id='different', backend='vault', organization_id='other', workspace_id='other',
            name='Renamed', provider='slack', purpose='other', owner_identity_id='other',
            allowed_identity_ids=['other'], reveal_identity_ids=['other'], created_at=1,
            expires_at=250, rotated_at=110, rotation=1, revoked_at=110, revoke_reason='revoked')
        self.assertEqual(set(changes), set(SecretReference.model_fields))
        for field, value in changes.items():
            with self.subTest(field=field):
                current = self.state.model_copy(deep=True)
                setattr(current.references[0], field, value)
                self.assertFalse(self.pin.matches(current, now=110))

    def test_schema_version_remains_exact(self):
        current = self.state.model_copy(update={'schema_version': 'future'})
        self.assertFalse(self.pin.matches(current, now=110))

    def test_reference_inventory_and_order_remain_exact(self):
        current = self.state.model_copy(deep=True)
        current.references.append(current.references[0].model_copy(update={'id': 'second'}))
        self.assertFalse(self.pin.matches(current, now=110))
        pin = ReadySecretObservation(current.model_copy(deep=True), 100)
        current.references.reverse()
        self.assertFalse(pin.matches(current, now=110))
        current.references.clear()
        self.assertFalse(self.pin.matches(current, now=110))

    def test_original_expiry_bounds_observation_without_metadata_change(self):
        self.assertTrue(self.pin.matches(self.state, now=199))
        self.assertFalse(self.pin.matches(self.state, now=200))
        self.assertFalse(self.pin.matches(self.state, now=201))
        for now in (99, float('nan'), float('inf')):
            self.assertFalse(self.pin.matches(self.state, now=now))
