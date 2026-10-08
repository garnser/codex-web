from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from codex_web.agent_runtime_usage import AgentRuntimeUsage, AGENT_RUNTIME_USAGE_STATE_CONTRACT
from codex_web.storage.agent_runtime_usage import AgentRuntimeUsageStore
from codex_web.storage.sqlite_state import SQLiteStateStore

class AgentRuntimeUsagePointReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = SQLiteStateStore(Path(self.temp.name) / 'state.sqlite3')
        self.store = AgentRuntimeUsageStore(self.backend)
        self.record = AgentRuntimeUsage(id='usage-one', organization_id='org', workspace_id='ws',
            provider_id='openai', runtime_id='codex', runtime_type='codex-app-server')

    def envelope(self, record):
        return {'schema_version': AGENT_RUNTIME_USAGE_STATE_CONTRACT.current,
                'records': [record.model_dump(mode='json')]}

    def test_point_read_and_upsert_do_not_reconstruct_or_replace_keyed_catalog(self):
        self.store.upsert(self.record)
        with patch.object(self.backend, 'get', side_effect=AssertionError('full catalog get')), \
             patch.object(self.backend, 'record_items', side_effect=AssertionError('full catalog enumeration')), \
             patch.object(self.backend, 'record_replace', side_effect=AssertionError('collection replacement')):
            self.assertEqual(self.store.get(self.record.id), self.record)
            self.store.upsert(self.record.model_copy(update={'input_tokens': 9}))
            self.assertEqual(self.store.get(self.record.id).input_tokens, 9)
            self.assertIsNone(self.store.get('missing'))
            with self.assertRaisesRegex(RuntimeError, 'another tenant'):
                self.store.upsert(self.record.model_copy(update={'organization_id': 'other'}))

    def test_document_read_distinguishes_keyed_records_from_legacy_document(self):
        self.store.upsert(self.record)
        self.assertIsNone(self.backend.document_get(self.store.namespace))
        self.assertIn(self.record.id, self.backend.get(self.store.namespace))

    def test_legacy_document_migrates_with_version_and_measurements_preserved(self):
        self.backend.put(self.store.namespace, self.envelope(self.record))
        self.assertEqual(self.store.get(self.record.id), self.record)
        self.assertIsNone(self.backend.document_get(self.store.namespace))
        self.assertEqual(self.store.get(self.record.id), self.record)

    def test_coexisting_legacy_document_merges_existing_keyed_records(self):
        self.store.upsert(self.record)
        newer = self.record.model_copy(update={'id': 'usage-two', 'input_tokens': 4})
        with self.backend._connection() as connection:
            connection.execute('INSERT INTO state_documents(namespace,payload,updated_at) VALUES (?,?,?)',
                               (self.store.namespace, json.dumps(self.envelope(newer)), 1.0))
        self.assertEqual(self.store.get(newer.id), newer)
        self.assertEqual(self.store.get(self.record.id), self.record)
        self.assertEqual(set(self.backend.record_items(self.store.namespace)), {'usage-one','usage-two'})

    def test_generic_keyed_legacy_envelope_migrates_without_reserved_key_records(self):
        self.store.upsert(self.record)
        self.backend.put(self.store.namespace, self.envelope(self.record))
        self.assertEqual(self.store.get(self.record.id), self.record)
        self.assertEqual(set(self.backend.record_items(self.store.namespace)), {'usage-one'})
        self.assertEqual([r.id for r in self.store.list()], ['usage-one'])
