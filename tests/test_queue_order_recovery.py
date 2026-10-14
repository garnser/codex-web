import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from codex_web.identity import AuthenticationActor
from codex_web.models import Project, QueuedTurn, QueueOrderRepairRequest
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.queue_order_recovery import QueueOrderRecoveryService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.turn_queue import TurnQueueRepository


class QueueOrderRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'state.sqlite'
        self.state = SQLiteStateStore(self.path)
        self.queues = TurnQueueRepository(self.state, Path(self.temp.name)/'queue.json')
        self.preflight = ExecutionPreflightService(ExecutionPreflightStore(self.state))
        self.actor = AuthenticationActor(identity_id='admin-fixture',principal_kind='human',
            organization_id='local',workspace_id='default',roles=('admin',),assurance='local_trusted')
        self.project = Project(id='project-fixture',name='Fixture',path='/workspace/fixture')
        self.first = QueuedTurn(id='b843d0a3504e',thread_id='thread-fixture',project_id=self.project.id,
            message='Sanitized oldest original',execution_id='first-fixture',created_at=1791778009.4276829,
            agent_profile_id='profile-fixture',agent_profile_revision=7,agent_profile_actor_id=self.actor.identity_id,
            repository_resource_id='repository-fixture',writable_repository_resource_ids=('repository-fixture',))
        self.second = self.first.model_copy(update={'id':'bbf52558a87e', 'message':'Sanitized later original',
            'execution_id':'second-fixture','created_at':1791782056.4454837})
        for item in (self.first,self.second):
            retained=self.preflight.retain_queued(item,organization_id='local',workspace_id='default')
            _,claim=self.preflight.claim_queued_admission(retained.id)
            self.preflight.begin_queued_start(retained.id,claim_id=claim)
        # Restart reads turn active admissions into the existing unknown domain.
        self.preflight = ExecutionPreflightService(ExecutionPreflightStore(SQLiteStateStore(self.path)))
        for item in (self.first,self.second):
            self.preflight.store.get('preflight-queued-'+item.id)
        self.queues.put('thread-fixture',[self.second,self.first])
        self.active=False
        self.events=[]
        self.service=QueueOrderRecoveryService(queues=self.queues,preflight=self.preflight,
            replacement_resolver=lambda *args,**kwargs:None,thread_is_active=lambda _:self.active,
            event_sink=self.events.append)

    def tearDown(self):
        self.temp.cleanup()

    def preview(self, actor=None):
        return self.service.preview('thread-fixture',project=self.project,actor=actor or self.actor)

    async def repair(self, preview=None, actor=None):
        preview=preview or self.preview()
        return await self.service.repair('thread-fixture',QueueOrderRepairRequest(
            expected_snapshot_sha256=preview['snapshot_sha256'],reason='Repair observed maintenance order'),
            project=self.project,actor=actor or self.actor)

    async def test_guarded_repair_keeps_complete_originals_and_unknown_outcomes_across_reopen(self):
        before=self.state.record_items('execution_preflight_attempts')
        preview=self.preview()
        self.assertTrue(preview['can_repair'])
        self.assertEqual(preview['proposed_order'],[self.first.id,self.second.id])
        result=await self.repair(preview)
        reopened=TurnQueueRepository(SQLiteStateStore(self.path),Path(self.temp.name)/'queue.json')
        self.assertEqual([q.model_dump(mode='json') for q in reopened.get('thread-fixture')],
            [self.first.model_dump(mode='json'),self.second.model_dump(mode='json')])
        self.assertEqual(self.state.record_items('execution_preflight_attempts'),before)
        self.assertFalse(result['repair']['native_outcomes_changed'])
        self.assertTrue(result['audit_recorded'])
        self.assertEqual(self.events[0]['actor_id'],self.actor.identity_id)
        self.assertNotIn('Sanitized',str(self.events))
        with self.assertRaises(HTTPException):
            await self.repair(preview)

    async def test_queue_and_intent_changes_invalidate_preview(self):
        preview=self.preview()
        attempt=self.preflight.store.get('preflight-queued-'+self.first.id)
        self.preflight.store.update(attempt.id,lambda value:value.model_copy(update={'attempt_number':2}))
        with self.assertRaises(HTTPException) as error:
            await self.repair(preview)
        self.assertEqual(error.exception.detail['code'],'queue_order_snapshot_stale')
        preview=self.preview()
        self.queues.put('thread-fixture',[self.first,self.second])
        with self.assertRaises(HTTPException):
            await self.repair(preview)

    async def test_explicit_priority_preserved_without_changing_native_status(self):
        row=self.preflight.store.get('preflight-queued-'+self.second.id)
        self.preflight.store.update(row.id,lambda value:value.model_copy(update={'attempt_number':2}))
        preview=self.preview()
        self.assertEqual(preview['proposed_order'],[self.second.id,self.first.id])
        self.assertFalse(preview['can_repair'])

    async def test_active_claim_and_turn_refuse_repair(self):
        self.active=True
        self.assertEqual(self.preview()['blockers'][0]['code'],'queue_order_turn_active')
        self.active=False
        row=self.preflight.store.get('preflight-queued-'+self.first.id)
        self.preflight.store.update(row.id,lambda value:value.model_copy(update={'status':'retrying'}))
        self.assertEqual(self.preview()['blockers'][0]['code'],'queue_order_admission_active')

    async def test_authority_assurance_tenant_and_project_do_not_leak_or_mutate(self):
        before=self.queues.get('thread-fixture')
        for actor in (self.actor.model_copy(update={'roles':('viewer',)}),
                      self.actor.model_copy(update={'assurance':'primary'})):
            self.assertFalse(self.preview(actor)['can_repair'])
            with self.assertRaises(HTTPException):await self.repair(actor=actor)
        with self.assertRaises(HTTPException) as denied:
            self.preview(self.actor.model_copy(update={'workspace_id':'foreign'}))
        self.assertEqual(denied.exception.status_code,404)
        with self.assertRaises(HTTPException):
            await self.service.repair('thread-fixture',QueueOrderRepairRequest(
                expected_snapshot_sha256=self.preview()['snapshot_sha256'],reason='Fixture'),
                project=self.project.model_copy(update={'id':'foreign-project'}),actor=self.actor)
        self.assertEqual(self.queues.get('thread-fixture'),before)

    async def test_missing_or_changed_original_refuses_proposal(self):
        changed=self.first.model_copy(update={'agent_profile_revision':8})
        self.queues.put('thread-fixture',[self.second,changed])
        self.assertEqual(self.preview()['blockers'][0]['code'],'queue_order_snapshot_changed')
        self.queues.put('thread-fixture',[self.first.model_copy(update={'id':'unretained'})])
        self.assertEqual(self.preview()['blockers'][0]['code'],'queue_order_provenance_missing')

    async def test_lost_ownership_cannot_mutate_queue(self):
        before=self.queues.get('thread-fixture')
        class Busy:
            async def run_exclusive(self,name,operation):return False,None
        self.service.ownership=Busy()
        with self.assertRaises(HTTPException) as error:await self.repair()
        self.assertEqual(error.exception.detail['code'],'queue_order_ownership_busy')
        self.assertEqual(self.queues.get('thread-fixture'),before)

    async def test_audit_failure_reports_committed_order_without_native_replay(self):
        self.service.event_sink=lambda event:(_ for _ in ()).throw(RuntimeError('Audit unavailable'))
        result=await self.repair()
        self.assertFalse(result['audit_recorded'])
        self.assertEqual([q.id for q in self.queues.get('thread-fixture')],[self.first.id,self.second.id])

    async def test_replacement_cycle_and_unsupported_snapshot_fail_closed(self):
        self.service.replacement_resolver=lambda *args,**kwargs:(_ for _ in ()).throw(ValueError('cycle fixture'))
        self.assertEqual(self.preview()['blockers'][0]['code'],'queue_order_route_unverified')
        self.service.replacement_resolver=lambda *args,**kwargs:None
        row=self.preflight.store.get('preflight-queued-'+self.first.id)
        snapshot={**row.queued_turn_snapshot,'future_contract_field':'fixture'}
        self.preflight.store.update(row.id,lambda value:value.model_copy(update={'queued_turn_snapshot':snapshot}))
        self.assertEqual(self.preview()['blockers'][0]['code'],'queue_order_snapshot_unsupported')

    async def test_changed_fence_fails_inside_queue_transaction(self):
        before=self.queues.get('thread-fixture')
        class Lost:
            async def run_exclusive(self,name,operation):return True,await operation()
            def require_fence(self,name):raise HTTPException(status_code=409,detail={'code':'lost_fence'})
        self.service.ownership=Lost()
        with self.assertRaises(HTTPException) as error:await self.repair()
        self.assertEqual(error.exception.detail['code'],'lost_fence')
        self.assertEqual(self.queues.get('thread-fixture'),before)

    async def test_http_routes_enforce_canonical_thread_scope_and_protected_payload(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from codex_web.api.turns import build_turns_router
        from codex_web.models import IndexedThread
        from codex_web.services.projects import ProjectService
        from codex_web.services.thread_scope import ThreadScopeService
        from codex_web.storage.thread_index import ThreadIndexRepository
        index=ThreadIndexRepository(self.state,Path(self.temp.name)/'threads.json')
        index.upsert(IndexedThread(id='thread-fixture',name='Fixture',project_id=self.project.id))
        projects=ProjectService(SimpleNamespace(load=lambda:[self.project]))
        sessions=SimpleNamespace(store=SimpleNamespace(list=lambda:[]))
        scope=ThreadScopeService(projects,sessions,index)
        app=FastAPI()
        self.request_actor=self.actor
        @app.middleware('http')
        async def identity(request,call_next):
            request.state.identity_actor=self.request_actor
            return await call_next(request)
        app.include_router(build_turns_router(SimpleNamespace(),scope,self.service))
        with TestClient(app) as client:
            path='/api/threads/thread-fixture/queue/order-recovery'
            response=client.get(path)
            self.assertEqual(response.status_code,200)
            payload={'expected_snapshot_sha256':response.json()['snapshot_sha256'],'reason':'Fixture repair'}
            self.assertEqual(client.post(path,json={**payload,'native_outcome':'unstarted'}).status_code,422)
            self.assertEqual(client.get(path+'?project_id=foreign-project').status_code,404)
            self.request_actor=self.actor.model_copy(update={'assurance':'primary'})
            self.assertEqual(client.post(path,json=payload).status_code,403)
            self.request_actor=self.actor.model_copy(update={'workspace_id':'foreign'})
            self.assertEqual(client.get(path).status_code,404)
            self.request_actor=self.actor
            self.assertEqual(client.post(path,json=payload).status_code,200)
            self.assertEqual([q.id for q in self.queues.get('thread-fixture')],[self.first.id,self.second.id])

    async def test_verified_replacement_projects_only_scheduling_thread_and_reply_route(self):
        from codex_web.models import BotReplyTarget
        originals=[]
        for item in (self.first,self.second):
            row=self.preflight.store.get('preflight-queued-'+item.id)
            original=item.model_copy(update={'reply_target':BotReplyTarget(thread_id=item.thread_id,
                provider='slack',external_conversation_id='fixture-channel',updated_at=1)})
            self.preflight.store.update(row.id,lambda value,original=original:value.model_copy(update={
                'queued_turn_snapshot':original.model_dump(mode='json'),'replacement_thread_id':'verified-current'}))
            originals.append(original.model_copy(update={'thread_id':'verified-current',
                'reply_target':original.reply_target.model_copy(update={'thread_id':'verified-current'})}))
        self.queues.put('verified-current',list(reversed(originals)))
        self.service.replacement_resolver=lambda *args,**kwargs:'verified-current'
        before=self.state.record_items('execution_preflight_attempts')
        preview=self.service.preview('verified-current',project=self.project,actor=self.actor)
        self.assertTrue(preview['can_repair'])
        await self.service.repair('verified-current',QueueOrderRepairRequest(
            expected_snapshot_sha256=preview['snapshot_sha256'],reason='Restore verified current route order'),
            project=self.project,actor=self.actor)
        self.assertEqual(self.queues.get('verified-current'),originals)
        self.assertEqual(self.state.record_items('execution_preflight_attempts'),before)
        self.service.replacement_resolver=lambda *args,**kwargs:'foreign-route'
        self.assertEqual(self.service.preview('verified-current',project=self.project,actor=self.actor)
            ['blockers'][0]['code'],'queue_order_route_unverified')
