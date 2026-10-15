"""Compatibility must preserve raw identity and unknown-start safety."""
import hashlib
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_web.models import QueuedTurn
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore

FIELDS = ('canonical_ingress_id', 'ingress_execution_profile_definition')


class QueuedSnapshotCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ExecutionPreflightStore(SQLiteStateStore(Path(self.temp.name) / 'state.sqlite'))
        self.service = ExecutionPreflightService(self.store)
        self.queued = QueuedTurn(id='original', thread_id='thread', project_id='project',
            message='input', created_at=1, repository_resource_id='repository')
        self.attempt = self.service.retain_queued(self.queued, organization_id='org', workspace_id='ws')

    def write_original(self, snapshot):
        value = self.attempt.model_copy(update={'queued_turn_snapshot':snapshot,
            'status':'reconciliation_required'})
        self.store.store.record_apply(self.store.NAMESPACE, upserts={
            self.store._attempt_key(value.id):value.model_dump(mode='json')})
        return value

    def test_all_historical_index_shapes_and_duplicate_retention(self):
        fresh = self.queued.model_dump(mode='json')
        for present in itertools.product((False, True), repeat=2):
            with self.subTest(present=present):
                snapshot = {k:v for k,v in fresh.items() if k not in FIELDS}
                snapshot.update({field:None for field,keep in zip(FIELDS,present) if keep})
                original = self.write_original(snapshot)
                # Remove new indexes: lookup must discover the pre-fix hash itself.
                rows, _ = self.store.store.record_page(self.store.NAMESPACE, key_prefix='q/', limit=200)
                identity = {k:v for k,v in snapshot.items() if k not in {'id','execution_id','created_at','attempts'}}
                digest = hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',', ':')).encode()).hexdigest()
                key = f'q/thread/{digest}/{original.id}'
                self.store.store.record_apply(self.store.NAMESPACE, deletes=list(rows), upserts={key:{'attemptId':original.id}})
                page = self.store.store.record_page
                def bounded(*args, **kwargs):
                    self.assertTrue(kwargs.get('key_prefix', '').startswith('q/thread/'))
                    return page(*args, **kwargs)
                with patch.object(self.store.store, 'record_page', side_effect=bounded):
                    self.assertEqual(self.store.matching_queued(fresh).id, original.id)
                    self.assertEqual(self.store.matching_queued(snapshot).id, original.id)
                for _ in range(2):
                    retained = self.service.retain_queued(self.queued, organization_id='org', workspace_id='ws')
                    self.assertEqual(retained.model_dump(), original.model_dump())
                self.assertIsNone(retained.queued_start_receipt)
                self.assertIsNone(retained.started_at)

    def test_exact_nonnull_and_all_other_identity_data_remain_authoritative(self):
        fresh = self.queued.model_dump(mode='json')
        for field in FIELDS:
            for value in ('ingress-1', {'record_id':'exact-definition', 'revision':7}):
                snapshot = fresh | {field:value}
                original = self.write_original(snapshot)
                self.assertEqual(self.store.retain_queued(original).queued_turn_snapshot, snapshot)
                self.assertEqual(self.store.matching_queued(snapshot).id, original.id)
                for changed in (None, 'different', {'record_id':'different', 'revision':8}):
                    with self.assertRaises(ValueError):
                        self.store.retain_queued(original.model_copy(update={'queued_turn_snapshot':snapshot | {field:changed}}))
                    self.assertIsNone(self.store.matching_queued(snapshot | {field:changed}))
        for change in ({'project_id':'other'}, {'repository_resource_id':'other'},
                       {'model':'other'}, {'message':'other'}, {'agent_profile_revision':9},
                       {'unknown_identity':None}):
            original = self.write_original(fresh)
            with self.assertRaises(ValueError):
                self.store.retain_queued(original.model_copy(update={'queued_turn_snapshot':fresh | change}))
        original = self.write_original(fresh | {'unknown_identity':None})
        with self.assertRaises(ValueError):
            self.store.retain_queued(original.model_copy(update={'queued_turn_snapshot':fresh}))
        for field in ('organization_id','workspace_id','project_id'):
            with self.assertRaises(ValueError):
                self.store.retain_queued(original.model_copy(update={field:'other'}))

    def test_verified_route_index_preserves_nonnull_ingress_and_unknown_fields(self):
        fresh = self.queued.model_dump(mode='json') | {
            'canonical_ingress_id':'ingress-1',
            'ingress_execution_profile_definition':{'record_id':'pinned', 'revision':7},
            'unknown_identity':{'nested':None}}
        original = self.write_original(fresh)
        self.store.retain_queued(original)
        self.store.update(original.id, lambda value: value.model_copy(update={'replacement_thread_id':'new'}))
        routed = fresh | {'thread_id':'new', 'id':'new-queue-id'}
        self.assertEqual(self.store.matching_queued(routed).id, original.id)
        self.assertEqual(self.store.matching_queued(fresh).id, original.id)  # original hold alias
        for field in FIELDS + ('unknown_identity',):
            self.assertIsNone(self.store.matching_queued(routed | {field:None}))
        for mismatch in (fresh | {'thread_id':'unrelated'},
                         routed | {'canonical_ingress_id':'changed'},
                         fresh | {'unknown_identity':None},
                         routed | {'project_id':'different'}):
            # A genuinely mismatched index row cannot confer an alias or scope.
            self.store.store.record_apply(self.store.NAMESPACE, upserts={
                self.store._queued_identity_key(mismatch)+original.id:{'attemptId':original.id}})
            self.assertIsNone(self.store.matching_queued(mismatch))
        self.store.update(original.id, lambda value: value.model_copy(update={'status':'started'}))
        self.assertIsNone(self.store.matching_queued(routed))


