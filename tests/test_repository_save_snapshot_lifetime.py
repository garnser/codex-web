from __future__ import annotations

import copy
import gc
import tempfile
import unittest
from pathlib import Path

from codex_web.models import BotBinding, QueuedTurn
from codex_web.storage.bot_state import ModelListRepository
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository


class SaveSnapshotLifetimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = SQLiteStateStore(self.root / 'state.sqlite3')
        self.queue = TurnQueueRepository(self.store, self.root / 'queue.json')
        self.bindings = ModelListRepository(
            self.store, namespace='bot_bindings', legacy_path=self.root / 'bindings.json',
            model=BotBinding,
        )
        self.queue.save({'thread': [QueuedTurn(
            id='q', thread_id='thread', project_id='home', message='x' * 4096, created_at=1,
        )]})
        self.bindings.save([BotBinding(
            id='b', provider='slack', external_conversation_id='C1', thread_id='thread',
            created_at=1, updated_at=1,
        )])

    @staticmethod
    def retained(repo):
        snapshots = repo._snapshots
        return len(getattr(snapshots, '_entries', snapshots))

    def test_overlapping_discarded_reads_release_all_baselines(self):
        for repo in (self.queue, self.bindings):
            with self.subTest(repository=type(repo).__name__):
                for _ in range(3):
                    loaded = [repo.load() for _ in range(24)]
                    self.assertEqual(self.retained(repo), 24)
                    del loaded
                    gc.collect()
                    self.assertEqual(self.retained(repo), 0)

    def test_live_edit_survives_unrelated_read_churn_and_preserves_concurrent_rows(self):
        left = self.queue.load()
        binding_edit = self.bindings.load()
        for _ in range(40):
            self.queue.load()
            self.bindings.load()
        gc.collect()
        self.assertEqual(self.retained(self.queue), 1)
        self.assertEqual(self.retained(self.bindings), 1)
        self.queue.update('other', lambda _: [QueuedTurn(
            id='other', thread_id='other', project_id='home', message='concurrent', created_at=2,
        )])
        other = self.bindings.load()
        other.append(BotBinding(id='other', provider='slack', external_conversation_id='new', thread_id='other', created_at=2, updated_at=2))
        self.bindings.save(other)
        left['thread'][0].message = 'edited'
        binding_edit[0].thread_name = 'edited'
        self.queue.save(left)
        self.bindings.save(binding_edit)
        self.assertEqual(self.queue.get('thread')[0].message, 'edited')
        self.assertEqual(self.queue.get('other')[0].id, 'other')
        self.assertEqual({x.id for x in self.bindings.load()}, {'b', 'other'})
        self.assertEqual(self.retained(self.queue), 0)
        self.assertEqual(self.retained(self.bindings), 0)

    def test_plain_shallow_and_deep_copies_do_not_inherit_list_merge_baseline(self):
        for copying in (list, copy.copy, copy.deepcopy):
            with self.subTest(copy=copying.__name__):
                loaded = self.bindings.load()
                cloned = copying(loaded)
                latest = self.bindings.load()
                latest.append(BotBinding(id='new', provider='slack', external_conversation_id='new', thread_id='other', created_at=2, updated_at=2))
                self.bindings.save(latest)
                self.bindings.save(cloned)
                self.assertEqual([x.id for x in self.bindings.load()], ['b'])
                del loaded, cloned, latest
                gc.collect()
                self.assertEqual(self.retained(self.bindings), 0)

    def test_empty_baselines_preserve_concurrent_additions(self):
        self.queue.save({})
        self.bindings.save([])
        queues = self.queue.load()
        bindings = self.bindings.load()
        self.queue.update('peer', lambda _: [QueuedTurn(
            id='peer', thread_id='peer', project_id='home', message='peer', created_at=2,
        )])
        peer = self.bindings.load()
        peer.append(BotBinding(id='peer', provider='slack', external_conversation_id='peer',
                               thread_id='peer', created_at=2, updated_at=2))
        self.bindings.save(peer)
        queues['mine'] = [QueuedTurn(id='mine', thread_id='mine', project_id='home',
                                    message='mine', created_at=2)]
        bindings.append(BotBinding(id='mine', provider='slack', external_conversation_id='mine',
                                   thread_id='mine', created_at=2, updated_at=2))
        self.queue.save(queues)
        self.bindings.save(bindings)
        self.assertEqual(set(self.queue.list_queues()), {'mine', 'peer'})
        self.assertEqual({x.id for x in self.bindings.load()}, {'mine', 'peer'})

    def test_cross_repository_collection_uses_existing_plain_save_behavior(self):
        values = self.bindings.load()
        other = ModelListRepository(self.store, namespace='bot_bindings',
                                   legacy_path=self.root / 'other.json', model=BotBinding)
        later = other.load()
        later.append(BotBinding(id='new', provider='slack', external_conversation_id='new', thread_id='other', created_at=2, updated_at=2))
        other.save(later)
        other.save(values)
        self.assertEqual([x.id for x in other.load()], ['b'])
        self.assertEqual(self.retained(self.bindings), 1)

    def test_saved_collection_consumes_baseline_once_even_if_store_fails(self):
        from unittest.mock import patch
        values = self.bindings.load()
        values[0].thread_name = 'changed'
        with patch.object(self.store, 'update', side_effect=RuntimeError('private failure')):
            with self.assertRaises(RuntimeError):
                self.bindings.save(values)
        self.assertEqual(self.retained(self.bindings), 0)

    def test_delayed_old_cleanup_does_not_remove_replacement_baseline(self):
        from codex_web.storage.save_snapshots import SaveSnapshots, SnapshotDict
        tracker = SaveSnapshots()
        loaded = SnapshotDict()
        tracker.remember(loaded, {'version': 1})
        old_reference = tracker._entries[id(loaded)][0]
        tracker.remember(loaded, {'version': 2})
        old_reference.__callback__(old_reference)
        self.assertEqual(tracker.take(loaded), {'version': 2})
        self.assertIsNone(tracker.take(loaded))

    def test_repeat_save_retains_existing_no_baseline_replacement_behavior(self):
        values = self.bindings.load()
        self.bindings.save(values)
        other = self.bindings.load()
        other.append(BotBinding(id='new', provider='slack', external_conversation_id='new', thread_id='other', created_at=2, updated_at=2))
        self.bindings.save(other)
        self.bindings.save(values)
        self.assertEqual([x.id for x in self.bindings.load()], ['b'])
