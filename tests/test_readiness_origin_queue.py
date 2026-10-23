"""Typed pre-input cursor continuation, canonical queue claims and no native replay."""
import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from codex_web.models import Project
from codex_web.runtime.execution import ReadinessOriginPrestartDeferred, TurnExecutionService
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests.test_turn_execution import _Host


def progress(cursor='page1', epoch='e'*32):
    return ReadinessOriginPrestartDeferred(status_code=409, detail={
        'code':'readiness_origin_index_progressing', 'epoch':epoch,
        'cursor_sha256':hashlib.sha256(cursor.encode()).hexdigest(), 'retryable':True})


class ReadinessOriginQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.host=_Host();self.host._project=lambda _:Project(id='project',name='Project',path='/private')
        self.runtime=TurnExecutionService(self.host)
        self.preflight=ExecutionPreflightService(ExecutionPreflightStore(SQLiteStateStore(Path(self.temp.name)/'state.sqlite')))
        self.runtime.queued_preflight=self.preflight;self.runtime.schedule_queue_drain=Mock()
        self.original=self.runtime.enqueue_turn(thread_id='thread',project_id='project',message='same original',source='web')
        self.attempt=self.preflight.retain_queued(self.original,organization_id='local',workspace_id='default')
        self.before=self.original.model_dump(mode='json');self.wakes=[]
        self.runtime.start_thread_turn_now=AsyncMock(side_effect=progress())

    async def drain(self):
        loop=asyncio.get_running_loop()
        with patch.object(loop,'call_later',side_effect=lambda *args:self.wakes.append(args)):
            await self.runtime.drain_thread_queue('thread')

    async def test_same_original_queued_and_woken_then_started_once(self):
        await self.drain()
        self.assertEqual([q.model_dump(mode='json') for q in self.host.queues['thread']],[self.before])
        current=self.preflight.store.get(self.attempt.id)
        self.assertEqual(current.status,'queued');self.assertEqual(current.queued_turn_snapshot,self.before)
        self.assertIsNone(current.queued_start_correlation);self.assertIsNone(current.started_at)
        self.assertEqual(len(self.wakes),1);self.assertEqual(self.wakes[0][0],1)
        event=next(e for e in self.host.events if e['type']=='queued_turn_waiting_for_readiness_origin_index')
        self.assertIs(event['continuation_scheduled'],True);json.dumps(event)
        self.runtime.start_thread_turn_now=AsyncMock(return_value={'turn':{'id':'one-native'}})
        await self.drain();await self.drain()
        self.runtime.start_thread_turn_now.assert_awaited_once()
        self.assertEqual(self.preflight.store.get(self.attempt.id).status,'started')
        self.assertEqual(self.host._thread_queue('thread'),[])

    async def test_same_cursor_has_no_second_wakeup_or_immediate_spin(self):
        await self.drain();await self.drain()
        self.assertEqual(len(self.wakes),1);self.runtime.schedule_queue_drain.assert_not_called()
        self.assertEqual(self.preflight.store.get(self.attempt.id).status,'queued')
        self.assertEqual(self.host.queues['thread'][0].model_dump(mode='json'),self.before)

    async def test_changing_cursor_has_finite_budget(self):
        self.runtime.start_thread_turn_now.side_effect=[progress(str(i)) for i in range(10)]
        for _ in range(10):await self.drain()
        self.assertEqual(len(self.wakes),8)
        self.assertEqual(len(self.runtime._readiness_origin_waits),1)
        self.assertEqual(self.host.queues['thread'][0].model_dump(mode='json'),self.before)

    async def test_provider_detail_code_is_never_a_noninput_witness(self):
        self.runtime.start_thread_turn_now.side_effect=HTTPException(status_code=409,detail=progress().detail)
        await self.drain()
        self.assertEqual(self.preflight.store.get(self.attempt.id).status,'reconciliation_required')
        self.assertEqual(self.host._thread_queue('thread'),[]);self.assertEqual(self.wakes,[])

    async def test_changed_authority_on_wake_is_not_bypassed(self):
        await self.drain()
        self.runtime._require_automated_admission=Mock(side_effect=PermissionError('current actor revoked'))
        self.runtime.start_thread_turn_now.reset_mock()
        await self.drain()
        self.runtime.start_thread_turn_now.assert_not_awaited()
        self.assertEqual(self.preflight.store.get(self.attempt.id).status,'reconciliation_required')
        self.assertEqual(len(self.wakes),1)

    async def test_fenced_claim_never_restores_original_or_schedules(self):
        async def refuse(*args,**kwargs):
            self.preflight.store.update(self.attempt.id,lambda a:a.model_copy(update={'queued_admission_claim_id':'new-owner'}))
            raise progress()
        self.runtime.start_thread_turn_now.side_effect=refuse
        await self.drain()
        current=self.preflight.store.get(self.attempt.id)
        self.assertEqual(current.queued_admission_claim_id,'new-owner');self.assertEqual(current.status,'reconciliation_required')
        self.assertEqual(self.host._thread_queue('thread'),[]);self.assertEqual(self.wakes,[])

    async def test_real_committed_native_evidence_always_wins_over_local_refusal(self):
        from codex_web.models import QueuedNativeStartEvidence, QueuedNativeResumeEvidence
        for field,value in (
            ('queued_start_evidence',QueuedNativeStartEvidence(runtime_generation='g',rpc_request_id='r',provider_native_session_id='s',provider_native_turn_id='t',observed_at=1)),
            ('queued_resume_evidence',QueuedNativeResumeEvidence(runtime_generation='g',rpc_request_id='r',provider_native_session_id='s',outcome='resumed',observed_at=1)),
            ('started_at',1.0)):
            with self.subTest(field=field):
                await self.asyncSetUp()
                async def refuse(*args,**kwargs):
                    self.preflight.store.update(self.attempt.id,lambda a:a.model_copy(update={field:value}))
                    raise progress()
                self.runtime.start_thread_turn_now.side_effect=refuse
                await self.drain()
                self.assertEqual(self.host._thread_queue('thread'),[]);self.assertEqual(self.wakes,[])
                current=self.preflight.store.get(self.attempt.id)
                self.assertEqual(current.status,'reconciliation_required');self.assertEqual(getattr(current,field),value)

    async def test_changed_epoch_or_expired_window_never_auto_wakes(self):
        for which in ('epoch','deadline'):
            with self.subTest(which=which):
                await self.asyncSetUp();await self.drain()
                if which=='epoch':self.runtime.start_thread_turn_now.side_effect=progress('next','f'*32)
                else:
                    key=('thread',self.original.id);old=self.runtime._readiness_origin_waits[key]
                    self.runtime._readiness_origin_waits[key]=(old[0],old[1],0)
                    self.runtime.start_thread_turn_now.side_effect=progress('next')
                await self.drain();self.assertEqual(len(self.wakes),1)
                self.assertEqual(self.host._thread_queue('thread')[0].model_dump(mode='json'),self.before)

    async def test_changed_snapshot_or_current_route_never_restores_old_request(self):
        for update in ({'replacement_thread_id':'other'}, {'queued_turn_snapshot':{**self.before,'message':'different'}}):
            with self.subTest(update=update):
                await self.asyncSetUp()
                async def refuse(*args,**kwargs):
                    self.preflight.store.update(self.attempt.id,lambda a:a.model_copy(update=update))
                    raise progress()
                self.runtime.start_thread_turn_now.side_effect=refuse
                await self.drain()
                self.assertEqual(self.host._thread_queue('thread'),[]);self.assertEqual(self.wakes,[])
                self.assertEqual(self.preflight.store.get(self.attempt.id).status,'reconciliation_required')

    async def test_invalid_progress_digest_is_retained_without_wake(self):
        error=progress();error.detail['cursor_sha256']='invalid'
        self.runtime.start_thread_turn_now.side_effect=error
        await self.drain()
        self.assertEqual(self.wakes,[]);self.assertEqual(self.host._thread_queue('thread'),[])
        self.assertEqual(self.preflight.store.get(self.attempt.id).status,'blocked')

    async def test_wakeup_waits_for_original_drain_owner_instead_of_being_lost(self):
        self.runtime.schedule_queue_drain=TurnExecutionService.schedule_queue_drain.__get__(self.runtime)
        entered,release,started=asyncio.Event(),asyncio.Event(),asyncio.Event();calls=0
        async def start(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==1:raise progress()
            started.set();return {'turn':{'id':'one-native'}}
        async def publish(_thread):
            if calls==1:
                entered.set();await release.wait()
        self.runtime.start_thread_turn_now=start;self.runtime.publish_queue_status=publish
        loop=asyncio.get_running_loop();original=loop.call_later
        def later(delay,callback,*args,**kwargs):
            if delay==1:self.wakes.append((delay,callback,*args));return Mock()
            return original(delay,callback,*args,**kwargs)
        with patch.object(loop,'call_later',side_effect=later):
            self.runtime.schedule_queue_drain('thread')
            await asyncio.wait_for(entered.wait(),2)
            owned=self.runtime.queue_drain_tasks['thread']
            self.wakes[0][1]()  # Timer fires while original drain is still publishing.
            self.assertEqual(calls,1)
            release.set();await owned
            await asyncio.wait_for(started.wait(),2)
            current=self.runtime.queue_drain_tasks.get('thread')
            if current is not None:await current
        self.assertEqual(calls,2);self.assertEqual(self.preflight.store.get(self.attempt.id).status,'started')

    async def test_late_timer_or_owner_completion_never_crosses_continuation_deadline(self):
        for owner_pending in (False,True):
            with self.subTest(owner_pending=owner_pending):
                await self.asyncSetUp()
                self.runtime.schedule_queue_drain=TurnExecutionService.schedule_queue_drain.__get__(self.runtime)
                entered,release=asyncio.Event(),asyncio.Event()
                async def publish(_thread):
                    entered.set();await release.wait()
                self.runtime.publish_queue_status=publish
                loop=asyncio.get_running_loop();original=loop.call_later
                def later(delay,callback,*args,**kwargs):
                    if delay==1:self.wakes.append((delay,callback,*args));return Mock()
                    return original(delay,callback,*args,**kwargs)
                with patch.object(loop,'call_later',side_effect=later):
                    self.runtime.schedule_queue_drain('thread');await asyncio.wait_for(entered.wait(),2)
                    owned=self.runtime.queue_drain_tasks['thread'];callback=self.wakes[0][1]
                    if owner_pending:callback()  # Register completion wake while still inside budget.
                    deadline=self.runtime._readiness_origin_waits[('thread',self.original.id)][2]
                    with patch('codex_web.runtime.execution.time',SimpleNamespace(monotonic=lambda:deadline+1)):
                        if not owner_pending:callback()  # Timer itself delivered late.
                        release.set();await owned;await asyncio.sleep(0)
                self.runtime.start_thread_turn_now.assert_awaited_once()
                self.assertEqual(self.preflight.store.get(self.attempt.id).status,'queued')
                self.assertEqual(self.host._thread_queue('thread')[0].model_dump(mode='json'),self.before)
