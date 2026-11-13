"""No-handoff recipient history uses canonical preparation and native START."""
import asyncio
import io
import json
import time
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from codex_web.execution_workers import AssignmentStatus
from codex_web.agent_runtime import AgentSessionStatus
from codex_web.models import QueuedTurn
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.queued_native_start import QueuedNativeStartCapture
from codex_web.services.turns import TurnService
from codex_web.storage.thread_history import ThreadHistoryRepository
from tests import test_current_owner_recipient_scope as current_fixture


class CurrentWorkRecipientRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.current = current_fixture.CurrentOwnerRecipientScopeTests()
        self.current.setUp()
        self.addCleanup(self.current.doCleanups)
        self.n = self.current.n
        self.f = self.current.f
        self.state = self.current.state
        self.n.template.agent_profile = self.n.template.agent_profile.model_copy(update={
            'selected_worker_id': self.n.template.assigned_worker_id})
        self.history = ThreadHistoryRepository(self.f.f.env.env.state)
        # Composition supplies the existing canonical transcript owner, not
        # caller-authored completion/cleanup booleans or a native reply override.
        self.n.routing.thread_history = self.history
        self.n.routing.authenticated_origin_revalidator = lambda actor: actor
        self.project = self.f.f.env.env.projects.get('project-a', scope=self.f.f.env.env.scope)
        self.service = TurnService(projects=NS(), settings=NS(), recovery=NS(),
            resume_runtime=NS(), bindings=NS(for_thread=lambda _: []),
            queue_policy=NS(), execution=NS(), event_sink=lambda _: None,
            truncate_text=lambda text, limit: text[:limit], binding_public=lambda item: item)
        self.service.owner_routing = self.n.routing

    async def started(self, *, work_ref=None, native_turn='native-current-work', complete=True):
        if not hasattr(self, 'pin'):
            self.pin = await self.n.prepare()
        assignment = self.n.reserved
        self.n.store.upsert(self.n.session.model_copy(deep=True, update={
            'id': 'native-' + self.pin.recipient.thread_id, 'provider_native_session_id': self.pin.recipient.thread_id,
            'assignment_id': assignment.id, 'execution_id': assignment.execution_id,
            'execution_workspace_id': assignment.execution_workspace_id,
            'worker_id': assignment.assigned_worker_id, 'resource_ids': assignment.resource_ids,
            'status': AgentSessionStatus.READY, 'failure': None}))
        queued = QueuedTurn(id='original-' + native_turn, execution_id='execution-' + native_turn,
            thread_id=self.pin.recipient.thread_id, project_id=self.project.id,
            message='Read the existing governed input', work_item_ref=work_ref or self.state.ref,
            handoff_requested_at=None, created_at=time.time(), owner_routing_pin=self.pin,
            agent_profile_id=self.f.f.profile.profile_id,
            agent_profile_revision=self.f.f.profile.revision,
            agent_profile_actor_id=self.f.f.actor.identity_id,
            execution_profile_id='repository-write', repository_resource_id='platform',
            writable_repository_resource_ids=('platform',), read_only_repository_resource_ids=())
        preflight = ExecutionPreflightService(self.f.f.preflight, clock=time.time)
        attempt = preflight.retain_queued(queued, organization_id='org-a', workspace_id='workspace-a')
        _, claim = preflight.claim_queued_admission(attempt.id)
        preflight.begin_queued_start(attempt.id, claim_id=claim)
        def validate():
            self.n.routing.validate_native(self.pin, thread_id=queued.thread_id,
                project=self.project, work_item_ref=self.state.ref, handoff_requested_at=None,
                writable_repository_resource_ids=('platform',), read_only_repository_resource_ids=(),
                agent_profile_id=self.f.f.profile.profile_id,
                agent_profile_revision=self.f.f.profile.revision,
                agent_profile_actor_id=self.f.f.actor.identity_id,
                queued_admission_attempt_id=attempt.id)
        capture = QueuedNativeStartCapture(preflight, attempt.id, claim, queued.execution_id,
            queued.thread_id, self.f.f.actor.identity_id,
            admission_validator=validate, require_send_guard=True).bind_assignment(
                assignment, worker_id=assignment.assigned_worker_id, fence=assignment.fence,
                validator=lambda: assignment)
        generation = getattr(self, 'active_native', NS(generation='current-work-generation')).generation
        correlation = capture.prepare(generation=generation, rpc_request_id=native_turn,
            native_session_id=queued.thread_id)
        capture.validate_before_send(correlation)
        self.assertTrue(capture.observe(correlation, generation=generation, message={
            'id': native_turn, 'result': {'thread': {'id': queued.thread_id},
                'turn': {'id': native_turn, 'threadId': queued.thread_id, 'status': 'inProgress'}}}))
        self.history.start_turn(queued.thread_id, turn_id=queued.execution_id,
            message=queued.message, provider_id=correlation.provider_id, runtime_id=correlation.runtime_id)
        self.history.project_message(queued.thread_id,
            {'method': 'turn/started', 'params': {'turn': {'id': native_turn}}})
        if complete:
            self.history.project_message(queued.thread_id,
                {'method': 'turn/completed', 'params': {'turn': {'id': native_turn, 'status': 'completed'}}})
        saved = self.f.f.preflight.get(attempt.id)
        self.assertEqual(saved.status, 'started')
        self.assertEqual(saved.queued_start_receipt.provider_native_turn_id, native_turn)
        self.assertEqual(self.pin.schema_version, '1.2')
        self.assertIsNone(self.state.handoff)
        return saved

    async def owned_replacement_service(self):
        """Actual storage/capture/manager/recovery; only native I/O is external."""
        from codex_web.execution_subjects import ExecutionSubject
        from codex_web.models import ThreadRunSettings
        from codex_web.runtime.execution import TurnExecutionService
        from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager
        from codex_web.services.execution_workers import ExecutionWorkerService
        from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
        from codex_web.services.thread_execution_settings import ThreadExecutionSettingsService
        from codex_web.services.thread_recovery import ThreadRecoveryService
        from codex_web.storage.execution_workers import ExecutionWorkerStore
        from codex_web.storage.thread_index import ThreadIndexRepository
        self.pin = await self.n.prepare()
        # Older routing fixtures construct source rows in memory. Complete
        # their real thread subjects before passing through canonical decoding;
        # the prepared current-work row already has its actual bootstrap subject.
        for assignment in self.f.assignments.values():
            if assignment.subject is None:
                sources = [b.thread_id for b in self.f.bootstraps.values()
                    if b.assignment_id == assignment.id]
                if assignment.id == self.n.session.assignment_id:
                    sources.append(self.n.session.provider_native_session_id)
                self.assertEqual(len(set(sources)), 1)
                assignment.subject = ExecutionSubject(kind='thread', ref=sources[0])
        store = ExecutionWorkerStore(self.f.f.env.env.state)
        store.update(lambda state: state.model_copy(update={'assignments': list(self.f.assignments.values())}))
        workers = ExecutionWorkerService(store)
        self.n.routing.assignments = store.assignment
        self.n.routing.recipient_preparer.existing_assignment = lambda **kw: next(
            (a for a in store.list_assignments() if a.execution_id == kw['execution_id']), None)
        original = store.assignment(self.n.reserved.id)
        physical = await self.adopt_native(store, workers, original, self.pin.recipient.thread_id)
        native, process, session, local, pipe = (physical.runtime, physical.process,
            physical.session, physical.local, physical.pipe)
        self.n.reserved = store.assignment(original.id)
        manager = AssignmentBoundAgentProcessSessionManager.__new__(AssignmentBoundAgentProcessSessionManager)
        manager._assignment_locks = weakref.WeakValueDictionary()
        manager.sessions = {original.id: session}
        manager.local_worker = local
        host = NS(THREAD_REPLACEMENTS={}, _load_active_turns=lambda: {},
            _save_active_turns=lambda _: None, _load_turn_queues=lambda: {},
            _save_turn_queues=lambda _: None, _load_slack_thread_icons=lambda: {},
            _save_slack_thread_icons=lambda _: None, hub=NS(publish=AsyncMock()))
        runtime = TurnExecutionService(host, session_manager=manager,
            bootstrap_bindings=self.n.routing.bootstraps, control_actor=self.f.f.actor,
            thread_history=self.history)
        runtime._bootstrap_binding_for_thread = lambda thread: self.f.bootstraps.get(thread)
        runtime.queued_preflight = ExecutionPreflightService(self.f.f.preflight, clock=time.time)
        self.n.routing.current_work_cleanup_status = runtime.current_work_replacement_status
        settings_rows = {self.pin.recipient.thread_id: ThreadRunSettings(
            sandbox=self.n.binding.sandbox, approval_policy=self.n.binding.approval_policy,
            execution_profile_id='repository-write', repository_resource_id='platform',
            writable_repository_resource_ids=('platform',))}
        settings = ThreadExecutionSettingsService(load_settings=lambda: settings_rows,
            save_settings=lambda rows: settings_rows.update(rows), bindings=NS(for_thread=lambda _: []),
            load_bindings=lambda: [self.n.binding], save_bindings=lambda _: self.fail('binding rewritten'),
            gitlab_routing_enabled_for_project=lambda _: False, binding_report_name=lambda _: None,
            binding_prefix=lambda _: None)
        index = ThreadIndexRepository(self.f.f.env.env.state,
            Path(self.f.f.env.env.temp.name) / 'current-work-replacement-index.json')
        self.n.routing.replacement_resolver = index.replacement_thread_id
        self.n.routing.replacement_lineage = index.replacement_lineage
        def exact_bootstrap(key, actor):
            rows = [b for b in self.f.bootstraps.values() if getattr(b, 'bootstrap_id', None) == key]
            if len(rows) != 1:
                raise ThreadBootstrapBindingNotFoundError('Exact bootstrap unavailable')
            return rows[0]
        self.n.routing.bootstraps.get_by_bootstrap = exact_bootstrap
        self.new_calls, self.successors = [], {}
        async def create(**kwargs):
            predecessor = self.pin.recipient.assignment_id
            self.assertNotIn(predecessor, manager.sessions)
            self.assertIsNotNone(store.assignment(predecessor).bootstrap_create_observation.cleanup_joined_at)
            kwargs['admission_guard']()
            self.new_calls.append(kwargs)
            number, token = len(self.new_calls), kwargs['bootstrap_token']
            thread_id = 'successor-thread-' + str(number)
            new = original.model_copy(deep=True, update={
                'id': 'successor-assignment-' + str(number), 'status': AssignmentStatus.RUNNING,
                'subject': ExecutionSubject(kind='thread_bootstrap', ref='bootstrap-' + token),
                'execution_id': 'thread-bootstrap-' + token,
                'execution_workspace_id': 'successor-workspace-' + str(number),
                'bootstrap_create_observation': None})
            new.lease = new.lease.model_copy(update={'lease_token': 'private-successor-lease-token-' + str(number),
                'acquired_at': time.time(), 'renewed_at': None})
            store.update(lambda state: state.model_copy(update={'assignments': state.assignments + [new]}))
            self.f.bootstraps[thread_id] = NS(id='successor-bootstrap-' + str(number),
                bootstrap_id='bootstrap-' + token, thread_id=thread_id, assignment_id=new.id,
                execution_id=new.execution_id, execution_workspace_id=new.execution_workspace_id,
                created_by=new.created_by, organization_id=new.organization_id, workspace_id=new.workspace_id)
            owned = await self.adopt_native(store, workers, new, thread_id)
            manager.sessions[new.id] = owned.session
            self.successors[thread_id] = owned
            self.n.store.upsert(self.n.session.model_copy(deep=True, update={
                'id': 'native-' + thread_id, 'provider_native_session_id': thread_id,
                'assignment_id': new.id, 'execution_id': new.execution_id,
                'execution_workspace_id': new.execution_workspace_id, 'worker_id': new.assigned_worker_id,
                'resource_ids': new.resource_ids, 'status': AgentSessionStatus.READY, 'failure': None}))
            return {'thread': {'id': thread_id}}
        recovery = ThreadRecoveryService(host, projects=NS(get=lambda _: self.project),
            settings=settings, naming=NS(set_name=AsyncMock()), thread_index=index,
            runtime_request=AsyncMock(), event_sink=lambda _: None, truncate_text=lambda x, n: x[:n],
            thread_creator=create)
        recovery.predecessor_retirement = runtime.retire_replacement_predecessor
        host._archive_replaced_bot_thread = recovery.archive_replaced_bot_thread
        self.service.recovery, self.service.settings = recovery, settings
        self.service.execution = NS(schedule_queue_drain=Mock())
        self.service.queue_policy = NS(depth=lambda _: 0)
        self.physical = NS(runtime=native, process=process, session=session,
            manager=manager, store=store, recovery=recovery, pipe=pipe)
        self.active_native = native
        return self.physical

    async def adopt_native(self, store, workers, original, thread_id, *, actor=None, workspace_path=None):
        from codex_web.runtime.codex import CodexRuntime
        from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSession
        from codex_web.services.bootstrap_native_create import BootstrapNativeCreateCapture
        from tests.test_codex_runtime import _Host
        actor = actor or self.f.f.actor
        workspace_path = workspace_path or Path(self.f.f.env.env.temp.name)
        native = CodexRuntime(_Host(), restart_on_timeout=False)
        native.ensure_started = AsyncMock()
        pipe, sent = io.StringIO(), asyncio.Event()
        process = NS(pid=12345, stdin=pipe, poll=lambda: None, wait=lambda: 0)
        def terminate(_process=process):
            _process.poll = lambda: 0
        process.terminate = process.kill = terminate
        backend = NS(discover_git_metadata=lambda _: None,
            discover_git_worktree_metadata=lambda _: None, terminate_process=terminate)
        local = NS(worker_service=workers, control_actor=actor,
            _pending_assignment=store.assignment, worker=NS(id=original.assigned_worker_id),
            artifact_evidence=None, backend=backend)
        watchdog_hold = asyncio.Event()
        session = AssignmentBoundAgentProcessSession(local, _Host(), original.id,
            runtime_factory=lambda *_args, **_kwargs: native, credential_provider=NS(),
            sleep=lambda _: watchdog_hold.wait())
        self.addCleanup(session._supervision_executor.shutdown, wait=True, cancel_futures=True)
        self.addCleanup(session._diagnostic_executor.shutdown, wait=True, cancel_futures=True)
        # Physical workspace/delegation/provider initialization are controlled
        # boundaries here. The real Session.start adopts their actual process;
        # no test writes its process-generation ownership tuple or cleanup proof.
        def prepare_physical():
            current = store.assignment(original.id)
            self.assertEqual(current, original)
            self.assertEqual(current.status, AssignmentStatus.RUNNING)
            self.assertIsNotNone(current.lease)
            self.assertGreater(current.lease.expires_at, time.time())
            return current, workspace_path
        session._prepare_assignment = prepare_physical
        session._start_dependency_egress_broker = AsyncMock(return_value=None)
        session._start_egress_broker = AsyncMock(return_value=None)
        session._start_control_plane_broker = AsyncMock(return_value=None)
        session._spawn_delegated_process = lambda *_args: (process, None, ())
        async def initialize_provider():
            native.proc = process
            native._pipe_reader_executor()
            native.ready.set()
        native.start = initialize_provider
        await session.start()
        self.addCleanup(native._pipe_executor.shutdown, wait=True, cancel_futures=True)
        self.addAsyncCleanup(session.stop)
        original_send = native._send
        async def send(*args, **kwargs):
            await original_send(*args, **kwargs)
            sent.set()
        native._send = send
        capture = BootstrapNativeCreateCapture(workers, actor).bind_assignment(
            original, worker_id=original.assigned_worker_id, fence=original.fence,
            validator=lambda: store.assignment(original.id))
        request = asyncio.create_task(native.request('thread/start', {'cwd': '/private/workspace'}, start_capture=capture))
        try:
            await sent.wait()
            await native._dispatch_message({'id': json.loads(pipe.getvalue())['id'],
                'result': {'thread': {'id': thread_id}}})
            await request
        finally:
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
        return NS(runtime=native, process=process, session=session, local=local, pipe=pipe)

    async def test_unowned_terminal_current_work_exposes_cleanup_prerequisite_without_replacement(self):
        original = await self.started()
        rows = self.history.get(self.pin.recipient.thread_id)['turns']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['providerTurnId'], original.queued_start_receipt.provider_native_turn_id)
        self.assertEqual(rows[0]['status'], 'completed')
        self.assertIsNotNone(rows[0]['completedAt'])
        assignment = self.n.reserved.model_copy(deep=True,
            update={'status': AssignmentStatus.LOST, 'lease': None})
        self.f.assignments[assignment.id] = assignment
        self.n.reserved = assignment
        before = assignment.model_dump(mode='json')
        with self.assertRaises(HTTPException) as refused:
            self.service.replacement_eligibility(self.pin.recipient.thread_id,
                work_item_ref=self.state.ref, actor=self.f.f.actor, project=self.project)
        self.assertEqual(refused.exception.status_code, 409)
        self.assertEqual(refused.exception.detail['code'], 'owner_current_work_cleanup_unavailable')
        self.assertEqual(self.f.assignments[assignment.id].model_dump(mode='json'), before)
        self.n.threads.create.assert_awaited_once()
        self.assertIsNone(self.f.f.env.states.get(self.state.ref).handoff)
        inspected = self.f.continuity.owner_dispatch_projection(self.state)
        self.assertFalse(inspected['eligible'])
        self.assertEqual(inspected['blocked_reason'], 'owner_current_work_recipient_custody_unavailable')
        self.assertEqual(inspected['recipient_replacement']['thread_id'], self.pin.recipient.thread_id)
        self.assertEqual(inspected['recipient_replacement']['assignment_id'], assignment.id)
        self.assertTrue(inspected['recipient_replacement']['eligibility_requires_authenticated_inspection'])

    def lose_recipient(self):
        assignment = self.n.reserved.model_copy(deep=True,
            update={'status': AssignmentStatus.LOST, 'lease': None})
        self.f.assignments[assignment.id] = assignment
        self.n.reserved = assignment
        return assignment

    def eligibility(self):
        return self.service.replacement_eligibility(self.pin.recipient.thread_id,
            work_item_ref=self.state.ref, actor=self.f.f.actor, project=self.project)

    async def test_unfinished_other_work_on_same_recipient_is_not_a_cleanup_witness(self):
        await self.started()
        # A real second canonical transcript entry represents another input on
        # the same process. Its unresolved outcome survives any target-Work
        # success; it cannot be hidden by a Work-filtered START lookup.
        self.history.start_turn(self.pin.recipient.thread_id,
            turn_id='execution-other-work', message='Existing other Work input',
            provider_id='openai', runtime_id='codex')
        self.history.project_message(self.pin.recipient.thread_id,
            {'method': 'turn/started', 'params': {'turn': {'id': 'native-other-work'}}})
        before = self.history.get(self.pin.recipient.thread_id)
        assignment = self.lose_recipient()
        with self.assertRaises(HTTPException) as refused:
            self.eligibility()
        self.assertEqual(refused.exception.detail['code'], 'owner_current_work_outcome_unresolved')
        self.assertEqual(self.history.get(self.pin.recipient.thread_id), before)
        self.assertEqual(self.f.assignments[assignment.id], assignment)
        self.n.threads.create.assert_awaited_once()

    async def test_started_original_without_exact_completed_native_turn_refuses(self):
        await self.started(complete=False)
        self.lose_recipient()
        before = self.history.get(self.pin.recipient.thread_id)
        with self.assertRaises(HTTPException) as refused:
            self.eligibility()
        self.assertEqual(refused.exception.detail['code'], 'owner_current_work_completion_unverified')
        self.assertEqual(self.history.get(self.pin.recipient.thread_id), before)
        self.n.threads.create.assert_awaited_once()

    async def terminal_owned(self, physical):
        assignment_id = self.pin.recipient.assignment_id
        def lost(state):
            state.assignments = [state.assignments[0].model_copy(update={
                'status': AssignmentStatus.LOST, 'lease': None})]
            return state
        self.physical.store.transition_assignment(self.pin.recipient.worker_id, assignment_id, lost)
        self.n.reserved = self.physical.store.assignment(assignment_id)
        original_generation = physical.runtime.generation
        await physical.session._stop_runtime_from_watchdog()
        self.assertNotEqual(physical.runtime.generation, original_generation)
        self.assertIsNone(physical.runtime.proc)
        self.assertEqual(physical.process.poll(), 0)
        return self.n.reserved

    async def test_owned_completed_current_work_joins_then_reuses_one_successor_and_admits_continuation(self):
        physical = await self.owned_replacement_service()
        original_start = await self.started()
        old = self.pin.recipient.thread_id
        original = await self.terminal_owned(physical)
        before = original.model_dump(mode='json', exclude={'bootstrap_create_observation'})
        results = await asyncio.gather(*(self.service.replace(old, work_item_ref=self.state.ref,
            actor=self.f.f.actor, project=self.project) for _ in range(2)))
        result = results[0]
        self.assertEqual(results[1]['newThreadId'], result['newThreadId'])
        self.assertEqual(result['newThreadId'], 'successor-thread-1')
        self.assertEqual(self.physical.store.assignment(original.id).model_dump(
            mode='json', exclude={'bootstrap_create_observation'}), before)
        self.assertEqual(self.f.f.preflight.get(original_start.id), original_start)
        self.assertEqual(len(self.new_calls), 1)
        again = await self.service.replace(old, work_item_ref=self.state.ref,
            actor=self.f.f.actor, project=self.project)
        self.assertEqual(again['newThreadId'], result['newThreadId'])
        self.assertEqual(len(self.new_calls), 1)
        self.pin = self.n.routing.compatible_pin(self.state, self.n.binding,
            'orchestrator', writable_repository_ids=('platform',))
        self.n.reserved = self.physical.store.assignment(self.pin.recipient.assignment_id)
        self.active_native = self.successors[self.pin.recipient.thread_id].runtime
        continuation = await self.started(native_turn='native-successor-continuation')
        self.assertNotEqual(continuation.queued_start_receipt.thread_id,
            original_start.queued_start_receipt.thread_id)
        self.assertEqual(continuation.queued_start_receipt.thread_id, result['newThreadId'])
        selected = self.n.routing.compatible_pin(self.state, self.n.binding,
            'orchestrator', writable_repository_ids=('platform',))
        self.assertEqual(selected.recipient.thread_id, result['newThreadId'])
        self.current.validate(selected)
        self.assertIsNone(self.f.f.env.states.get(self.state.ref).handoff)

    async def test_next_terminal_generation_uses_existing_scoped_chain_and_its_own_start(self):
        await self.test_owned_completed_current_work_joins_then_reuses_one_successor_and_admits_continuation()
        successor = self.pin.recipient.thread_id
        await self.terminal_owned(self.successors[successor])
        result = await self.service.replace(successor, work_item_ref=self.state.ref,
            actor=self.f.f.actor, project=self.project)
        self.assertEqual(result['newThreadId'], 'successor-thread-2')
        self.assertEqual(len(self.new_calls), 2)
        self.assertEqual(self.physical.recovery.replacement_thread_id('platform-thread'),
            result['newThreadId'])
        self.pin = self.n.routing.compatible_pin(self.state, self.n.binding,
            'orchestrator', writable_repository_ids=('platform',))
        self.n.reserved = self.physical.store.assignment(self.pin.recipient.assignment_id)
        self.active_native = self.successors[self.pin.recipient.thread_id].runtime
        final = await self.started(native_turn='native-second-generation')
        self.assertEqual(final.queued_start_receipt.thread_id, result['newThreadId'])
        self.assertEqual(len(self.new_calls), 2)

    async def test_unrelated_uncertain_input_without_transcript_stays_held(self):
        physical = await self.owned_replacement_service()
        await self.started()
        queued = QueuedTurn(id='other-work-original', execution_id='other-work-execution',
            thread_id=self.pin.recipient.thread_id, project_id=self.project.id,
            work_item_ref='team/other!389', message='Retained other Work input', created_at=time.time())
        preflight = ExecutionPreflightService(self.f.f.preflight, clock=time.time)
        original = preflight.retain_queued(queued, organization_id='org-a', workspace_id='workspace-a')
        _, claim = preflight.claim_queued_admission(original.id)
        preflight.begin_queued_start(original.id, claim_id=claim)
        before = self.f.f.preflight.get(original.id).model_dump(mode='json')
        await self.terminal_owned(physical)
        with self.assertRaises(HTTPException) as refused:
            self.eligibility()
        self.assertEqual(refused.exception.detail['code'], 'owner_current_work_outcome_unresolved')
        self.assertEqual(self.f.f.preflight.get(original.id).model_dump(mode='json'), before)
        self.assertEqual(self.new_calls, [])
        self.assertIn(self.pin.recipient.assignment_id, physical.manager.sessions)

    async def test_saturated_transcript_does_not_claim_all_prior_outcomes_are_known(self):
        physical = await self.owned_replacement_service()
        await self.started()
        # Exercise the actual projection retention boundary in one private
        # storage fixture write; no 1000-turn workflow is constructed.
        history = self.history.get(self.pin.recipient.thread_id)
        completed = history['turns'][0]
        history['turns'] = [dict(completed, id='prior-' + str(number),
            providerTurnId='native-prior-' + str(number))
            for number in range(self.history.MAX_TURNS - 1)] + [completed]
        self.history.store.record_apply(self.history.NAMESPACE,
            upserts={self.pin.recipient.thread_id: history})
        await self.terminal_owned(physical)
        with self.assertRaises(HTTPException) as refused:
            self.eligibility()
        self.assertEqual(refused.exception.detail['code'], 'owner_current_work_outcome_unresolved')
        self.assertEqual(self.history.get(self.pin.recipient.thread_id), history)
        self.assertEqual(self.new_calls, [])

    async def test_current_work_authority_profile_definition_and_scope_drift_refuse(self):
        from codex_web.definitions import DefinitionLifecycle
        from codex_web.identity import MembershipRole
        from codex_web.services.identity import AuthorizationError
        started = await self.started()
        original = self.lose_recipient()
        retained = started.model_dump(mode='json')
        with self.assertRaises(AuthorizationError):
            self.service.replacement_eligibility(self.pin.recipient.thread_id,
                work_item_ref=self.state.ref, project=self.project,
                actor=self.f.f.actor.model_copy(update={'roles': (MembershipRole.MEMBER,)}))
        changes = (
            {'agent_profile': original.agent_profile.model_copy(update={'profile_revision': 999})},
            {'repository_scope': original.repository_scope.model_copy(update={
                'writable_repository_ids': ('foreign-repository',)})},
        )
        for change in changes:
            with self.subTest(fields=tuple(change)):
                changed = original.model_copy(deep=True, update=change)
                self.f.assignments[original.id] = changed
                self.n.reserved = changed
                with self.assertRaises(HTTPException) as refused:
                    self.eligibility()
                self.assertEqual(refused.exception.status_code, 409)
                self.assertNotEqual(refused.exception.detail['code'], 'owner_current_work_cleanup_unavailable')
                self.assertEqual(self.f.assignments[original.id], changed)
        self.f.assignments[original.id] = original
        self.n.reserved = original
        reference = self.n.routing.snapshot(self.state).definition
        registry = self.f.f.env.env.registry.store
        self.assertEqual(sum(record.record_id == reference.record_id for record in registry.load()), 1)
        registry.update(lambda records: [record.model_copy(update={
            'lifecycle': DefinitionLifecycle.QUARANTINED})
            if record.record_id == reference.record_id else record for record in records])
        self.assertEqual(next(record for record in registry.load()
            if record.record_id == reference.record_id).lifecycle, DefinitionLifecycle.QUARANTINED)
        with self.assertRaises(HTTPException) as refused:
            self.eligibility()
        self.assertEqual(refused.exception.status_code, 409)
        self.assertIn(refused.exception.detail['code'], {
            'owner_current_work_lineage_unverified', 'owner_routing_unavailable',
            'owner_routing_owner_unavailable'})
        self.assertEqual(self.f.f.preflight.get(started.id).model_dump(mode='json'), retained)
        self.assertEqual(self.f.assignments[original.id], original)
        self.n.threads.create.assert_awaited_once()

    async def test_absent_reservation_preserves_ordinary_first_native_admission(self):
        # This is the normal pre-native state, not a missing historical owner:
        # its canonical bootstrap is live and has no native Session yet.
        self.n.store.update(lambda state: state.model_copy(update={'sessions': []}))
        preparer = self.n.routing.recipient_preparer
        with patch.object(preparer, 'context', wraps=preparer.context) as context:
            selected = self.n.routing.compatible_pin(self.state, self.n.binding,
                'operator', writable_repository_ids=('platform',))
        self.assertEqual(selected.original_thread_id, self.n.binding.thread_id)
        self.assertEqual(selected.binding.configured_owner, 'operator')
        self.assertEqual(selected.binding.canonical_owner, 'orchestrator')
        self.assertEqual(selected.schema_version, '1.0')
        self.assertIsNone(selected.recipient)
        context.assert_not_called()
        self.n.threads.create.assert_not_awaited()
        self.assertIsNone(self.n.reserved)

    async def test_bound_successor_after_lost_create_reply_is_reused_without_second_creation(self):
        physical = await self.owned_replacement_service()
        started = await self.started()
        old = self.pin.recipient.thread_id
        await self.terminal_owned(physical)
        actual_create = physical.recovery.thread_creator
        async def lost_reply(**kwargs):
            await actual_create(**kwargs)
            raise ConnectionError('Controlled external create reply lost')
        physical.recovery.thread_creator = lost_reply
        with self.assertRaises(ConnectionError):
            await self.service.replace(old, work_item_ref=self.state.ref,
                actor=self.f.f.actor, project=self.project)
        self.assertIsNone(physical.recovery.replacement_thread_id(old))
        self.assertEqual(len(self.new_calls), 1)
        result = await self.service.replace(old, work_item_ref=self.state.ref,
            actor=self.f.f.actor, project=self.project)
        self.assertEqual(result['newThreadId'], 'successor-thread-1')
        self.assertEqual(len(self.new_calls), 1)
        self.assertEqual(physical.recovery.replacement_thread_id(old), result['newThreadId'])
        self.assertEqual(self.f.f.preflight.get(started.id), started)

    async def test_reserved_successor_without_bound_native_custody_is_not_recreated(self):
        from codex_web.execution_subjects import ExecutionSubject
        physical = await self.owned_replacement_service()
        await self.started()
        old = self.pin.recipient.thread_id
        original = await self.terminal_owned(physical)
        calls = []
        async def uncertain_create(**kwargs):
            kwargs['admission_guard']()
            calls.append(kwargs)
            token = kwargs['bootstrap_token']
            reserved = original.model_copy(deep=True, update={
                'id': 'uncertain-successor-assignment', 'status': AssignmentStatus.RUNNING,
                'subject': ExecutionSubject(kind='thread_bootstrap', ref='bootstrap-' + token),
                'execution_id': 'thread-bootstrap-' + token,
                'execution_workspace_id': 'uncertain-successor-workspace',
                'lease': self.n.template.lease.model_copy(deep=True), 'bootstrap_create_observation': None})
            physical.store.update(lambda state: state.model_copy(update={
                'assignments': state.assignments + [reserved]}))
            raise ConnectionError('Controlled external creation outcome unavailable')
        physical.recovery.thread_creator = uncertain_create
        with self.assertRaises(ConnectionError):
            await self.service.replace(old, work_item_ref=self.state.ref,
                actor=self.f.f.actor, project=self.project)
        reserved = physical.store.assignment('uncertain-successor-assignment').model_dump(mode='json')
        with self.assertRaises(HTTPException) as refused:
            await self.service.replace(old, work_item_ref=self.state.ref,
                actor=self.f.f.actor, project=self.project)
        self.assertEqual(refused.exception.detail['code'], 'owner_replacement_original_unresolved')
        self.assertEqual(len(calls), 1)
        self.assertIsNone(physical.recovery.replacement_thread_id(old))
        self.assertEqual(physical.store.assignment('uncertain-successor-assignment').model_dump(mode='json'), reserved)

    async def test_current_profile_change_at_final_creator_guard_refuses_before_new_native_creation(self):
        physical = await self.owned_replacement_service()
        started = await self.started()
        old = self.pin.recipient.thread_id
        original = await self.terminal_owned(physical)
        entered = []
        async def changed_before_create(**kwargs):
            entered.append(True)
            def change(state):
                current = state.assignments[0]
                state.assignments = [current.model_copy(update={'agent_profile':
                    current.agent_profile.model_copy(update={'profile_revision': 999})})]
                return state
            physical.store.transition_assignment(original.assigned_worker_id, original.id, change)
            kwargs['admission_guard']()
            self.fail('Changed Profile reached native creator')
        physical.recovery.thread_creator = changed_before_create
        with self.assertRaises(HTTPException) as refused:
            await self.service.replace(old, work_item_ref=self.state.ref,
                actor=self.f.f.actor, project=self.project)
        self.assertEqual(refused.exception.status_code, 409)
        self.assertEqual(refused.exception.detail['code'], 'owner_executor_profile_changed')
        self.assertEqual(entered, [True])
        self.assertEqual(self.new_calls, [])
        self.assertIsNone(physical.recovery.replacement_thread_id(old))
        self.assertEqual(self.f.f.preflight.get(started.id), started)
