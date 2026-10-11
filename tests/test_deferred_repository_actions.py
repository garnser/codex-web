from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from codex_web.services.deferred_repository_actions import DeferredRepositoryActionRecovery
from codex_web.services.execution_workers import AssignmentNotFoundError


class DeferredRepositoryActionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.actor = NS(organization_id="local", workspace_id="default")
        self.assignment = NS(status="running", assigned_worker_id="worker", fence=3,
            lease=NS(worker_id="worker", fence=3, expires_at=time.time()+300),
            deadline_at=None, organization_id="local", workspace_id="default",
            project_id="project", created_by="requester", execution_workspace_id="workspace")
        self.intent = NS(id="intent", status="pending", attempt=0, lease=None,
            action_id="code-host.branch.publish", policy_decision=NS(source="policy:assignment-control-plane"),
            execution_id="origin", not_before=None, organization_id="local",
            workspace_id="default", project_id="project", requested_by="requester",
            resource_ids=("repo",), request=NS(parameters={"execution_workspace_id":"workspace"}))
        self.worker = NS(id="worker", lifecycle="active")
        self.workers = NS(store=NS(load=lambda:NS(workers=[self.worker])),
            assignment_for_execution=Mock(return_value=self.assignment))
        self.pending = [self.intent]
        self.admitted = False
        self.claimed = False
        def claim(payload, *, actor, intent_id):
            if not self.admitted or self.claimed:
                return None
            self.claimed = True
            return NS(id=intent_id)
        self.intents = NS(store=NS(indexed_intents=lambda *a, **k:iter(self.pending)),
            claim=Mock(side_effect=claim), execute_claimed=AsyncMock(),
            fail_if_unstarted=Mock())
        self.broker = NS(_writable_repository_id=Mock(return_value="repo"),
            _resolve_operation=Mock(return_value=NS()), _dispatch_scope=Mock(return_value=(None,self.actor,None,None)),
            execution_workspaces=NS(get=Mock(return_value=NS(status="active",execution_id="origin",
                owner_identity_id="requester", project_id="project",writable_repository_ids=("repo",)))))
        self.recovery = DeferredRepositoryActionRecovery(intents=self.intents,
            workers=self.workers, broker=self.broker, actor=self.actor, worker_id="worker")

    async def test_maintenance_deferral_resumes_once_after_admission_opens(self):
        await self.recovery.run_once()
        self.intents.execute_claimed.assert_not_awaited()
        self.admitted = True
        await asyncio.gather(self.recovery.run_once(), self.recovery.run_once())
        self.intents.execute_claimed.assert_awaited_once()
        self.broker._dispatch_scope.assert_called()
        self.assertEqual(self.intents.claim.call_args.kwargs["intent_id"], "intent")

    async def test_missing_released_expired_and_scope_changed_origins_do_not_publish(self):
        self.admitted = True
        for attribute, value in [("status", "cancelled"), ("fence", 4),
                                 ("project_id", "foreign"), ("created_by", "foreign")]:
            with self.subTest(attribute=attribute):
                old=getattr(self.assignment, attribute)
                setattr(self.assignment, attribute, value)
                await self.recovery.run_once()
                setattr(self.assignment, attribute, old)
        self.assignment.lease.expires_at = time.time()-1
        await self.recovery.run_once()
        self.workers.assignment_for_execution.side_effect = AssignmentNotFoundError("missing")
        await self.recovery.run_once()
        self.intents.execute_claimed.assert_not_awaited()
        self.intents.claim.assert_not_called()
        self.assertEqual(self.intents.fail_if_unstarted.call_count, 6)

    async def test_uncertain_retried_delayed_and_nonbroker_actions_are_not_replayed(self):
        self.admitted = True
        for attribute, value in [("status", "uncertain"), ("attempt", 1),
                                 ("not_before", time.time()+300), ("action_id", "other")]:
            old=getattr(self.intent,attribute);setattr(self.intent,attribute,value)
            await self.recovery.run_once();setattr(self.intent,attribute,old)
        self.intents.claim.assert_not_called()
        self.intents.fail_if_unstarted.assert_not_called()

    async def test_draining_worker_and_transient_storage_error_do_not_mutate_intents(self):
        self.worker.lifecycle="draining"
        await self.recovery.run_once()
        self.intents.claim.assert_not_called()
        self.worker.lifecycle="active"
        self.workers.assignment_for_execution.side_effect=RuntimeError("database temporarily unavailable")
        with self.assertRaises(RuntimeError):
            await self.recovery.run_once()
        self.intents.fail_if_unstarted.assert_not_called()

    async def test_released_workspace_requires_recovery_without_claim(self):
        self.broker.execution_workspaces.get.return_value.status="released"
        self.admitted=True
        await self.recovery.run_once()
        self.intents.claim.assert_not_called()
        self.intents.fail_if_unstarted.assert_called_once()

    async def test_cycle_limits_mutations_to_eight(self):
        self.pending=[NS(**{**vars(self.intent),"id":str(i)}) for i in range(20)]
        self.assignment.status="cancelled"
        await self.recovery.run_once()
        self.assertEqual(self.intents.fail_if_unstarted.call_count,8)
