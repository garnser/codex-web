"""Canonical active Home Goal restart qualification; no app import or live fault."""
import unittest
from codex_web.services.home_restart_recovery import HomeRestartRecoveryService
from codex_web.services.stale_active_turns import ActiveTurnRecoveryStore
from codex_web.runtime.execution import ExecutionPrestartRefusal
from tests.test_home_goal_continuation import HomeGoalContinuationTests

class HomeGoalRestartTests(HomeGoalContinuationTests):
    async def test_started_home_goal_is_a_canonical_restart_origin(self):
        active=await self.start_home_goal_native()
        binding=self.goal_bindings.get(self.goal_binding.id,scope=self.actor.tenant)
        self.assertEqual(binding.home_continuation.delivery.phase,'started')
        self.assertEqual(binding.home_continuation.delivery.native_turn_id,active.turn_id)
        restart=HomeRestartRecoveryService(home_host=self.service.home_operator_host,
            recovery_store=ActiveTurnRecoveryStore(self.f.sqlite),execution=self.execution,
            intents=self.intents,preflight=self.execution.queued_preflight)
        try:
            queued=restart.retain_startup(active,project=self.projects.get('home',self.actor.tenant),actor=self.actor)
        except ExecutionPrestartRefusal as exc:
            self.fail('Actual canonical Goal START cannot retain restart continuation: '+str(exc.detail))
        self.assertEqual(queued.thread_id,active.thread_id)
        self.assertEqual(queued.source,'restart-recovery:home')
        self.assertIsNotNone(queued.home_restart_input)
        self.assertEqual(len(self.starts()),1) # retention cannot repeat original native input

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(HomeGoalRestartTests(name) for name in HomeGoalRestartTests.__dict__ if name.startswith('test_'))
