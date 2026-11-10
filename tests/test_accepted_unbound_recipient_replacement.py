"""Accepted native lineage survives a logical anchor in another repository."""
import time
import unittest
from types import SimpleNamespace

from codex_web.execution_subjects import ExecutionSubject
from codex_web.execution_workers import AssignmentStatus
from tests import test_owner_native_session_recipient as native_fixtures
from tests import test_owner_cancelled_new_handoff as start_fixtures


class AcceptedUnboundRecipientReplacementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.n = native_fixtures.NativeRecipientScopeTests()
        self.n.setUp()
        self.addCleanup(self.n.doCleanups)
        self.f = self.n.fixture
        # The logical Role's native scope is core; the real Work is platform.
        # Both the current assignment and retained native predecessor agree.
        source = self.f.assignments['assignment-existing-thread']
        source.resource_ids = self.n.original.resource_ids
        source.repository_scope = self.n.original.repository_scope.model_copy(deep=True)
        self.n.prepare_fixture()
        self.n.threads.create.side_effect = self.create
        self.nonce = time.time()
        self.n.state.handoff.requested_at = self.nonce
        self.f.f.env.states.put(self.n.state.ref, self.n.state)

    async def create(self, **kwargs):
        kwargs['admission_guard']()
        token = kwargs['bootstrap_token']
        assignment = self.n.template.model_copy(deep=True, update={
            'id': 'fresh-recipient-assignment', 'status': AssignmentStatus.RUNNING,
            'subject': ExecutionSubject(kind='thread_bootstrap', ref='bootstrap-' + token),
            'execution_workspace_id': 'fresh-recipient-workspace',
            'execution_id': 'thread-bootstrap-' + token})
        if getattr(self, 'sealed_source', False):
            from codex_web.execution_workers import NativeCredentialCustody
            self.f.worker.service_identity_id = 'private-worker-identity'
            assignment.native_credential_custody = NativeCredentialCustody(
                runtime_secret_ref='private-runtime-reference', admitted_at=time.time() - 1,
                requester_identity_id=assignment.created_by,
                worker_identity_id=self.f.worker.service_identity_id, fence=assignment.fence,
                procedure_secret_refs=('private-procedure-reference',))
            assignment.secret_refs = ('private-runtime-reference', 'private-procedure-reference')
        self.f.assignments[assignment.id] = assignment
        self.n.reserved = assignment
        self.n.bound = SimpleNamespace(id='fresh-recipient-binding',
            bootstrap_id='bootstrap-' + token, thread_id='fresh-recipient-thread',
            assignment_id=assignment.id, execution_id=assignment.execution_id,
            execution_workspace_id=assignment.execution_workspace_id,
            created_by=assignment.created_by, organization_id='org-a', workspace_id='workspace-a')
        self.f.bootstraps['fresh-recipient-thread'] = self.n.bound
        return {'thread': {'id': 'fresh-recipient-thread'}}

    async def prepared(self):
        return await self.n.prepare()

    def validate(self, pin, attempt_id=None):
        self.n.routing.validate_native(pin, thread_id=pin.recipient.thread_id,
            project=self.f.f.env.env.projects.get('project-a', scope=self.f.f.env.env.scope),
            writable_repository_resource_ids=('platform',), read_only_repository_resource_ids=(),
            work_item_ref=self.n.state.ref, handoff_requested_at=self.nonce,
            queued_admission_attempt_id=attempt_id, agent_profile_id=self.f.f.profile.profile_id,
            agent_profile_revision=self.f.f.profile.revision,
            agent_profile_actor_id=self.f.f.actor.identity_id)

    async def accepted(self):
        # The retained original, actual correlated native START, delivery and
        # genuine ACK are produced by the existing canonical fixture owners.
        await start_fixtures.CancelledNewHandoffRecipientTests.test_actual_native_start_own_exclusion_delivery_and_ack(self)
        return self.f.f.env.states.get(self.n.state.ref)

    async def test_active_accepted_cross_scope_recipient_uses_own_start_not_cancelled_anchor(self):
        state = await self.accepted()
        before = self.f.f.binding.model_dump(mode='json')
        result, error = None, None
        try:
            result = self.n.routing.compatible_pin(state, self.n.binding, 'orchestrator',
                writable_repository_ids=('platform',))
        except Exception as exc:
            error = type(exc).__name__ + ':' + str(exc)
        self.assertIsNone(error, error)
        self.assertEqual(result.recipient.thread_id, state.handoff.delivery.thread_id)
        self.assertEqual(result.recipient.assignment_id, 'fresh-recipient-assignment')
        self.assertEqual(self.f.f.binding.model_dump(mode='json'), before)
        self.n.threads.create.assert_awaited_once()

    def session_for(self, thread, assignment):
        from codex_web.agent_runtime import AgentSessionStatus
        session = self.n.session.model_copy(deep=True, update={
            'id': 'native-' + thread, 'provider_native_session_id': thread,
            'assignment_id': assignment.id, 'execution_id': assignment.execution_id,
            'execution_workspace_id': assignment.execution_workspace_id,
            'worker_id': assignment.assigned_worker_id, 'resource_ids': assignment.resource_ids,
            'status': AgentSessionStatus.READY, 'failure': None})
        self.n.store.upsert(session)
        return session

    async def terminal(self):
        state = await self.accepted()
        assignment = self.f.assignments['fresh-recipient-assignment'].model_copy(deep=True)
        assignment.status = AssignmentStatus.CANCELLED
        assignment.fence += 1
        assignment.lease = None
        self.f.assignments[assignment.id] = assignment
        self.session_for(state.handoff.delivery.thread_id, assignment)
        return state, assignment

    def replacement_service(self, state):
        # This legacy lineage fixture controls only the identity I/O seam.
        # Real session/MFA provenance is covered by the authenticated-origin
        # regression module using the canonical IdentityService.
        self.n.routing.authenticated_origin_revalidator = lambda actor: actor
        from pathlib import Path
        from unittest.mock import AsyncMock, Mock
        from codex_web.models import ThreadRunSettings
        from codex_web.runtime.execution import TurnExecutionService
        from codex_web.services.thread_execution_settings import ThreadExecutionSettingsService
        from codex_web.services.thread_recovery import ThreadRecoveryService
        from codex_web.services.turns import TurnService
        from codex_web.storage.thread_index import ThreadIndexRepository
        project = self.f.f.env.env.projects.get('project-a', scope=self.f.f.env.env.scope)
        old = state.handoff.delivery.thread_id
        self.saved_settings = {old: ThreadRunSettings(sandbox=self.n.binding.sandbox,
            approval_policy=self.n.binding.approval_policy, execution_profile_id='repository-write',
            repository_resource_id='platform', writable_repository_resource_ids=('platform',))}
        settings = ThreadExecutionSettingsService(load_settings=lambda: self.saved_settings,
            save_settings=lambda rows: self.saved_settings.update(rows),
            bindings=SimpleNamespace(for_thread=lambda _: []), load_bindings=lambda: [self.n.binding],
            save_bindings=lambda _: self.fail('must preserve logical anchor'),
            gitlab_routing_enabled_for_project=lambda _: False,
            binding_report_name=lambda _: None, binding_prefix=lambda _: None)
        self.queue = {old: []}
        host = SimpleNamespace(THREAD_REPLACEMENTS={}, _load_turn_queues=lambda: self.queue,
            _save_turn_queues=lambda q: self.queue.update(q), _load_active_turns=lambda: {},
            _save_active_turns=lambda _: None, _load_slack_thread_icons=lambda: {},
            _save_slack_thread_icons=lambda _: None, hub=SimpleNamespace(publish=AsyncMock()),
            _retarget_bot_thread_state=Mock(side_effect=AssertionError('no bot target changes')))
        index = ThreadIndexRepository(self.f.f.env.env.state,
            Path(self.f.f.env.env.temp.name) / 'accepted-replacement-index.json')
        self.joined = []
        self.cancelled_new = []
        self.sessions = {}
        async def stop(key, **kwargs):
            self.assertEqual(key, 'fresh-recipient-assignment')
            self.assertEqual(kwargs['expected_terminal'], self.f.assignments[key])
            self.joined.append(key)
        async def cancel_bootstrap(key, **kwargs):
            current = self.f.assignments[key]
            cancelled = current.model_copy(update={'status': AssignmentStatus.CANCELLED,
                'lease': None, 'fence': current.fence + 1})
            self.f.assignments[key] = cancelled
            self.sessions.pop(key, None)
            self.cancelled_new.append(key)
            return cancelled
        manager = SimpleNamespace(local_worker=SimpleNamespace(
            _pending_assignment=self.f.assignments.get), stop=AsyncMock(side_effect=stop),
            get=self.sessions.get, cancel_bootstrap=AsyncMock(side_effect=cancel_bootstrap))
        runtime = TurnExecutionService(SimpleNamespace(_load_active_turns=lambda: {}),
            session_manager=manager, bootstrap_bindings=self.n.routing.bootstraps,
            control_actor=self.f.f.actor)
        # Existing binding service point owner needs a canonical get signature.
        self.n.routing.bootstraps.get_by_thread = lambda thread, actor: self.f.bootstraps[thread]
        runtime._bootstrap_binding_for_thread = lambda thread: self.f.bootstraps.get(thread)
        self.new_calls = []
        self.n.routing.recipient_preparer.existing_assignment = lambda **kw: next(
            (a for a in self.f.assignments.values() if a.execution_id == kw['execution_id']), None)
        from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingNotFoundError
        def exact_bootstrap(key, actor):
            rows = [b for b in self.f.bootstraps.values() if getattr(b, 'bootstrap_id', None) == key]
            if len(rows) != 1:
                raise ThreadBootstrapBindingNotFoundError('Private exact bootstrap absent')
            return rows[0]
        self.n.routing.bootstraps.get_by_bootstrap = exact_bootstrap
        async def create(**kwargs):
            self.assertEqual(self.joined, ['fresh-recipient-assignment'])
            kwargs['admission_guard']()
            self.new_calls.append(kwargs)
            predecessor = self.f.assignments['fresh-recipient-assignment']
            new = predecessor.model_copy(deep=True, update={
                'id': 'successor-assignment', 'status': AssignmentStatus.RUNNING,
                'subject': ExecutionSubject(kind='thread_bootstrap', ref='bootstrap-' + kwargs['bootstrap_token']),
                'fence': 1, 'lease': self.n.template.lease.model_copy(deep=True),
                'execution_id': 'thread-bootstrap-' + kwargs['bootstrap_token'],
                'execution_workspace_id': 'successor-workspace'})
            if new.native_credential_custody is not None:
                new.native_credential_custody = new.native_credential_custody.model_copy(
                    update={'admitted_at': time.time(), 'fence': new.fence})
            self.f.assignments[new.id] = new
            self.f.bootstraps['successor-thread'] = SimpleNamespace(
                id='successor-binding', bootstrap_id='bootstrap-' + kwargs['bootstrap_token'], thread_id='successor-thread',
                assignment_id=new.id, execution_id=new.execution_id,
                execution_workspace_id=new.execution_workspace_id, created_by=new.created_by,
                organization_id='org-a', workspace_id='workspace-a')
            self.session_for('successor-thread', new)
            self.sessions[new.id] = SimpleNamespace(validate_current=lambda: self.f.assignments[new.id],
                status=lambda: SimpleNamespace(running=True, ready=True, fence=1, pending_executor_work=0),
                managed_validation=SimpleNamespace(blocks_assignment_renewal=lambda: False))
            if getattr(self, 'during_create', None) is not None:
                self.during_create()
            return {'thread': {'id': 'successor-thread'}}
        recovery = ThreadRecoveryService(host, projects=SimpleNamespace(get=lambda _: project),
            settings=settings, naming=SimpleNamespace(set_name=AsyncMock()), thread_index=index,
            runtime_request=AsyncMock(), event_sink=lambda _: None, truncate_text=lambda x,n: x[:n],
            thread_creator=create)
        recovery.predecessor_retirement = runtime.retire_replacement_predecessor
        host._archive_replaced_bot_thread = recovery.archive_replaced_bot_thread
        self.n.routing.replacement_resolver = index.replacement_thread_id
        service = TurnService(projects=SimpleNamespace(get=lambda _: project), settings=settings,
            recovery=recovery, resume_runtime=None, bindings=SimpleNamespace(for_thread=lambda _: []),
            queue_policy=SimpleNamespace(depth=lambda key: len(self.queue.get(key, []))),
            execution=SimpleNamespace(schedule_queue_drain=Mock()), event_sink=lambda _: None,
            truncate_text=lambda x,n:x[:n], binding_public=lambda x:x)
        service.owner_routing = self.n.routing
        return service, project, recovery

    async def test_explicit_operator_replaces_cancelled_unbound_then_normal_owner_continues(self):
        state, assignment = await self.terminal()
        service, project, recovery = self.replacement_service(state)
        before = state.model_dump(mode='json')
        binding_before = self.n.binding.model_dump(mode='json')
        from pathlib import Path
        marker = Path(self.f.f.env.env.temp.name) / 'retained-previous-workspace.txt'
        marker.write_text('unpublished source bytes')
        from codex_web.owner_routing import OwnerRoutingError
        with self.assertRaises(OwnerRoutingError):
            self.n.routing.compatible_pin(state, self.n.binding, 'orchestrator',
                writable_repository_ids=('platform',))
        result = await service.replace(state.handoff.delivery.thread_id,
            work_item_ref=state.ref, actor=self.f.f.actor, project=project)
        self.assertEqual(result['newThreadId'], 'successor-thread')
        self.assertEqual(self.f.assignments[assignment.id], assignment)
        self.assertEqual(self.n.binding.model_dump(mode='json'), binding_before)
        self.assertEqual(self.f.f.env.states.get(state.ref).model_dump(mode='json'), before)
        self.assertEqual(marker.read_text(), 'unpublished source bytes')
        self.assertEqual(recovery.replacement_thread_id(state.handoff.delivery.thread_id), 'successor-thread')
        self.assertEqual(len(self.new_calls), 1)
        # Existing continuity owner uses the SAME accepted Work, not a fake
        # handoff, ACK or replay of the already-started original.
        self.f.fixture.dispatch.reset_mock()
        await self.f.continuity.dispatch_actionable_owner(state, source='operator-retry')
        self.f.fixture.dispatch.assert_awaited_once()
        pin = self.f.fixture.dispatch.call_args.kwargs['owner_routing_pin']
        self.assertEqual(pin.recipient.thread_id, 'successor-thread')
        self.assertIsNone(pin.handoff_requested_at)
        self.assertEqual(self.n.threads.create.await_count, 1)
        self.assertEqual(self.f.f.env.states.get(state.ref).handoff.requested_at, self.nonce)

    async def test_cancelled_recipient_is_never_automatic_preparation_authority(self):
        from codex_web.owner_routing import OwnerRoutingError
        state, assignment = await self.terminal()
        before = assignment.model_dump(mode='json')
        with self.assertRaises(OwnerRoutingError):
            await self.n.routing.prepare_compatible_pin(state, self.n.binding,
                'orchestrator', writable_repository_ids=('platform',))
        self.assertEqual(self.f.assignments[assignment.id].model_dump(mode='json'), before)
        self.n.threads.create.assert_awaited_once()

    async def test_missing_explicit_work_or_foreign_actor_cannot_replace(self):
        from fastapi import HTTPException
        from codex_web.identity import MembershipRole
        state, _ = await self.terminal()
        service, project, _ = self.replacement_service(state)
        with self.assertRaises(HTTPException):
            await service.replace(state.handoff.delivery.thread_id)
        self.assertEqual(self.new_calls, [])
        with self.assertRaisesRegex(HTTPException, 'owner_recipient_accepted_lineage_unverified'):
            await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
                actor=self.f.f.actor.model_copy(update={'workspace_id': 'foreign'}), project=project)
        self.assertEqual(self.new_calls, [])
        # An existing tenant member is still not an operator recovery grant.
        actor = self.f.f.actor.model_copy(update={'roles': (MembershipRole.MEMBER,)})
        resolver = self.n.routing.actor_resolver
        self.n.routing.actor_resolver = lambda *_: actor
        from codex_web.services.identity import AuthorizationError
        with self.assertRaises(AuthorizationError):
            await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
                actor=actor, project=project)
        self.n.routing.actor_resolver = resolver
        self.assertEqual(self.new_calls, [])

    async def test_missing_start_unknown_or_fence_drift_refuses_before_replacement(self):
        from fastapi import HTTPException
        state, assignment = await self.terminal()
        service, project, _ = self.replacement_service(state)
        rows = self.f.f.preflight.for_work_item(organization_id='org-a', workspace_id='workspace-a',
            project_id='project-a', ref=state.ref)
        original = rows[0]
        self.f.f.preflight.put(original.model_copy(update={'queued_start_receipt': None}))
        with self.assertRaisesRegex(HTTPException, 'accepted_lineage_unverified'):
            await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
                actor=self.f.f.actor, project=project)
        self.f.f.preflight.put(original)
        self.f.assignments[assignment.id] = assignment.model_copy(update={'fence': assignment.fence + 1})
        with self.assertRaisesRegex(HTTPException, 'custody_changed'):
            await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
                actor=self.f.f.actor, project=project)
        self.f.assignments[assignment.id] = assignment
        unknown = self.f.f.uncertain()
        self.f.f.preflight.put(unknown)
        with self.assertRaisesRegex(HTTPException, 'native_outcome_unresolved'):
            await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
                actor=self.f.f.actor, project=project)
        self.assertEqual(self.f.f.preflight.get(unknown.id), unknown)
        self.assertEqual(self.new_calls, [])

    async def test_current_work_drift_during_native_create_joins_unused_successor_without_graph(self):
        from fastapi import HTTPException
        state, original = await self.terminal()
        service, project, recovery = self.replacement_service(state)
        def change():
            current = self.f.f.env.states.get(state.ref)
            current.handoff.requested_at += 1
            self.f.f.env.states.put(state.ref, current)
        self.during_create = change
        with self.assertRaises(HTTPException):
            await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
                actor=self.f.f.actor, project=project)
        self.assertIsNone(recovery.replacement_thread_id(state.handoff.delivery.thread_id))
        self.assertEqual(self.cancelled_new, ['successor-assignment'])
        self.assertEqual(self.f.assignments['successor-assignment'].status, AssignmentStatus.CANCELLED)
        self.assertEqual(self.f.assignments[original.id], original)
        self.assertNotIn('successor-assignment', self.sessions)
        self.assertEqual(len(self.new_calls), 1)

    async def test_existing_exact_successor_is_idempotent_without_second_provider_attempt(self):
        state, _ = await self.terminal()
        service, project, _ = self.replacement_service(state)
        first = await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
            actor=self.f.f.actor, project=project)
        second = await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
            actor=self.f.f.actor, project=project)
        self.assertEqual(first['newThreadId'], second['newThreadId'])
        self.assertEqual(len(self.new_calls), 1)
        self.assertEqual(self.joined, ['fresh-recipient-assignment'])

    async def test_unknown_reserved_replacement_cannot_mint_a_second_attempt(self):
        from codex_web.owner_routing import OwnerRoutingError
        state, original = await self.terminal()
        service, project, _ = self.replacement_service(state)
        context, _ = service.replacement_eligibility(state.handoff.delivery.thread_id,
            work_item_ref=state.ref, actor=self.f.f.actor, project=project)
        token, existing = self.n.routing.accepted_replacement_reservation(context)
        self.assertIsNone(existing)
        pending = original.model_copy(deep=True, update={
            'id': 'unresolved-replacement', 'execution_id': 'thread-bootstrap-' + token,
            'status': AssignmentStatus.RUNNING})
        self.f.assignments[pending.id] = pending
        with self.assertRaisesRegex(OwnerRoutingError, 'owner_replacement_original_unresolved'):
            await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
                actor=self.f.f.actor, project=project)
        self.assertEqual(self.f.assignments[pending.id], pending)
        self.assertEqual(self.new_calls, [])

    async def test_metadata_eligibility_has_no_effect_and_live_original_is_not_cancelled(self):
        from fastapi import HTTPException
        state = await self.accepted()
        assignment = self.f.assignments['fresh-recipient-assignment']
        self.session_for(state.handoff.delivery.thread_id, assignment)
        service, project, _ = self.replacement_service(state)
        with self.assertRaisesRegex(HTTPException, 'custody_changed|requires_cancelled_recipient'):
            service.replacement_eligibility(state.handoff.delivery.thread_id,
                work_item_ref=state.ref, actor=self.f.f.actor, project=project)
        self.assertEqual(self.f.assignments[assignment.id], assignment)
        self.assertEqual(self.new_calls, [])
        self.assertEqual(self.joined, [])

    async def test_nonce_profile_scope_or_current_actor_drift_cannot_prepare(self):
        from fastapi import HTTPException
        state, assignment = await self.terminal()
        service, project, _ = self.replacement_service(state)
        changes = [
            {'project_id': 'foreign'},
            {'agent_profile': assignment.agent_profile.model_copy(update={'profile_revision': 999})},
            {'repository_scope': assignment.repository_scope.model_copy(
                update={'writable_repository_ids': ('core',)})},
            {'resource_ids': ('core',)},
        ]
        for change in changes:
            with self.subTest(change=tuple(change)):
                self.f.assignments[assignment.id] = assignment.model_copy(deep=True, update=change)
                with self.assertRaises(HTTPException):
                    await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
                        actor=self.f.f.actor, project=project)
                self.assertEqual(self.new_calls, [])
                self.assertEqual(self.joined, [])
        self.f.assignments[assignment.id] = assignment
        changed = state.model_copy(deep=True)
        changed.handoff.requested_at += 1
        self.f.f.env.states.put(state.ref, changed)
        with self.assertRaisesRegex(HTTPException, 'accepted_lineage_unverified'):
            await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
                actor=self.f.f.actor, project=project)
        self.assertEqual(self.new_calls, [])

    async def test_replacement_preserves_retained_preflight_and_queue_payload(self):
        from codex_web.models import QueuedTurn
        state, _ = await self.terminal()
        service, project, _ = self.replacement_service(state)
        old = state.handoff.delivery.thread_id
        queued = QueuedTurn(id='known-unstarted-owner-input', thread_id=old,
            project_id='project-a', message='Preserve this already queued request',
            work_item_ref=state.ref, created_at=time.time())
        self.queue[old] = [queued]
        attempts = self.f.f.preflight.for_work_item(organization_id='org-a',
            workspace_id='workspace-a', project_id='project-a', ref=state.ref)
        before = [a.model_dump(mode='json') for a in attempts]
        await service.replace(old, work_item_ref=state.ref, actor=self.f.f.actor, project=project)
        self.assertEqual([q.id for q in self.queue['successor-thread']], [queued.id])
        self.assertEqual(self.queue['successor-thread'][0].message, queued.message)
        self.assertEqual(self.queue['successor-thread'][0].thread_id, 'successor-thread')
        after = self.f.f.preflight.for_work_item(organization_id='org-a',
            workspace_id='workspace-a', project_id='project-a', ref=state.ref)
        self.assertEqual([a.model_dump(mode='json') for a in after], before)

    async def test_real_thread_create_rechecks_after_native_await_and_uses_existing_cleanup(self):
        from fastapi import HTTPException
        from tests import test_thread_bootstrap_routing as bootstrap
        fixture = bootstrap.ThreadBootstrapCreateTests()
        session = bootstrap._Session()
        original_request = session.request
        admitted = True
        async def request(*args, **kwargs):
            nonlocal admitted
            result = await original_request(*args, **kwargs)
            admitted = False
            return result
        session.request = request
        host, planner, manager, bindings, service = fixture._service(session=session)
        def guard():
            if not admitted:
                raise HTTPException(status_code=409, detail={'code': 'owner_replacement_context_changed'})
        with self.assertRaises(HTTPException) as caught:
            await service.create(project_id='p1', admission_guard=guard)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail, {'code': 'owner_replacement_context_changed'})
        from codex_web.services.threads import ThreadBootstrapPrestartRefusal, bootstrap_preparation_diagnostic
        self.assertIsNone(bootstrap_preparation_diagnostic(caught.exception))
        # A post-start phase can never become a fabricated no-provider-call
        # witness, even when a caller supplies the typed diagnostic envelope.
        self.assertIsNone(bootstrap_preparation_diagnostic(ThreadBootstrapPrestartRefusal(
            status_code=503, detail={'code': 'bootstrap_prestart_failed',
                'bootstrap_preparation_stage': 'admission_after_provider_start',
                'bootstrap_exception_type': 'RuntimeError', 'provider_start_called': False})))
        self.assertEqual(len(manager.completed), 1)
        self.assertEqual(manager.completed[0][0], 'assignment-bootstrap')
        self.assertFalse(manager.completed[0][1]['succeeded'])
        self.assertEqual(bindings.calls, [])
        self.assertEqual(len(session.requests), 1)
        host.codex.request.assert_not_awaited()

    async def test_sealed_original_lost_publication_reply_reuses_exact_completed_bootstrap(self):
        self.sealed_source = True
        state, original = await self.terminal()
        service, project, recovery = self.replacement_service(state)
        remember = recovery._remember_replacement
        def interrupted(*args, **kwargs):
            raise RuntimeError('private interruption before graph commit')
        recovery._remember_replacement = interrupted
        with self.assertRaisesRegex(RuntimeError, 'before graph commit'):
            await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
                actor=self.f.f.actor, project=project)
        self.assertIsNone(recovery.replacement_thread_id(state.handoff.delivery.thread_id))
        self.assertEqual(len(self.new_calls), 1)
        recovery._remember_replacement = remember
        result = await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
            actor=self.f.f.actor, project=project)
        self.assertEqual(result['newThreadId'], 'successor-thread')
        self.assertEqual(len(self.new_calls), 1)
        self.assertEqual(self.f.assignments[original.id], original)
        self.assertEqual(recovery.replacement_thread_id(state.handoff.delivery.thread_id), 'successor-thread')

    async def test_observation_only_work_updates_during_create_preserve_authorized_replacement(self):
        from codex_web.models import TaskSourceIdentity, TaskSourceProjectionState
        state, original = await self.terminal()
        service, project, recovery = self.replacement_service(state)
        def observe():
            current = self.f.f.env.states.get(state.ref)
            current.updated_at += 1
            current.last_gitlab_event_at = time.time()
            current.notes.append('Canonical provider observation, no owner or intent change')
            current.source_projection = TaskSourceProjectionState(
                desired_revision='a' * 64,
                target=TaskSourceIdentity(source_type='gitlab', source_instance='private-fixture',
                    external_id='platform!1'),
                desired_stage=current.current_stage, status='succeeded', attempts=1,
                attempted_at=time.time(), observed_revision='private-observed-revision')
            self.f.f.env.states.put(state.ref, current)
        self.during_create = observe
        result = await service.replace(state.handoff.delivery.thread_id, work_item_ref=state.ref,
            actor=self.f.f.actor, project=project)
        self.assertEqual(result['newThreadId'], 'successor-thread')
        self.assertEqual(self.cancelled_new, [])
        self.assertEqual(self.f.assignments['successor-assignment'].status, AssignmentStatus.RUNNING)
        self.assertEqual(self.f.assignments[original.id], original)
        self.assertEqual(recovery.replacement_thread_id(state.handoff.delivery.thread_id), 'successor-thread')
        observed = self.f.f.env.states.get(state.ref)
        self.assertEqual(observed.handoff, state.handoff)
        self.assertEqual(observed.current_owner, state.current_owner)
        self.assertEqual(observed.source_projection.status, 'succeeded')
        self.assertEqual(len(self.new_calls), 1)
