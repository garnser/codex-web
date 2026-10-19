"""Custody recovery publication using canonical keyed assignment storage."""
import unittest
from types import SimpleNamespace
from fastapi import HTTPException
from codex_web.execution_workers import ExecutionAssignment
from codex_web.storage.execution_workers import ExecutionWorkerStore
from tests import test_queued_bootstrap_runtime as fixtures


class CustodyBootstrapRecoveryTests(unittest.IsolatedAsyncioTestCase):
    reopen = fixtures.QueuedBootstrapRuntimeTests.reopen
    make_runtime = fixtures.QueuedBootstrapRuntimeTests.make_runtime
    recover = fixtures.QueuedBootstrapRuntimeTests.recover
    custody_refusal = fixtures.QueuedBootstrapRuntimeTests.custody_refusal

    def setUp(self):
        fixtures.QueuedBootstrapRuntimeTests.setUp(self)
        self.custody_refusal()
        self.worker_store = ExecutionWorkerStore(self.service.store.store)
        self.persist('old-assignment')
        self.runtime.assignments = self.worker_store.assignment

    def persist(self, key):
        value = vars(self.assignments[key]).copy()
        profile = vars(value.pop('agent_profile'))
        profile['profile_record_id'] = 'pinned-profile-record'
        value.pop('repository_target', None)
        value.setdefault('subject', {'kind':'thread_bootstrap','ref':'old-bootstrap'})
        value.setdefault('sandbox', 'workspace-write')
        value.setdefault('approval_policy', 'on-request')
        value.pop('repository_scope', None)
        scope = dict(organization_id='org', workspace_id='ws', project_id='project', source='explicit',
            writable_repository_ids=['repo'], read_only_repository_ids=['reference'])
        if isinstance(value.get('subject'), SimpleNamespace):
            value['subject'] = vars(value['subject'])
        model = ExecutionAssignment(id=key, execution_contract_version='1.0', required_capabilities=(),
            network={}, limits={}, repository_scope=scope, agent_profile=profile,
            repository_target={k:v for k,v in scope.items() if k != 'writable_repository_ids'} | {'mutable_repository_id':'repo'},
            **({'resource_ids':('repo','reference')} if 'resource_ids' not in value else {}), **value)
        self.worker_store._ensure_records()
        self.service.store.store.record_apply(self.worker_store.assignment_namespace,
            upserts={key:model.model_dump(mode='json')})
        if key == 'old-assignment':
            # The controller serializes the full canonical repository scope.
            current = self.service.store.get(self.attempt.id)
            blocker = current.blockers[0].model_copy(update={
                'requested_repository_scope':model.repository_scope.model_dump(mode='json')})
            self.attempt = self.service.store.update(current.id, lambda row:row.model_copy(update={'blockers':(blocker,)}))

    async def create(self, **values):
        result = await fixtures.QueuedBootstrapRuntimeTests.create(self, **values)
        self.persist('assignment')
        return result

    async def test_publication_preserves_source_assignment_and_reconciles_created_receipt(self):
        before = self.worker_store.assignment('old-assignment').model_dump(mode='json')
        result = await self.recover()
        self.assertEqual(result['threadId'], 'fresh')
        self.assertEqual(self.worker_store.assignment('old-assignment').model_dump(mode='json'), before)
        self.assertEqual(self.service.store.get(self.attempt.id).queued_turn_snapshot, self.attempt.queued_turn_snapshot)
        self.assertIsNone(self.service.store.get(self.attempt.id).queued_start_receipt)
        await self.recover()
        self.assertEqual(self.threads.create.await_count, 1)

    async def test_changed_source_after_creation_refuses_route_and_retains_pending_receipt(self):
        async def change_source(**values):
            result = await self.create(**values)
            source = self.worker_store.assignment('old-assignment')
            self.service.store.store.record_apply(self.worker_store.assignment_namespace,
                upserts={source.id:source.model_copy(update={'fence':3}).model_dump(mode='json')})
            return result
        self.threads.create.side_effect = change_source
        with self.assertRaises(HTTPException):
            await self.recover()
        self.assertIsNone(self.index.replacement_thread_id('archived'))
        self.assertEqual(self.service.store.get(self.attempt.id).bootstrap_recovery.state, 'pending')

    async def test_changed_fresh_actor_after_creation_refuses_route(self):
        async def change_target(**values):
            result = await self.create(**values)
            target = self.worker_store.assignment('assignment')
            self.service.store.store.record_apply(self.worker_store.assignment_namespace,
                upserts={target.id:target.model_copy(update={'created_by':'another-actor'}).model_dump(mode='json')})
            return result
        self.threads.create.side_effect = change_target
        with self.assertRaises(HTTPException):
            await self.recover()
        self.assertIsNone(self.index.replacement_thread_id('archived'))
