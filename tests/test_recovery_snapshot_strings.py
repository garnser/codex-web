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
        from codex_web.services import recovery_snapshot_capture
        with patch.object(recovery_snapshot_capture, 'require_snapshot_string_bound',
                          wraps=require_snapshot_string_bound) as guard, \
                patch.object(fixture.crypto, 'encrypt', wraps=fixture.crypto.encrypt) as encrypt:
            manifest = fixture.service.create_backup(actor=fixture.actor)
        self.assertGreater(guard.call_count, 0)
        plaintext = encrypt.call_args.args[1]
        document = json.loads(plaintext)
        self.assertEqual(plaintext, json.dumps(document, sort_keys=True, separators=(',', ':')).encode())
        self.assertEqual(document['documents']['snapshot-values'],
                         {'unicode': '\U0001f642', 'nested': [None, 1, True]})
        verified = fixture.service.verify_restore(manifest.id, actor=fixture.actor)
        self.assertEqual(verified.status, RestoreVerificationStatus.PASS)

    def test_ordinary_document_growth_is_refused_before_encryption_and_storage(self):
        fixture = test_recovery.RecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        service = fixture.service
        fixture.state.put('snapshot-growth', {'value': 'small'})
        original_observe = observe_snapshot_string_bound

        def grow_after_observation(value):
            bound = original_observe(value)
            if value == {'value': 'small'}:
                value['value'] = 'x' * (bound + 1)
            return bound

        from codex_web.services import recovery_snapshot_capture
        from codex_web.services.recovery import RecoveryConflictError
        with patch.object(recovery_snapshot_capture, 'observe_snapshot_string_bound',
                          new=grow_after_observation), \
                patch.object(service.crypto, 'encrypt') as encrypt, \
                patch.object(service._destination(service.policy().destination_id), 'put') as put:
            with self.assertRaisesRegex(RecoveryConflictError, '^Snapshot string metadata changed after initial observation; capture refused$'):
                service.create_backup(actor=fixture.actor)
        encrypt.assert_not_called()
        put.assert_not_called()
        self.assertEqual(service.store.load().backups, {})