class QueuedOriginalRouteHoldTests(unittest.TestCase):
    def test_runtime_enqueue_refuses_unknown_original_on_both_verified_routes(self):
        self.check_fresh_writer_hold(nonnull_ingress=True)

    def test_fresh_null_writer_hold_survives_released_raw_hash_reader(self):
        self.check_fresh_writer_hold(nonnull_ingress=False)

    def check_fresh_writer_hold(self, *, nonnull_ingress):
        from unittest.mock import AsyncMock
        from codex_web.models import BotReplyTarget
        from codex_web.runtime.execution import TurnExecutionService, ExecutionPrestartRefusal
        from codex_web.storage.thread_index import ThreadIndexRepository
        from tests.test_turn_execution import _Host

        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteStateStore(Path(directory) / 'state.sqlite')
            routes = ThreadIndexRepository(state, Path(directory) / 'threads.json')
            routes.remember_replacement('old', 'new', project_id='project',
                provenance={'organization_id':'local', 'workspace_id':'default'})
            host = _Host()
            # Same queue transaction seam as the host fixture, with ingress ID
            # passed separately by enqueue_turn. No preflight or lookup mock.
            host._update_thread_queue_with_ingress = (
                lambda thread, ingress, update: host._update_thread_queue_record(thread, update))
            runtime = TurnExecutionService(host)
            runtime.start_thread_turn_now = AsyncMock()
            definition = dict(definition_id='execution.profiles.default', kind='execution-profile-catalog',
                definition_schema_version='1.0', revision=7, record_id='pinned', checksum='a'*64)
            request = dict(thread_id='old', project_id='project', message='uncertain input',
                canonical_ingress_id='ingress-original', ingress_execution_profile_definition=definition,
                repository_resource_id='repository', writable_repository_resource_ids=('repository',),
                agent_profile_id='profile', agent_profile_revision=3, agent_profile_actor_id='actor',
                reply_target=BotReplyTarget(provider='telegram', external_conversation_id='chat', thread_id='old', updated_at=1))
            if not nonnull_ingress:
                request.update(canonical_ingress_id=None, ingress_execution_profile_definition=None)
            queued = runtime.enqueue_turn(**request)
            service = ExecutionPreflightService(ExecutionPreflightStore(state),
                replacement_resolver=routes.replacement_thread_id)
            retained = service.retain_queued(queued, organization_id='local', workspace_id='default')
            _, claim = service.claim_queued_admission(retained.id)
            self.assertTrue(service.begin_queued_start(retained.id, claim_id=claim))
            # A dequeued native attempt with unknown outcome remains durable.
            host.queues.clear()
            moved = queued.model_copy(update={'thread_id':'new',
                'reply_target':queued.reply_target.model_copy(update={'thread_id':'new'})})
            service.retain_queued(moved, organization_id='local', workspace_id='default')
            runtime.queued_preflight = service
            before = service.store.get(retained.id).model_dump(mode='json')
            self.assertEqual(before['status'], 'reconciliation_required')
            self.assertEqual(before['replacement_thread_id'], 'new')
            # Independently reproduce e025/514's raw identity hash and page lookup.
            # Do not call any new normalization/key/lookup helper. This database
            # contains only fresh writer indexes, with no historical aliases.
            def released_reader(snapshot):
                identity = {k:v for k,v in snapshot.items()
                    if k not in {'id', 'execution_id', 'created_at', 'attempts'}}
                digest = hashlib.sha256(json.dumps(identity, sort_keys=True,
                    separators=(',', ':')).encode()).hexdigest()
                prefix = f"q/{snapshot['thread_id']}/{digest}/"
                cursor = None
                while True:
                    rows, cursor = state.record_page(service.store.NAMESPACE,
                        key_prefix=prefix, after=cursor, limit=200)
                    for row in rows.values():
                        attempt = service.store.get(row['attemptId'])
                        if attempt is not None and attempt.status != 'started':
                            return attempt
                    if cursor is None:
                        return None

            for thread in ('old', 'new'):
                projected = queued.model_copy(update={'thread_id':thread,
                    'reply_target':queued.reply_target.model_copy(update={'thread_id':thread})})
                found = released_reader(projected.model_dump(mode='json'))
                self.assertIsNotNone(found, 'released raw-hash reader lost a fresh writer hold')
                self.assertEqual(found.id, retained.id)
                host.queues.clear()
                routed = request | {'thread_id':thread,
                    'reply_target':queued.reply_target.model_copy(update={'thread_id':thread})}
                with self.subTest(thread=thread):
                    with self.assertRaises(ExecutionPrestartRefusal) as caught:
                        runtime.enqueue_turn(**routed)
                    self.assertEqual(caught.exception.detail['attemptId'], retained.id)
                    self.assertEqual(caught.exception.detail['status'], 'reconciliation_required')
                    self.assertFalse(caught.exception.detail['retryable'])
                    self.assertEqual(host.queues, {})
                    # The rollback reader must enforce the actual enqueue hold,
                    # not merely produce the same digest in a unit assertion.
                    with patch.object(service.store, 'matching_queued', side_effect=released_reader):
                        with self.assertRaises(ExecutionPrestartRefusal) as rollback:
                            runtime.enqueue_turn(**routed)
                    self.assertEqual(rollback.exception.detail['attemptId'], retained.id)
                    self.assertEqual(rollback.exception.detail['status'], 'reconciliation_required')
                    self.assertFalse(rollback.exception.detail['retryable'])
                    self.assertEqual(host.queues, {})
                for change in ({'project_id':'other'}, {'repository_resource_id':'other'},
                        {'writable_repository_resource_ids':('other',)},
                        {'agent_profile_revision':4}, {'canonical_ingress_id':'different'},
                        {'ingress_execution_profile_definition':definition | {'revision':8}}):
                    with self.subTest(thread=thread, change=change):
                        host.queues.clear()
                        different = runtime.enqueue_turn(**(routed | change))
                        self.assertNotEqual(different.id, queued.id)
                        self.assertEqual([q.id for q in host._thread_queue(thread)], [different.id])
                        host.queues.clear()
            runtime.start_thread_turn_now.assert_not_awaited()
            self.assertEqual(service.store.get(retained.id).model_dump(mode='json'), before)
            self.assertIsNone(before['queued_start_receipt'])
            self.assertIsNone(before['started_at'])
