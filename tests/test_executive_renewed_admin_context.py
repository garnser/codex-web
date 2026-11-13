"""Canonical admin lease renewal during real Executive context preparation.

Private SQLite/authority/Profile/worker/graph/Executive owners; only filesystem
provisioning and input Work metadata are controlled. No model or provider work.
"""
import asyncio
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from fastapi import HTTPException

from codex_web.agent_profiles import AgentProfileUpdate
from codex_web.execution_workers import AssignmentRenewRequest
from codex_web.execution_workspaces import ExecutionWorkspaceAcquire
from codex_web.models import Project, WorkItemState
from codex_web.executive_roles import ExecutiveActivationCreate
from codex_web.services.assignment_reasoning_broker import AssignmentReasoningBroker, OPERATIONS
from codex_web.services.control_plane_broker import ControlPlaneBrokerDeniedError
from codex_web.services.definitions import DefinitionNotFoundError
from codex_web.services.procedure_execution import ProcedureExecutionService, ProcedureExecutionError
from codex_web.services.work_graph import WorkGraphService
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from codex_web.storage.work_graph import WorkGraphStore
from tests import test_administrative_broker_runtime as admin_fixture
from tests import test_executive_management as executive_fixture
from tests import test_execution_workspaces as workspace_fixture


class RenewedAdminExecutiveContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin = admin_fixture.AdministrativeBrokerRuntimeTests()
        self.admin.addCleanup = self.addCleanup
        self.admin.addAsyncCleanup = self.addAsyncCleanup
        await self.admin.asyncSetUp(operations=tuple(op.id for op in OPERATIONS), prepare_only=True)
        # prepare_only starts no broker; its normal teardown expects a socket.
        self.addCleanup(self.admin.temp.cleanup)
        b = self.admin
        project = Project(id='project-a', name='Private admin context fixture',
            path=b.temp.name, organization_id=b.admin.organization_id,
            workspace_id=b.admin.workspace_id)
        b.workers.workspaces = ExecutionWorkspaceService(
            ExecutionWorkspaceStateStore(b.sqlite),
            workspace_fixture._FakeBackend(Path(b.temp.name) / 'scratch'),
            b.resources, lambda _: project)
        workspace = b.workers.workspaces.acquire(ExecutionWorkspaceAcquire(
            subject=b.assignment.subject, execution_id=b.assignment.execution_id,
            project_id=project.id, resource_ids=(), scratch=True, ttl_seconds=3600), actor=b.admin)
        def attach_workspace(state):
            next(a for a in state.assignments if a.id == b.assignment.id).execution_workspace_id = workspace.id
            return state
        b.workers.store.update_assignment(b.worker.id, b.assignment.id, attach_workspace)
        b.assignment = b.current_assignment = b.workers.store.assignment(b.assignment.id)
        self.executive = executive_fixture.ExecutiveManagementTests()
        await self.executive.asyncSetUp()
        self.addAsyncCleanup(self.executive.asyncTearDown)
        observed = time.time()
        state = WorkItemState(ref='fixture/work!1', organization_id='local',
            workspace_id='default', project_id='project-a',
            resource_ids=(b.target.id,), title='Private canonical Work fixture',
            last_meaningful_update_at=observed, updated_at=observed, created_at=observed)
        self.executive.service.work_graph = WorkGraphService(
            WorkGraphStore(self.executive.store.store), lambda: {state.ref: state})
        machine = NS(_work_item_state=lambda ref: state,
                     _work_item_state_public=lambda row: row.model_dump(mode='json'))
        self.executive.service.work_items = NS(state_machine=machine)
        b.service.work_items = NS(state_machine=machine)
        b.service.execution_workers = b.workers
        owner = NS(workers=b.workers, identity=b.identity, control_actor=b.admin,
                   clock=lambda: time.time())
        procedures = NS(_live=lambda assignment: ProcedureExecutionService._live(owner, assignment))
        self.adapter = AssignmentReasoningBroker(b.service, procedures=procedures,
            executives=self.executive.service)

    def renew(self, initial):
        b = self.admin
        return b.workers.renew(b.worker.id, initial.id, AssignmentRenewRequest(
            lease_token=initial.lease.lease_token, fence=initial.fence,
            lease_seconds=600), actor=b.worker_actor)

    def change_assignment(self, initial, drift):
        def change(state):
            row = next(a for a in state.assignments if a.id == initial.id)
            if drift == 'fence':
                row.fence += 1
            elif drift == 'resources':
                row.resource_ids = (self.admin.target.id,)
            elif drift == 'lease_generation':
                row.lease = row.lease.model_copy(update={'lease_token': 'changed-generation-token-' * 2})
            else:
                row.status = 'cancelled'
                row.lease = None
            return state
        self.admin.workers.store.update_assignment(self.admin.worker.id, initial.id, change)

    async def exercise(self, drift=None, *, cancel=False):
        b = self.admin
        initial = b.workers.store.assignment(b.assignment.id)
        now = [time.time()]
        operation = next(op for op in OPERATIONS if op.id == 'executive.activation.create')
        request = ExecutiveActivationCreate(subject='Lease renewal during context preparation',
            request='One bounded technical judgment.', project_id='project-a',
            trigger_ref=self.adapter.origin(initial), requested_role_ids=('cto',), max_roles=1,
            work_item_refs=('fixture/work!1',)).model_dump(mode='json')
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        prepare = self.executive.service.prepare
        prepared = []
        def gated_prepare(*args, **kwargs):
            result = prepare(*args, **kwargs)
            prepared.append(result)
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return result
        with patch('codex_web.services.execution_profile_definitions.time.time', side_effect=lambda: now[0]), \
                patch.object(self.executive.service, 'prepare', side_effect=gated_prepare):
            task = asyncio.create_task(self.adapter.dispatch(initial, b.worker_actor,
                operation, None, request, {}))
            entered_task = asyncio.create_task(entered.wait())
            try:
                done, _ = await asyncio.wait((task, entered_task), return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    await task
                    self.fail('CREATE completed without reaching real preparation gate')
                self.assertEqual(len(prepared), 1)
                renewed = self.renew(initial)
                now[0] = initial.lease.expires_at + 1
                self.assertGreater(renewed.lease.expires_at, now[0])
                self.assertEqual((renewed.id, renewed.fence, renewed.assigned_worker_id),
                    (initial.id, initial.fence, initial.assigned_worker_id))
                b.profiles.authorize_administrative_target(renewed,
                    operation_id=operation.id, repository_id=b.target.id,
                    actor=b.admin, authority=b.authority, now=now[0])
                if drift == 'authority':
                    record = b.authority.catalog_record(actor=b.admin, project_id='project-a')
                    b.registry.quarantine(record.record_id, actor=b.admin.identity_id, reason='Current authority revoked')
                elif drift == 'profile':
                    b.agent_profiles.update('orchestrator', AgentProfileUpdate(
                        description='Changed during preparation', reason='Current Profile changed'), actor=b.admin)
                elif drift is not None:
                    self.change_assignment(initial, drift)
                if cancel:
                    task.cancel()
                    # Cancellation remains owned; inspect tasks through causal
                    # loop callbacks while the actual producer is still gated.
                    for _ in range(4):
                        await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertEqual(self.executive.store.load().activations, [])
                release.set()
                if cancel:
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                elif drift is None:
                    try:
                        result = await task
                    except ValueError as error:
                        if str(error) != 'administrative operation requires a matching live fenced assignment':
                            raise
                        self.fail('Post-prepare validation reused expired admin snapshot despite canonical same-fence renewal: ' + str(error))
                    self.assertEqual(result['item']['trigger_ref'], request['trigger_ref'])
                    self.assertEqual(len(self.executive.store.load().activations), 1)
                else:
                    expected = {
                        'authority': DefinitionNotFoundError,
                        'profile': ControlPlaneBrokerDeniedError,
                    }.get(drift, (HTTPException, ProcedureExecutionError, ValueError))
                    with self.assertRaises(expected) as refusal:
                        await task
                    self.assertNotEqual(str(refusal.exception),
                        'administrative operation requires a matching live fenced assignment',
                        'Negative must not pass merely because the old snapshot expired')
                if drift is not None or cancel:
                    self.assertEqual(self.executive.store.load().activations, [])
                self.assertEqual(self.executive.gateway.requests, [])
            finally:
                release.set()
                entered_task.cancel()
                await asyncio.gather(task, entered_task, return_exceptions=True)

    async def test_same_fence_renewal_during_preparation_does_not_use_expired_admin_snapshot(self):
        await self.exercise()

    async def test_changed_fence_after_preparation_still_refuses_without_persistence(self):
        await self.exercise('fence')

    async def test_cancelled_assignment_after_preparation_still_refuses_without_persistence(self):
        await self.exercise('cancelled')

    async def test_changed_authority_after_preparation_still_refuses_without_persistence(self):
        await self.exercise('authority')

    async def test_changed_profile_after_preparation_still_refuses_without_persistence(self):
        await self.exercise('profile')

    async def test_changed_resource_scope_after_preparation_still_refuses_without_persistence(self):
        await self.exercise('resources')

    async def test_changed_lease_generation_after_preparation_still_refuses_without_persistence(self):
        await self.exercise('lease_generation')

    async def test_cancelled_observer_joins_renewed_preparation_without_late_persistence(self):
        await self.exercise(cancel=True)

    def test_empty_scratch_scope_remains_empty_at_real_authority_evaluation(self):
        b = self.admin
        initial = b.workers.store.assignment(b.assignment.id)
        operation = next(op for op in OPERATIONS if op.id == 'executive.activation.create')
        profile, _ = b.profiles.resolve('orchestration-admin', project_id=initial.project_id)
        self.assertEqual(initial.resource_ids, ())
        self.assertEqual(profile.administrative_repository_ids, (b.target.id,))
        # The nonempty administrative envelope supplies a declaration check,
        # not additional Resource requirements for an empty scratch operation.
        authorize = b.service._authorize
        evaluate = b.authority.evaluate
        target_guard = b.profiles.authorize_administrative_target
        with patch.object(b.service, '_authorize', wraps=authorize) as broker_guard, \
                patch.object(b.authority, 'evaluate', wraps=evaluate) as authority_guard, \
                patch.object(b.profiles, 'authorize_administrative_target', wraps=target_guard) as declaration_guard:
            current, actor = self.adapter.current(initial, b.worker_actor, operation, None)
        self.assertEqual(current.id, initial.id)
        self.assertEqual(actor.identity_id, b.admin.identity_id)
        self.assertEqual(broker_guard.call_args.kwargs['resource_ids'], ())
        # Inspect the request actually accepted by the canonical owner, not
        # just a call count or an allow-all replacement for authority.
        requests = [call.args[0] for call in authority_guard.call_args_list]
        self.assertTrue(requests)
        self.assertEqual([(request.capability, request.level, request.project_id,
                           request.resource_ids) for request in requests],
                         [(operation.capability, operation.authority_level,
                           initial.project_id, ())])
        self.assertEqual([call.kwargs['repository_id']
                          for call in declaration_guard.call_args_list], [b.target.id])
        self.assertEqual(self.executive.store.load().activations, [])
        self.assertEqual(self.executive.gateway.requests, [])

    def test_serial_target_checks_refresh_after_same_owner_renewal(self):
        b = self.admin
        initial = b.workers.store.assignment(b.assignment.id)
        now = [time.time()]
        operation = next(op for op in OPERATIONS if op.id == 'executive.activation.create')
        # scoped() checks the exact target, then the real broker authorizes it
        # again. Renewal between those canonical guards must not reuse a lease.
        item = NS(organization_id=initial.organization_id, workspace_id=initial.workspace_id,
                  project_id=initial.project_id, resource_ids=(b.target.id,))
        original = b.profiles.authorize_administrative_target
        seen = []
        def advance_after_first(*args, **kwargs):
            result = original(*args, **kwargs)
            seen.append(args[0].lease.expires_at)
            if len(seen) == 1:
                self.renew(initial)
                now[0] = initial.lease.expires_at + 1
            return result
        with patch('codex_web.services.execution_profile_definitions.time.time', side_effect=lambda: now[0]), \
                patch.object(b.profiles, 'authorize_administrative_target', side_effect=advance_after_first):
            self.adapter.scoped(item, initial, b.admin, operation)
        self.assertGreaterEqual(len(seen), 2)
        self.assertGreater(seen[-1], now[0])
        self.assertEqual(self.executive.store.load().activations, [])
        self.assertEqual(self.executive.gateway.requests, [])
