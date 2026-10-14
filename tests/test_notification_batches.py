import json
import unittest
from copy import deepcopy

from pydantic import ValidationError

from codex_web.models import BotReplyTarget, QueuedTurn
from codex_web.notification_batches import NotificationBatchPlan, select_notification_batch
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from pathlib import Path
import tempfile


class NotificationBatchPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = ExecutionPreflightService(ExecutionPreflightStore(SQLiteStateStore(Path(self.temp.name) / 'state.sqlite')))
        self.items = [QueuedTurn(id=f'original-{index}', execution_id=f'execution-{index}', thread_id='thread-a',
            project_id='project-a', message=f'Synthetic original notification {index}', work_item_ref=f'group/project#{index}',
            created_at=index, source='gitlab', execution_profile_id='orchestration-only',
            agent_profile_id='agent-a', agent_profile_revision=7, agent_profile_actor_id='actor-a',
            reply_target=BotReplyTarget(provider='slack',connection_id='connection-a',external_conversation_id='channel-a',
                thread_id='thread-a', updated_at=1), repository_resource_id='repo-a', writable_repository_resource_ids=('repo-a',))
            for index in range(1,5)]
        self.attempts = {item.id:self.service.retain_queued(item, organization_id='local', workspace_id='default') for item in self.items}

    def select(self, items=None, **changes):
        values = dict(anchor_id=self.items[0].id, attempts_by_original_id=self.attempts,
                      verified_notification_ids=frozenset(item.id for item in self.items), organization_id='local',
                      authority_scope_sha256_by_original_id={item.id:'a'*64 for item in self.items},
                      notification_source_origin='gitlab',
                      workspace_id='default', max_members=3, max_context_bytes=65536)
        values.update(changes)
        return select_notification_batch(self.items if items is None else items, **values)

    def test_complete_originals_and_requested_execution_ids_survive_roundtrip(self):
        before = deepcopy([item.model_dump(mode='json') for item in self.items])
        plan = self.select()
        restored = NotificationBatchPlan.model_validate_json(plan.model_dump_json())
        self.assertEqual([json.loads(value) for value in restored.original_snapshots_json], before[:3])
        self.assertEqual([item.execution_id for item in restored.members], ['execution-1','execution-2','execution-3'])
        self.assertEqual([item.model_dump(mode='json') for item in self.items], before)
        self.assertEqual(len(self.service.store.for_thread('thread-a')), 4)
        self.assertTrue(all(item.status == 'queued' for item in self.service.store.for_thread('thread-a')))

    def test_reply_metadata_time_is_preserved_without_changing_destination_compatibility(self):
        items=deepcopy(self.items[:2]); items[1].reply_target.updated_at=2
        self.attempts[items[1].id] = self.attempts[items[1].id].model_copy(update={
            'queued_turn_snapshot':items[1].model_dump(mode='json')})
        plan=self.select(items)
        self.assertEqual(plan.members[1].reply_target.updated_at,2)
        self.assertEqual(json.loads(plan.original_snapshots_json[1])['reply_target']['updated_at'],2)

    def test_verified_replacement_preserves_immutable_original_and_current_route(self):
        originals=deepcopy(self.items[:2])
        self.service.replacement_resolver=lambda old, **scope:'replacement-thread'
        moved=[]
        for original in originals:
            current=original.model_copy(deep=True)
            current.thread_id='replacement-thread'; current.reply_target.thread_id='replacement-thread'
            self.attempts[current.id]=self.service.retain_queued(current,organization_id='local',workspace_id='default')
            moved.append(current)
        plan=self.select(moved)
        self.assertEqual(json.loads(plan.original_snapshots_json[0])['thread_id'],'thread-a')
        self.assertEqual(plan.members[0].thread_id,'replacement-thread')
        self.assertEqual(plan.members[0].reply_target.thread_id,'replacement-thread')

    def test_exact_profile_repository_reply_and_context_scope(self):
        for field, value in (('agent_profile_revision',8),('agent_profile_actor_id','foreign'),
                             ('execution_profile_id','write'),('repository_resource_id','repo-b'),
                             ('writable_repository_resource_ids',('repo-b',)),('project_id','foreign'),
                             ('reply_target',None),('sandbox','read-only')):
            items=deepcopy(self.items[:2]); items[1]=items[1].model_copy(update={field:value})
            with self.subTest(field=field):
                self.assertIsNone(self.select(items))

    def test_unknown_started_handoff_and_unattested_entries_stay_separate(self):
        for status in ('reconciliation_required','started','admitting','blocked','retrying'):
            attempts=dict(self.attempts)
            attempts[self.items[1].id]=attempts[self.items[1].id].model_copy(update={'status':status})
            with self.subTest(status=status):
                self.assertIsNone(self.select(self.items[:2], attempts_by_original_id=attempts))
        self.assertIsNone(self.select(self.items[:2], verified_notification_ids=frozenset({self.items[0].id})))
        handoff=deepcopy(self.items[:2]); handoff[1].handoff_requested_at=123
        self.assertIsNone(self.select(handoff))
        human=deepcopy(self.items[:2]); human[0].source='slack'
        self.assertIsNone(self.select(human))

    def test_no_message_based_attestation_and_snapshot_change_or_foreign_tenant(self):
        items=deepcopy(self.items[:2]); items[1].message='Changed request content'
        self.assertIsNone(self.select(items))
        self.assertIsNone(self.select(self.items[:2], organization_id='foreign'))
        self.assertIsNone(self.select(self.items[:2], workspace_id='foreign'))
        self.assertIsNone(self.select(self.items[:2], verified_notification_ids=frozenset()))

    def test_current_authority_scope_must_match_despite_same_profile(self):
        self.assertIsNone(self.select(self.items[:2], authority_scope_sha256_by_original_id={}))
        self.assertIsNone(self.select(self.items[:2], authority_scope_sha256_by_original_id={
            'original-1':'a'*64,'original-2':'b'*64}))

    def test_provider_selection_is_data_and_human_protocols_are_forbidden(self):
        items=deepcopy(self.items[:2])
        attempts=dict(self.attempts)
        for item in items:
            item.source='github'
            attempts[item.id]=attempts[item.id].model_copy(update={
                'source':'github','queued_turn_snapshot':item.model_dump(mode='json')})
        self.assertIsNotNone(self.select(items,attempts_by_original_id=attempts,notification_source_origin='github'))
        for source in ('web','slack','','SLACK','queued:slack'):
            with self.subTest(source=source),self.assertRaises(ValueError):
                self.select(notification_source_origin=source)

    def test_byte_bound_never_truncates_or_consumes_oversized_original(self):
        items=deepcopy(self.items)
        items[1].message='🧪' * 10000
        self.attempts[items[1].id]=self.service.retain_queued(items[1].model_copy(update={'id':'new-long'}), organization_id='local', workspace_id='default')
        # Use an entirely new original so the canonical store refuses no changed identity.
        items[1].id='new-long'; self.attempts[items[1].id]=self.attempts.pop('original-2')
        plan=self.select(items, verified_notification_ids=frozenset(item.id for item in items), max_context_bytes=10000,
                         authority_scope_sha256_by_original_id={item.id:'a'*64 for item in items})
        self.assertEqual([item.id for item in plan.members], ['original-1','original-3','original-4'])
        self.assertEqual(len(items[1].message),10000)
        self.assertLessEqual(len(plan.native_input().encode()),10000)

    def test_anchor_and_compatible_order_preserve_unrelated_work(self):
        plan=self.select(anchor_id='original-2')
        self.assertEqual([item.id for item in plan.members], ['original-2','original-3','original-4'])
        self.assertIsNone(self.select(anchor_id='missing'))
        with self.assertRaises(ValueError):
            self.select([self.items[0],self.items[0]])

    def test_fingerprint_rejects_changed_retained_snapshot_and_detaches_members(self):
        plan=self.select()
        member=plan.members[0]; member.message='Mutation of detached view'
        self.assertNotIn('Mutation of detached view',plan.native_input())
        data=plan.model_dump(mode='json'); value=json.loads(data['original_snapshots_json'][0]);value['message']='Tampered'
        data['original_snapshots_json'][0]=json.dumps(value)
        with self.assertRaises(ValidationError):
            NotificationBatchPlan.model_validate(data)

    def test_invalid_bounds_and_explicit_retry_do_not_join_batch(self):
        for changes in ({'max_members':17},{'max_members':1},{'max_members':2.5},{'max_members':True},
                        {'max_context_bytes':255},{'max_context_bytes':262145},{'max_context_bytes':65536.5}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                self.select(**changes)
        attempts=dict(self.attempts)
        attempts[self.items[1].id]=attempts[self.items[1].id].model_copy(update={'attempt_number':2})
        self.assertIsNone(self.select(self.items[:2],attempts_by_original_id=attempts))
