from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException

from codex_web.autonomy import AutonomyMode, AutonomyCycleOutcome
from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind, TenantScope
from codex_web.models import Project, ProjectDeliveryConfiguration, ProjectDeliveryUpdate, TaskSourceConfiguration, WorkItemState
from codex_web.services.project_delivery import ProjectDeliveryService


class ProjectDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000.0
        self.actor = AuthenticationActor(identity_id="operator", principal_kind=PrincipalKind.HUMAN,
            organization_id="local", workspace_id="default", assurance=AuthenticationAssurance.LOCAL_TRUSTED,
            roles=("admin",))
        self.project = Project(id="project", name="Project", path="/repo",
            authoritative_task_source=TaskSourceConfiguration(source_type="github", source_instance="https://api.github.com", scope="owner/repo"),
            delivery_supervision=ProjectDeliveryConfiguration(thread_id="delivery-thread", actor_identity_id="operator"))
        self.items = {}
        self.projects = Mock()
        self.projects.list.return_value = [self.project]
        self.projects.get.return_value = self.project
        self.identity = Mock()
        self.identity.actor_for_identity.return_value = self.actor
        self.scope = Mock()
        self.operator = SimpleNamespace(sync_project=AsyncMock(return_value={"ok": True}), reconcile=AsyncMock())
        self.turns = SimpleNamespace(start=AsyncMock(return_value={"queued": True}), queue=Mock(return_value={"queueDepth": 0}))
        self.execution = SimpleNamespace(thread_is_active=Mock(return_value=False))
        self.controller = Mock()
        self.control = SimpleNamespace(mode=AutonomyMode.ACTIVE, dry_run=False, simulation=False)
        self.controller.store.control.return_value = self.control
        async def process(_event, _observation, **kwargs):
            await kwargs['reasoner']()
            return SimpleNamespace(outcome=AutonomyCycleOutcome.COMPLETED, reason="queued")
        self.controller.process = AsyncMock(side_effect=process)
        self.events = SimpleNamespace(ingest=AsyncMock(return_value=SimpleNamespace(inserted=True, event=object())))
        self.telemetry = []
        self.service = ProjectDeliveryService(projects=self.projects, identity=self.identity, scope=self.scope,
            operator=self.operator, states=lambda: self.items, turns=self.turns, execution=self.execution,
            controller=self.controller, events=self.events, event_sink=self.telemetry.append, clock=lambda: self.now)

    def item(self, ref="owner/repo#2", **kwargs):
        return WorkItemState(ref=ref, project_id="project", created_at=100, updated_at=100,
                             last_meaningful_update_at=100, **kwargs)

    async def test_new_work_wakes_delivery_beside_external_release_blocker_for_both_providers(self):
        for provider in ("github", "gitlab"):
            with self.subTest(provider=provider):
                self.project.authoritative_task_source = self.project.authoritative_task_source.model_copy(update={'source_type': provider})
                self.items = {"release": self.item("owner/repo#1", current_stage="failed_with_action_owner", blocker="external production evidence"),
                              "new": self.item(),
                              "foreign": self.item("other/repo#3", organization_id="foreign")}
                await self.service.run_cycle()
                payload = self.turns.start.call_args.args[1]
                self.assertTrue(payload.defer_start)
                self.assertIn("owner/repo#2", payload.message)
                self.assertNotIn("owner/repo#1", payload.message)
                self.assertNotIn("other/repo#3", payload.message)
                self.assertEqual(self.turns.start.call_args.kwargs['actor'], self.actor)
                self.now += 120

    async def test_no_model_dispatch_for_idle_blocked_or_active_or_queued_work(self):
        cases = [{}, {'blocked': self.item(blocker="external")}, {'failed': self.item(current_stage="failed_with_action_owner")},
                 {'closed': self.item(current_stage="closed", closed_at=900)}]
        for items in cases:
            self.items = items
            await self.service.run_cycle()
            self.now += 120
        self.items = {'new': self.item()}
        self.execution.thread_is_active.return_value = True
        await self.service.run_cycle()
        self.now += 120
        self.execution.thread_is_active.return_value = False
        self.turns.queue.return_value = {'queueDepth': 1}
        await self.service.run_cycle()
        self.turns.start.assert_not_awaited()
        self.controller.process.assert_not_awaited()

    async def test_failure_retries_next_bounded_scan(self):
        self.items = {'new': self.item()}
        self.operator.sync_project.side_effect = [ConnectionError(), {"ok": True}]
        await self.service.run_cycle()
        self.assertEqual(self.telemetry[-1]['outcome'], 'failed')
        self.turns.start.assert_not_awaited()
        await self.service.run_cycle()
        self.assertEqual(self.operator.sync_project.await_count, 1)
        self.now += 120
        await self.service.run_cycle()
        self.turns.start.assert_awaited_once()

    async def test_failed_dispatch_does_not_leave_permanent_duplicate_tombstone(self):
        self.items = {'new': self.item()}
        self.turns.start.side_effect = [RuntimeError('preflight'), {'queued': True}]
        await self.service.run_cycle()
        first = self.events.ingest.call_args.kwargs['idempotency_key']
        self.now += 120
        await self.service.run_cycle()
        second = self.events.ingest.call_args.kwargs['idempotency_key']
        self.assertNotEqual(first, second)
        self.assertEqual(self.turns.start.await_count, 2)

    async def test_global_pause_and_identity_revocation_fail_closed(self):
        self.items = {'new': self.item()}
        self.control.mode = AutonomyMode.PAUSED
        await self.service.run_cycle()
        self.operator.sync_project.assert_not_awaited()
        self.control.mode = AutonomyMode.ACTIVE
        self.identity.actor_for_identity.side_effect = ValueError('revoked')
        await self.service.run_cycle()
        self.operator.sync_project.assert_not_awaited()
        self.turns.start.assert_not_awaited()

    def test_configured_delivery_uses_exact_project_and_enabling_identity(self):
        self.service.configure('project', ProjectDeliveryUpdate(thread_id='delivery-thread'), self.actor)
        self.scope.for_thread.assert_called_once_with('delivery-thread', self.actor, 'project')
        config = self.projects.set_delivery_supervision.call_args.args[1]
        self.assertEqual(config.actor_identity_id, 'operator')
        self.scope.for_thread.side_effect = HTTPException(status_code=404)
        with self.assertRaises(HTTPException):
            self.service.configure('project', ProjectDeliveryUpdate(thread_id='foreign-thread'), self.actor)

    async def test_absent_retained_open_work_is_reconciled_through_provider_read(self):
        from codex_web.models import TaskSourceIdentity
        self.items = {'old': self.item('owner/repo#963', source_identity=TaskSourceIdentity(source_type='github', source_instance='https://api.github.com', external_id='owner/repo#963'))}
        self.operator.sync_project.return_value = {'ok': True, 'work_item_refs': []}
        async def reconcile(*_args, **_kwargs):
            self.items['old'].current_stage = 'closed'
            self.items['old'].closed_at = self.now
        self.operator.reconcile.side_effect = reconcile
        await self.service.run_cycle()
        self.operator.reconcile.assert_awaited_once()
        self.turns.start.assert_not_awaited()

    async def test_one_failed_project_does_not_stop_other_projects(self):
        second = self.project.model_copy(update={"id": "second"})
        self.projects.list.return_value = [self.project, second]
        self.operator.sync_project.side_effect = [ConnectionError(), {"ok": True}]
        await self.service.run_cycle()
        self.assertEqual(self.operator.sync_project.await_count, 2)
        self.assertEqual([e['outcome'] for e in self.telemetry], ['failed', 'idle'])

    async def test_disabling_configuration_during_scan_prevents_dispatch(self):
        self.items = {'new': self.item()}
        self.projects.get.return_value = self.project.model_copy(update={'delivery_supervision': None})
        await self.service.run_cycle()
        self.turns.start.assert_not_awaited()

    async def test_canonical_replacement_keeps_delivery_supervision_attached(self):
        self.items = {'new': self.item()}
        self.turns.recovery = SimpleNamespace(replacement_thread_id=Mock(return_value='replacement'))
        def persist(_project_id, config, _scope):
            self.project = self.project.model_copy(update={'delivery_supervision': config})
            self.projects.get.return_value = self.project
            return self.project
        self.projects.set_delivery_supervision.side_effect = persist
        await self.service.run_cycle()
        self.scope.for_thread.assert_any_call('replacement', self.actor, 'project')
        self.assertEqual(self.project.delivery_supervision.thread_id, 'replacement')
        self.turns.start.assert_awaited_once()
        self.assertEqual(self.turns.start.call_args.args[0], 'replacement')

    async def test_inflight_queue_dispatch_does_not_duplicate_a_wakeup(self):
        self.items = {'new': self.item()}
        self.execution.queue_drain_tasks = {'delivery-thread': SimpleNamespace(done=lambda: False)}
        await self.service.run_cycle()
        self.turns.start.assert_not_awaited()
        self.assertEqual(self.telemetry[-1]['outcome'], 'dispatching')
