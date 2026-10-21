import json
import unittest
from unittest.mock import patch

from codex_web.recovery import RestoreVerificationStatus
from codex_web.services.recovery_snapshot_strings import (
    SnapshotStringMetadataChanged,
    observe_snapshot_string_bound,
    require_snapshot_string_bound,
)
from tests import test_recovery


class SnapshotStringObservationTests(unittest.TestCase):
    def test_actual_bound_covers_keys_unicode_nested_values_and_shared_containers(self):
        shared = ['a' * 1000, '\U0001f642' * 2000]
        documents = {'long-key' * 400: [shared, shared], 'empty': {}}
        bound = observe_snapshot_string_bound(documents)
        self.assertEqual(bound, 3200)
        require_snapshot_string_bound(documents, bound)
        documents['other'] = 'z' * 3201
        with self.assertRaisesRegex(SnapshotStringMetadataChanged, 'metadata changed'):
            require_snapshot_string_bound(documents, bound)

    def test_string_subclass_cannot_lie_about_native_length(self):
        class WrongLength(str):
            def __len__(self):
                return 0
        self.assertEqual(observe_snapshot_string_bound([WrongLength('abcd')]), 4)

    def test_circular_input_is_refused_without_payload_repr(self):
        value = []
        value.append(value)
        with self.assertRaisesRegex(SnapshotStringMetadataChanged, 'circular document'):
            observe_snapshot_string_bound(value)

    def test_guard_does_not_change_valid_plaintext_or_restore_qualification(self):
        fixture = test_recovery.RecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.state.put('snapshot-values', {'unicode': '\U0001f642', 'nested': [None, 1, True]})
        from codex_web.services import recovery
        with patch.object(recovery, 'require_snapshot_string_bound', wraps=require_snapshot_string_bound) as guard, \
                patch.object(fixture.service, '_canonical_bytes', wraps=fixture.service._canonical_bytes) as encode:
            manifest = fixture.service.create_backup(actor=fixture.actor)
        guard.assert_called_once()
        plaintext = encode.call_args_list[0].args[0]
        from codex_web.canonical_json import canonical_json_bytes
        self.assertEqual(canonical_json_bytes(plaintext),
                         json.dumps(plaintext, sort_keys=True, separators=(',', ':')).encode())
        verified = fixture.service.verify_restore(manifest.id, actor=fixture.actor)
        self.assertEqual(verified.status, RestoreVerificationStatus.PASS)

    def test_ordinary_document_growth_is_refused_before_encryption_and_storage(self):
        fixture = test_recovery.RecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = fixture.service
        original_manifest = service._snapshot_key_manifest

        def add_ordinary_value(documents, actor):
            manifest = original_manifest(documents, actor)
            bound = observe_snapshot_string_bound(documents)
            documents['synthetic-document'] = {'value': 'x' * (bound + 1)}
            return manifest

        from codex_web.services.recovery import RecoveryConflictError
        with patch.object(service, '_snapshot_key_manifest', add_ordinary_value), \
                patch.object(service.crypto, 'encrypt') as encrypt, \
                patch.object(service._destination(service.policy().destination_id), 'put') as put:
            with self.assertRaisesRegex(RecoveryConflictError, '^Snapshot string metadata changed after initial observation; capture refused$'):
                service.create_backup(actor=fixture.actor)
        encrypt.assert_not_called()
        put.assert_not_called()
        self.assertEqual(service.store.load().backups, {})

