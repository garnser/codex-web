"""Paused-startup Home Goal recovery through existing canonical controls; private fixtures."""
import asyncio
import unittest
from tests.test_home_goal_session_custody import HomeGoalSessionCustodyTests
from codex_web.services.home_restart_recovery import HomeRestartRecoveryService
from codex_web.services.stale_active_turns import ActiveTurnRecoveryStore, StaleActiveTurnRecoveryService
from codex_web.services.autonomy_controller import AutonomyController
from codex_web.storage.autonomy import AutonomyStateStore
from codex_web.api.autonomy import build_autonomy_router
from starlette.requests import Request
from codex_web.identity import AuthenticationAssurance

class HomeGoalControlResumeTests(HomeGoalSessionCustodyTests):
 async def asyncSetUp(self):
  if self._testMethodName=="test_resume_cannot_bypass_exhausted_call_reservation":self.GOAL_MODEL_CALLS=1
  await super().asyncSetUp()

 async def prepare_paused_startup(self):
  repo=self.runtime_state.active_turns
  self.host._load_active_turns=repo.load;self.host._get_active_turn_record=repo.get
  self.host._put_active_turn_record=repo.put;self.host._delete_active_turn_record=repo.delete
  self.host._load_turn_queues=self.queue_store.load
  controller=AutonomyController(AutonomyStateStore(self.f.sqlite))
  controller.store.set_control(self.control,actor_id=self.actor.identity_id)
  self.goal_continuation.autonomy=controller
  active=await self.start_home_goal_native()
  restart=HomeRestartRecoveryService(home_host=self.service.home_operator_host,
   recovery_store=ActiveTurnRecoveryStore(self.f.sqlite),execution=self.execution,
   intents=self.intents,preflight=self.execution.queued_preflight)
  restart.goal_continuation=self.home_goal;self.home_goal.restart_recovery=restart
  self.execution.home_restart_recovery=restart
  stale=StaleActiveTurnRecoveryService(active_turns=repo,turn_queues=self.queue_store,
   worker_state_loader=self.f.workers.store.load_execution_state,store=restart.store,
   schedule_queue_drain=self.execution.schedule_queue_drain,append_event=self.host.events.append,
   resume_active_threads=self.execution.resume_active_threads_after_startup,resume_live_on_startup=True,
   control_retry_eligible=self.execution.home_startup_control_retry_eligible,
   resume_control_blocked=self.execution.resume_control_blocked_home_threads)
  self.addAsyncCleanup(stale.coordinator.stop)
  self.execution.home_startup_retry_schedule=lambda thread:stale.schedule_thread(thread,reason="control-resume")
  controller.pause(actor_id=self.actor.identity_id)
  first=await stale.reconcile(reason='startup',actor_id=self.actor.identity_id)
  self.assertEqual(first.resume_thread_ids,(self.thread,))
  self.assertEqual(repo.get(self.thread),active)
  self.assertEqual(self.queue_store.get(self.thread),[])
  self.assertTrue(any(x.get('type')=='home_restart_recovery_blocked' for x in self.host.events))
  router=build_autonomy_router(controller,control_changed=self.execution.resume_automated_queues)
  resume=next(route.endpoint for route in router.routes if route.path=='/api/autonomy/resume')
  actor=self.actor.model_copy(update={'assurance':AuthenticationAssurance.MFA})
  request=Request({'type':'http','state':{'identity_actor':actor}})
  return active,restart,stale,resume,request

 async def resume_and_join(self, stale, resume, request):
  await resume(request)
  pending=list(stale.coordinator._tasks.values())
  if pending:await asyncio.wait_for(asyncio.gather(*pending),5.)
  self.assertFalse(stale.coordinator._last_error)

 async def test_supported_resume_retains_same_goal_and_starts_one_recovery(self):
  active,restart,stale,resume,request=await self.prepare_paused_startup()
  await self.resume_and_join(stale,resume,request)
  queued=self.queue_store.get(self.thread)
  self.assertEqual(len(queued),1,self.host.events)
  self.assertEqual(queued[0].home_restart_input.schema_version,'1.5')
  await self.resume_and_join(stale,resume,request)
  self.assertEqual([row.id for row in self.queue_store.get(self.thread)],[queued[0].id])
  await self.execution.drain_thread_queue(self.thread)
  self.assertEqual(len(self.starts()),2,self.host.events)
  current=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
  self.assertEqual(current.home_continuation.delivery.native_turn_id,active.turn_id)
  self.assertEqual(current.home_continuation.reserved_calls,2)
  self.assertEqual(current.home_continuation.model_calls,0) # Original usage is still UNKNOWN, never invented.
  self.assertEqual(restart.store.get(self.thread).original,active)
  await self.resume_and_join(stale,resume,request)
  self.assertEqual(len(self.starts()),2)
  self.assertEqual(self.queue_store.get(self.thread),[])

 async def test_resume_racing_paused_startup_refusal_keeps_one_wakeup(self):
  import threading
  active,restart,stale,resume,request=await self.prepare_paused_startup()
  self.execution._home_startup_control_wait.clear()
  entered=asyncio.Event();release=threading.Event();loop=asyncio.get_running_loop()
  original=restart.retain_startup
  def held(*args,**kwargs):
   try:return original(*args,**kwargs)
   except ValueError as exc:
    if str(exc)=='global_autonomy_paused':
     loop.call_soon_threadsafe(entered.set)
     if not release.wait(5.):raise AssertionError('Private release missing')
    raise
  restart.retain_startup=held
  task=asyncio.create_task(stale.reconcile(reason='startup',actor_id=self.actor.identity_id))
  try:
   await asyncio.wait_for(entered.wait(),5.)
   await resume(request) # No wait observation exists yet.
   release.set()
   await asyncio.wait_for(task,5.)
   pending=list(stale.coordinator._tasks.values())
   if pending:await asyncio.wait_for(asyncio.gather(*pending),5.)
   self.assertFalse(stale.coordinator._last_error)
   self.assertEqual(len(self.queue_store.get(self.thread)),1,self.host.events)
   self.assertEqual(len(self.starts()),1)
  finally:
   release.set()
   await asyncio.wait_for(task,5.)

 async def test_resume_cannot_restart_changed_active_marker(self):
  active,restart,stale,resume,request=await self.prepare_paused_startup()
  changed=active.model_copy(update={'turn_id':'different-positive-turn'})
  self.runtime_state.active_turns.put(self.thread,changed)
  await self.resume_and_join(stale,resume,request)
  self.assertEqual(self.runtime_state.active_turns.get(self.thread),changed)
  self.assertEqual(self.queue_store.get(self.thread),[])
  self.assertEqual(len(self.starts()),1)

 async def test_resume_rechecks_paused_goal(self):
  from codex_web.goals import GoalTransitionRequest
  active,restart,stale,resume,request=await self.prepare_paused_startup()
  self.goals.transition(self.goal.id,GoalTransitionRequest(status='paused',reason='Operator paused this Goal'),scope=self.actor.tenant,actor_id=self.actor.identity_id)
  await self.resume_and_join(stale,resume,request)
  self.assertEqual(self.runtime_state.active_turns.get(self.thread),active)
  self.assertEqual(self.queue_store.get(self.thread),[])
  self.assertEqual(len(self.starts()),1)

 async def test_resume_rechecks_current_home_selection(self):
  active,restart,stale,resume,request=await self.prepare_paused_startup()
  self.select(None,1)
  await self.resume_and_join(stale,resume,request)
  self.assertEqual(self.runtime_state.active_turns.get(self.thread),active)
  self.assertEqual(self.queue_store.get(self.thread),[])
  self.assertEqual(len(self.starts()),1)

 async def test_resume_cannot_bypass_exhausted_call_reservation(self):
  active,restart,stale,resume,request=await self.prepare_paused_startup()
  await self.resume_and_join(stale,resume,request)
  self.assertEqual(self.runtime_state.active_turns.get(self.thread),active)
  self.assertEqual(self.queue_store.get(self.thread),[])
  self.assertEqual(len(self.starts()),1)

def load_tests(loader,tests,pattern):
 return unittest.TestSuite(HomeGoalControlResumeTests(name) for name in loader.getTestCaseNames(HomeGoalControlResumeTests) if name in HomeGoalControlResumeTests.__dict__)
