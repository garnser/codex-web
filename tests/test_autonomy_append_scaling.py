from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from codex_web.autonomy import AutonomyCycleRecord, AutonomyDeadLetter, AutonomyState
from codex_web.autonomy_audit import AutonomyAuditPayload, AutonomyAuditRecord, AutonomyAuditState, GENESIS_HASH
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.storage.autonomy_audit import AutonomyAuditStore
from codex_web.storage.sqlite_state import SQLiteStateStore


def cycle(i):
    return AutonomyCycleRecord(cycle_key=f'cycle-{i}', event_id=f'event-{i}',
        event_type='test.event', source='test', recursion_depth=0, reasoning_score=0,
        outcome='completed', reason='known', completed_at=float(i))


def audit_payload(i, workspace='ws'):
    return AutonomyAuditPayload(kind='cycle', organization_id='org', workspace_id=workspace,
                               cycle_id=f'cycle-{i}', occurred_at=float(i))


class AutonomyAppendContract:
    @staticmethod
    def dead_letter(i):
        return AutonomyDeadLetter(id=f"dead-{i}", cycle_id=f"cycle-{i}", cycle_key=f"cycle-{i}",
                                  event_id=f"event-{i}", event_type="test", reason="blocked", attempts=0)

    def test_dead_letter_write_leaves_cycle_history_and_lookup_unchanged(self):
        store = AutonomyStateStore(self.backend)
        self.backend.put(store.namespace, AutonomyState(cycles=[cycle(i) for i in range(5000)],
                         dead_letters=[self.dead_letter(i) for i in range(1000)]).model_dump(mode="json"))
        store._ensure_cycle_records()
        before = self.backend.record_items(store.namespace)
        with patch.object(self.backend, "get", side_effect=AssertionError("history read")), \
             patch.object(self.backend, "record_items", side_effect=AssertionError("history scan")), \
             patch.object(self.backend, "record_replace", side_effect=AssertionError("history replacement")), \
             patch.object(self.backend, "update", side_effect=AssertionError("full-state update")), \
             patch.object(store, "_decode", side_effect=AssertionError("cycle validation")):
            appended = self.dead_letter(1000)
            self.assertEqual(store.record_dead_letter(appended), appended)
        after = self.backend.record_items(store.namespace)
        history = lambda rows: {k: v for k, v in rows.items() if k not in ("dead_letters", "updated_at")}
        self.assertEqual(history(before), history(after))
        self.assertEqual(len(after["dead_letters"]), 1000)
        self.assertEqual(after["dead_letters"][0]["id"], "dead-1")
        self.assertEqual(after["dead_letters"][-1]["id"], "dead-1000")

    def test_concurrent_dead_letters_are_atomic_and_wrapper_keeps_full_state_contract(self):
        store = AutonomyStateStore(self.backend)
        store.max_dead_letters = 10
        store._ensure_records()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: store.record_dead_letter(self.dead_letter(i)), range(30)))
        state = store.load()
        self.assertEqual(len(state.dead_letters), 10)
        self.assertEqual(len({item.id for item in state.dead_letters}), 10)
        result = store.append_dead_letter(self.dead_letter(30))
        self.assertIsInstance(result, AutonomyState)
        self.assertEqual(len(result.dead_letters), 10)
        self.assertEqual(result.dead_letters[-1].id, "dead-30")

    def test_invalid_retained_dead_letter_rejects_write_atomically(self):
        store = AutonomyStateStore(self.backend)
        store._ensure_records()
        for raw in ([{"id": "invalid"}], {}):
            with self.subTest(raw=raw):
                self.backend.record_apply(store.namespace, upserts={"dead_letters": raw})
                with self.assertRaises(ValueError):
                    store.record_dead_letter(self.dead_letter(1))
                self.assertEqual(self.backend.record_get(store.namespace, "dead_letters"), raw)

    def test_cycle_append_and_duplicate_lookup_do_not_read_or_validate_history(self):
        store = AutonomyStateStore(self.backend)
        self.backend.put(store.namespace, AutonomyState(cycles=[cycle(i) for i in range(5000)]).model_dump(mode='json'))
        self.assertEqual(store.find_cycle('cycle-4999', 'event-4999').cycle_key, 'cycle-4999')
        with patch.object(self.backend, 'get', side_effect=AssertionError('history read')), \
             patch.object(self.backend, 'record_items', side_effect=AssertionError('history scan')), \
             patch.object(store, '_decode', side_effect=AssertionError('history validation')):
            appended = store.append_cycle(cycle(5000))
            self.assertEqual(store.find_cycle(appended.cycle_key, appended.event_id), appended)
            self.assertIsNone(store.find_cycle('cycle-0', 'event-0'))
            self.assertEqual(len(store.recent_cycles(100)), 100)
        state = store.load()
        self.assertEqual(len(state.cycles), 5000)
        self.assertEqual(state.cycles[0].cycle_key, 'cycle-1')
        self.assertEqual(state.cycles[-1], appended)

    def test_concurrent_cycles_preserve_retention_lookup_and_order(self):
        store = AutonomyStateStore(self.backend)
        store.max_cycles = 10
        store._ensure_cycle_records()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: store.append_cycle(cycle(i)), range(30)))
        state = store.load()
        self.assertEqual(len(state.cycles), 10)
        retained = {item.cycle_key for item in state.cycles}
        for i in range(30):
            self.assertEqual(store.find_cycle(f'cycle-{i}', f'event-{i}') is not None, f'cycle-{i}' in retained)
        rows = self.backend.record_items(store.namespace)
        self.assertEqual(sum(key.startswith('cycle:') for key in rows), 10)
        self.assertEqual(sum(key.startswith('cycle-lookup:') for key in rows), 10)

    def test_duplicate_cycle_alias_survives_expiring_an_older_duplicate(self):
        store = AutonomyStateStore(self.backend)
        store.max_cycles = 2
        first = cycle(1)
        store.append_cycle(first)
        duplicate = first.model_copy(update={'id': 'later'})
        store.append_cycle(duplicate)
        store.append_cycle(cycle(2))
        self.assertEqual(store.find_cycle(first.cycle_key, first.event_id), duplicate)

    def test_audit_append_uses_only_tail_with_eight_thousand_records(self):
        rows = []
        previous = GENESIS_HASH
        for i in range(8000):
            record = AutonomyAuditRecord.build(audit_payload(i), sequence=i+1, previous_hash=previous)
            rows.append(record)
            previous = record.record_hash
        store = AutonomyAuditStore(self.backend)
        self.backend.put(store.namespace, AutonomyAuditState(records=rows).model_dump(mode='json'))
        store._ensure_records()
        with patch.object(self.backend, 'get', side_effect=AssertionError('history read')), \
             patch.object(self.backend, 'record_items', side_effect=AssertionError('history scan')), \
             patch.object(store, '_decode', side_effect=AssertionError('history validation')):
            appended = store.append(audit_payload(8000))
            other = store.append(audit_payload(8001, workspace='other'))
        self.assertEqual(appended.sequence, 8001)
        self.assertEqual(appended.previous_hash, previous)
        self.assertEqual(other.sequence, 1)
        self.assertEqual(other.previous_hash, GENESIS_HASH)
        self.assertEqual(store.load().records[-2:], [appended, other])

    def test_concurrent_audit_appends_have_one_unbroken_partition_chain(self):
        store = AutonomyAuditStore(self.backend)
        store._ensure_records()
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(lambda i: AutonomyAuditStore(self.backend).append(audit_payload(i)), range(24)))
        ordered = sorted(records, key=lambda item: item.sequence)
        self.assertEqual([item.sequence for item in ordered], list(range(1, 25)))
        previous = GENESIS_HASH
        for item in ordered:
            self.assertEqual(item.previous_hash, previous)
            previous = item.record_hash
        self.assertEqual(store.load().records, ordered)

    def test_snapshot_decoding_preserves_audit_root_and_migration_is_rollback_safe(self):
        from types import SimpleNamespace
        from codex_web.services.recovery import RecoveryService
        store = AutonomyAuditStore(self.backend)
        receipt = store.append(audit_payload(1))
        snapshot = SimpleNamespace(documents=self.backend.documents(), organization_id='org', workspace_id='ws')
        self.assertEqual(RecoveryService._snapshot_audit_root(snapshot), receipt.record_hash)
        # Restored keyed snapshots can be ingested through the generic document API.
        restored = snapshot.documents[store.namespace]
        self.backend.put(store.namespace, restored)
        self.assertEqual(store.load().records, [receipt])
        # A legacy-format export is also accepted and re-materialized.
        self.backend.put(store.namespace, store.load().model_dump(mode='json'))
        self.assertEqual(store.load().records, [receipt])

    def test_future_internal_schemas_fail_closed(self):
        audit = AutonomyAuditStore(self.backend)
        self.backend.record_replace(audit.namespace, {audit.meta_key: {'schema_version': '99', 'next_sequence': 0}})
        with self.assertRaises(ValueError):
            audit.append(audit_payload(1))
        cycles = AutonomyStateStore(self.backend)
        cycles._ensure_cycle_records()
        self.backend.record_apply(cycles.namespace, upserts={cycles.cycle_meta_key: {'schema_version': '99'}})
        with self.assertRaises(ValueError):
            cycles.append_cycle(cycle(1))


class SQLiteAutonomyAppendTests(AutonomyAppendContract, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backend = SQLiteStateStore(Path(directory.name) / 'state.db')


@unittest.skipUnless(os.environ.get('CODEX_WEB_TEST_POSTGRES_DSN'), 'PostgreSQL test DSN required')
class PostgresAutonomyAppendTests(AutonomyAppendContract, unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg_pool import ConnectionPool
        from codex_web.storage.postgres_state import PostgresStateStore
        dsn = os.environ['CODEX_WEB_TEST_POSTGRES_DSN']
        schema = 'autonomy_append_test_' + uuid.uuid4().hex
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        def cleanup():
            with psycopg.connect(dsn) as connection:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        self.addCleanup(cleanup)
        pool = ConnectionPool(dsn, min_size=1, max_size=4, kwargs={'options': f'-c search_path={schema}'})
        self.addCleanup(pool.close)
        self.backend = PostgresStateStore(dsn, connection_pool=pool)
