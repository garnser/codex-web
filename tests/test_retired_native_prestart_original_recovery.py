"""A real before-turn refusal must not strand its original on retirement.

Only native process transport uses the existing inert runtime fixture. Work,
published routing/Profile, preflight claims, worker cancellation and recovery
inspection use their canonical owners. No cleanup or provider nonstart witness
is synthesized, and no new input or handoff nonce is introduced.
"""
import unittest
import asyncio
import time
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from codex_web.execution_workers import (
    AssignmentCancelRequest, AssignmentRenewRequest, ExecutionRuntimeBinding, ExecutionWorkerRegister, WorkerCapability,
)
from codex_web.execution_subjects import ExecutionSubject
from codex_web.identity import AuthenticationAssurance, PrincipalKind
from codex_web.models import IndexedThread, QueuedTurn, WorkItemAckCreate
from codex_web.resources import RepositoryExecutionTarget
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.queued_bootstrap_recovery import QueuedBootstrapRecoveryService
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from codex_web.storage.thread_index import ThreadIndexRepository
from codex_web.services.queued_native_start import QueuedNativeStartCapture
from tests import test_owner_routing_recipients as owner_fixture
from tests import test_turn_execution as native_fixture


class RetiredNativePrestartOriginalRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def retired_context(self, *, seal=True, change_claim=False, retire=True):
        routing_fixture = owner_fixture.CompatibleRecipientTests()
        routing_fixture.setUp()
        self.addCleanup(routing_fixture.doCleanups)
        f = routing_fixture
        backend = f.f.env.env.state
        actor = f.f.actor
        self.assertEqual(actor.assurance, AuthenticationAssurance.MFA)
        project = f.f.env.env.projects.get('project-a', scope=f.f.env.env.scope)
        root = Path(f.f.env.env.temp.name)

        # Persist the existing fixture's exact scoped custody, then use the
        # real worker owner for retirement rather than rewriting its status.
        worker_store = ExecutionWorkerStore(backend)
        workers = ExecutionWorkerService(worker_store)
        assignments = tuple(f.assignments.values())
        capabilities = tuple(dict.fromkeys((WorkerCapability.GIT,
            WorkerCapability.COMMAND_EXECUTION,
            *(capability for row in assignments for capability in row.required_capabilities))))
        worker = workers.register(ExecutionWorkerRegister(
            service_identity_id='retained-original-worker', pool='local',
            version='test', capabilities=capabilities, max_concurrency=len(assignments),
            supported_execution_contract_versions=tuple(dict.fromkeys(
                row.execution_contract_version for row in assignments)),
            supported_sandbox_profiles=tuple(dict.fromkeys(row.sandbox for row in assignments))), actor=actor)
        worker_store._ensure_records()
        for thread, bootstrap in f.bootstraps.items():
            assignment = f.assignments[bootstrap.assignment_id]
            assignment.subject = ExecutionSubject(kind='thread_bootstrap', ref=bootstrap.id)
            scope = assignment.repository_scope
            assignment.repository_target = RepositoryExecutionTarget(
                organization_id=scope.organization_id, workspace_id=scope.workspace_id,
                project_id=scope.project_id, mutable_repository_id=scope.writable_repository_ids[0],
                read_only_repository_ids=scope.read_only_repository_ids, source=scope.source)
            assignment.runtime_binding = ExecutionRuntimeBinding(
                provider_id='openai', runtime_id='codex', capability_revision=1,
                sandbox_profiles=(assignment.sandbox,))
            assignment.assigned_worker_id = worker.id
            assignment.lease = assignment.lease.model_copy(update={'worker_id': worker.id})
        backend.record_apply(worker_store.assignment_namespace, upserts={
            row.id: row.model_dump(mode='json') for row in f.assignments.values()})
        f.service.assignments = worker_store.assignment
        f.service.workers = worker_store.worker

        bootstraps = ThreadBootstrapBindingService(ThreadBootstrapBindingStore(backend))
        for thread, item in f.bootstraps.items():
            bootstraps.bind(bootstrap_id=item.id, thread_id=thread,
                assignment_id=item.assignment_id, execution_id=item.execution_id,
                execution_workspace_id=item.execution_workspace_id, actor=actor)
        f.service.bootstraps = bootstraps
        pin = f.select()
        self.assertIsNotNone(pin.recipient)
        f.validate(pin)
        source = worker_store.assignment(pin.recipient.assignment_id)

        preflight = ExecutionPreflightService(ExecutionPreflightStore(backend))
        queued = QueuedTurn(id='retained-context-original', thread_id=pin.recipient.thread_id,
            project_id=project.id, message='Continue the existing Work under its exact handoff.',
            source='work-item-handoff', created_at=1, execution_id='same-retained-execution',
            work_item_ref=f.state.ref, handoff_requested_at=f.state.handoff.requested_at,
            owner_routing_pin=pin, sandbox=source.sandbox, approval_policy=source.approval_policy,
            execution_profile_id=source.execution_profile_id,
            repository_resource_id='platform', writable_repository_resource_ids=('platform',),
            agent_profile_id=f.f.profile.profile_id, agent_profile_revision=f.f.profile.revision,
            agent_profile_actor_id=actor.identity_id)
        attempt = preflight.retain_queued(queued, organization_id=actor.organization_id,
            workspace_id=actor.workspace_id)
        snapshot = attempt.queued_turn_snapshot
        snapshot_sha = preflight.queued_snapshot_digest(snapshot)
        _, claim = preflight.claim_queued_admission(attempt.id)
        self.assertTrue(preflight.begin_queued_start(attempt.id, claim_id=claim))

        host, _, sessions, runtime = native_fixture.TurnExecutionStartTests()._service()
        runtime.bootstrap_bindings = bootstraps
        runtime.control_actor = actor
        runtime.owner_routing_validator = f.service.validate_native
        runtime.queued_preflight = preflight
        sessions.session.assignment = source.model_copy(deep=True)
        sessions.session.status = lambda: SimpleNamespace(worker_id=worker.id, fence=source.fence)
        sessions.get = lambda aid: sessions.session if aid == source.id else None
        sessions.local_worker = SimpleNamespace(_pending_assignment=worker_store.assignment)
        # The real source binding is pending its first input, so compose the
        # fixture's canonical adapter thread/start before the renderer refusal.
        # A durable Session row alone cannot attest this process generation.
        await sessions.session.create_initial_native_session(queued.thread_id)
        from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
        self.assertTrue(CodexAgentRuntimeAdapter(sessions.session).initial_session_is_live(queued.thread_id))
        # Real context renderer refuses an oversized required application
        # fragment before resume/turn RPC. No typed refusal is injected.
        host._effective_developer_instructions = lambda *args: '🙂' * 48_000
        with self.assertRaises(native_fixture.ExecutionPrestartRefusal) as refused:
            await runtime.start_thread_turn_now(queued.thread_id, project=project,
                message=queued.message, source=queued.source, execution_id=queued.execution_id,
                work_item_ref=queued.work_item_ref, handoff_requested_at=queued.handoff_requested_at,
                owner_routing_pin=pin, sandbox=queued.sandbox, approval_policy=queued.approval_policy,
                repository_resource_id='platform', writable_repository_resource_ids=('platform',),
                agent_profile_id=queued.agent_profile_id, agent_profile_revision=queued.agent_profile_revision,
                agent_profile_actor_id=actor.identity_id, actor=actor,
                queued_admission_attempt_id=attempt.id, queued_admission_claim_id=claim)
        self.assertEqual(refused.exception.detail['code'], 'native_context_delivery_limit')
        self.assertEqual(sessions.session.requests, [])
        observation = refused.exception.observation
        if change_claim:
            preflight.queued_disposition(attempt.id, status='queued', claim_id=claim)
            _, claim = preflight.claim_queued_admission(attempt.id)
            self.assertTrue(preflight.begin_queued_start(attempt.id, claim_id=claim))
        blocked = preflight.queued_disposition(attempt.id, status='blocked',
            detail=refused.exception.detail, claim_id=claim, preinput_refusal=True,
            native_preinput_observation=observation if seal else None)
        self.assertEqual(blocked.status, 'blocked')
        self.assertIsNone(blocked.queued_start_receipt)
        self.assertIsNone(blocked.queued_resume_correlation)

        if retire:
            retired = workers.cancel_bootstrap(source.id, AssignmentCancelRequest(
                expected_fence=source.fence, reason='Planned code-only maintenance'), actor=actor)
            self.assertEqual((str(retired.status), retired.fence, retired.lease),
                ('cancelled', source.fence + 1, None))
            # Canonical cancellation is not a fabricated joined-process cleanup or
            # before-provider bootstrap failure. Those facts must stay absent.
            self.assertIsNone(retired.failure)
        native_before = list(sessions.session.requests)

        # Reconstruct the controller-owned stores after deployment; do not
        # carry a Python claim/session handle forward as recovery authority.
        reopened = ExecutionPreflightService(ExecutionPreflightStore(backend))
        index = ThreadIndexRepository(backend, root / 'recovery-thread-index.json')
        index.upsert(IndexedThread(id=queued.thread_id, project_id=project.id,
            name='Existing scoped recipient', archived=False))
        recovery = QueuedBootstrapRecoveryService(preflight=reopened,
            threads=SimpleNamespace(), bootstrap_bindings=ThreadBootstrapBindingService(
                ThreadBootstrapBindingStore(backend)),
            assignments=ExecutionWorkerStore(backend).assignment, thread_index=index,
            actor_resolver=lambda identity, project: actor, thread_is_active=lambda thread: False)
        current = reopened.store.get(attempt.id)
        self.assertEqual(current.queued_turn_snapshot, snapshot)
        self.assertEqual(reopened.queued_snapshot_digest(current.queued_turn_snapshot), snapshot_sha)
        self.assertEqual(current.queued_turn_snapshot['handoff_requested_at'], queued.handoff_requested_at)
        self.assertEqual(current.queued_turn_snapshot['owner_routing_pin'], pin.model_dump(mode='json'))
        self.assertEqual(f.f.env.states.get(f.state.ref), f.state)
        self.assertEqual(sessions.session.requests, native_before)
        f.service.replacement_lineage = index.replacement_lineage
        recovery.owner_routing = f.service
        reopened.native_preinput_retry_validator = f.service.native_preinput_retry_guard
        runtime.queued_preflight = reopened
        return SimpleNamespace(f=f, backend=backend, actor=actor, project=project, root=root,
            workers=workers, worker_store=worker_store, bootstraps=bootstraps,
            queued=queued, attempt=current, snapshot=snapshot, snapshot_sha=snapshot_sha,
            recovery=recovery, preflight=reopened, index=index, pin=pin, source=source,
            native=sessions.session, observation=observation, host=host, runtime=runtime)

    async def test_live_context_fix_retries_same_original_and_archives_old_claim_proof(self):
        e = await self.retired_context(retire=False)
        before = e.preflight.store.get(e.attempt.id)
        proof = e.preflight.recorded_native_preinput_refusal(before)
        self.assertIsNotNone(proof)
        self.assertTrue(e.preflight.public(before, actor=e.actor)['can_retry'])
        e.host._effective_developer_instructions = lambda *args: 'Corrected bounded context.'
        retrying, retry, acquired = e.preflight.claim_retry(before.id, actor=e.actor)
        self.assertTrue(acquired)
        self.assertIsNone(e.preflight.recorded_native_preinput_refusal(retrying))
        archived = retrying.blocker_history[-1].blockers[0].model_dump(mode='json')
        self.assertEqual(archived['native_preinput_refusal'], proof.model_dump(mode='json'))
        e.preflight.queue_retry_committed(before.id, claim_id=retry)
        _, claim = e.preflight.claim_queued_admission(before.id)
        self.assertNotEqual(claim, proof.claim_id)
        self.assertTrue(e.preflight.begin_queued_start(before.id, claim_id=claim))
        unknown = e.preflight.store.get(before.id)
        self.assertIsNone(e.preflight.recorded_native_preinput_refusal(unknown))
        self.assertFalse(e.preflight.public(unknown, actor=e.actor)['can_retry'])
        with self.assertRaises(HTTPException):
            e.recovery.preview(before.id, project=e.project, actor=e.actor,
                thread_id=e.queued.thread_id)
        await e.runtime.start_thread_turn_now(e.queued.thread_id, project=e.project,
            message=e.queued.message, source=e.queued.source, execution_id=e.queued.execution_id,
            work_item_ref=e.queued.work_item_ref, handoff_requested_at=e.queued.handoff_requested_at,
            owner_routing_pin=e.pin, sandbox=e.queued.sandbox, approval_policy=e.queued.approval_policy,
            repository_resource_id='platform', writable_repository_resource_ids=('platform',),
            agent_profile_id=e.queued.agent_profile_id, agent_profile_revision=e.queued.agent_profile_revision,
            agent_profile_actor_id=e.actor.identity_id, actor=e.actor,
            queued_admission_attempt_id=before.id, queued_admission_claim_id=claim)
        started = e.preflight.store.get(before.id)
        self.assertEqual(started.status, 'started')
        self.assertIsNotNone(started.queued_start_receipt)
        self.assertEqual(started.queued_turn_snapshot, e.snapshot)
        self.assertEqual(sum(method == 'turn/start' for method, _ in e.native.requests), 1)
        self.assertIsNone(started.bootstrap_recovery)

    async def test_retired_or_racing_source_retry_keeps_current_recovery_proof(self):
        e = await self.retired_context()
        before = e.preflight.store.get(e.attempt.id)
        with self.assertRaises(HTTPException) as refused:
            e.preflight.claim_retry(before.id, actor=e.actor)
        self.assertEqual(refused.exception.detail['code'], 'native_preinput_bootstrap_recovery_required')
        self.assertEqual(e.preflight.store.get(before.id), before)
        self.assertTrue(e.recovery.preview(before.id, project=e.project,
            actor=e.actor, thread_id=e.queued.thread_id)['can_create'])

        live = await self.retired_context(retire=False)
        before = live.preflight.store.get(live.attempt.id)
        validate = live.f.service.native_preinput_retry_guard
        def retire_after_review(current):
            guard = validate(current)
            live.workers.cancel_bootstrap(live.source.id, AssignmentCancelRequest(
                expected_fence=live.source.fence, reason='Maintenance after retry review'), actor=live.actor)
            return guard
        live.preflight.native_preinput_retry_validator = retire_after_review
        with self.assertRaises(HTTPException) as raced:
            live.preflight.claim_retry(before.id, actor=live.actor)
        self.assertEqual(raced.exception.detail['code'], 'native_preinput_retry_review_changed')
        self.assertEqual(live.preflight.store.get(before.id), before)
        self.assertTrue(live.recovery.preview(before.id, project=live.project,
            actor=live.actor, thread_id=live.queued.thread_id)['can_create'])

    async def test_same_fence_canonical_renewal_permits_retry_but_writer_expiry_preserves_proof(self):
        e = await self.retired_context(retire=False)
        before = e.preflight.store.get(e.attempt.id)
        validate = e.f.service.native_preinput_retry_guard
        service_actor = e.actor.model_copy(update={
            'identity_id':'retained-original-worker', 'principal_kind':PrincipalKind.SERVICE,
            'assurance':AuthenticationAssurance.SERVICE_TOKEN,
            'service_scopes':('execution-worker:run',)})
        def renew_after_review(current):
            guard = validate(current)
            renewed = e.workers.renew(e.source.assigned_worker_id, e.source.id,
                AssignmentRenewRequest(fence=e.source.fence, lease_token=e.source.lease.lease_token,
                    lease_seconds=3600), actor=service_actor)
            self.assertEqual(renewed.fence, e.source.fence)
            self.assertEqual(renewed.lease.lease_token, e.source.lease.lease_token)
            self.assertGreater(renewed.lease.expires_at, e.source.lease.expires_at)
            return guard
        e.preflight.native_preinput_retry_validator = renew_after_review
        retrying, _, acquired = e.preflight.claim_retry(before.id, actor=e.actor)
        self.assertTrue(acquired)
        self.assertEqual(retrying.status, 'retrying')
        self.assertEqual(retrying.queued_turn_snapshot, before.queued_turn_snapshot)

        expired = await self.retired_context(retire=False)
        before = expired.preflight.store.get(expired.attempt.id)
        # Only the store's clock binding changes: the real owner inspection
        # succeeds, then the raw writer sees expiry without any row mutation.
        with patch('codex_web.storage.execution_preflight.time',
                SimpleNamespace(time=lambda: expired.source.lease.expires_at + 1)):
            with self.assertRaises(HTTPException) as denied:
                expired.preflight.claim_retry(before.id, actor=expired.actor)
        self.assertEqual(denied.exception.detail['code'], 'native_preinput_retry_review_changed')
        self.assertEqual(expired.preflight.store.get(before.id), before)
        self.assertIsNotNone(expired.preflight.recorded_native_preinput_refusal(before))

    async def test_nonfinite_retry_custody_or_writer_clock_never_consumes_proof(self):
        from codex_web.services.queued_native_preinput import same_retry_custody
        e = await self.retired_context(retire=False)
        raw = e.worker_store.assignment(e.source.id).model_dump(mode='json')
        for field in ('updated_at', 'deadline_at', 'acquired_at', 'expires_at', 'renewed_at'):
            for value in (float('inf'), float('-inf'), float('nan'), True, 'not-a-clock'):
                with self.subTest(field=field, value=repr(value)):
                    invalid = copy.deepcopy(raw)
                    target = invalid if field in {'updated_at', 'deadline_at'} else invalid['lease']
                    target[field] = value
                    self.assertFalse(same_retry_custody(raw, invalid))
                    self.assertFalse(same_retry_custody(invalid, raw))
        before = e.preflight.store.get(e.attempt.id)
        for value in (float('inf'), float('nan'), True):
            with self.subTest(writer_clock=repr(value)):
                with patch('codex_web.storage.execution_preflight.time',
                        SimpleNamespace(time=lambda: value)):
                    with self.assertRaises(HTTPException) as denied:
                        e.preflight.claim_retry(before.id, actor=e.actor)
                self.assertEqual(denied.exception.detail['code'], 'native_preinput_retry_review_changed')
                self.assertEqual(e.preflight.store.get(before.id), before)

    async def test_same_context_refused_original_remains_recoverable_after_recipient_retirement(self):
        e = await self.retired_context()
        try:
            preview = e.recovery.preview(e.attempt.id, project=e.project, actor=e.actor,
                thread_id=e.queued.thread_id)
        except HTTPException as exc:
            self.fail(f'Canonical same-original recovery refused after planned retirement: {exc.detail}')
        self.assertTrue(preview['can_create'])
        self.assertEqual(preview['queued_turn_id'], e.queued.id)
        self.assertEqual(preview['snapshot_sha256'], e.snapshot_sha)
        self.assertFalse(preview['native_turn_submitted'])

    def creation(self, e, *, lose_reply=False, during_creation=None):
        async def create(**values):
            values['admission_guard']()
            reserved = e.preflight.store.get(e.attempt.id).bootstrap_recovery
            self.assertIsNotNone(reserved)
            self.assertEqual(values['bootstrap_token'], reserved.bootstrap_id.removeprefix('bootstrap-'))
            self.assertEqual(values['agent_profile_revision'], e.queued.agent_profile_revision)
            self.assertEqual(values['writable_repository_resource_ids'], e.queued.writable_repository_resource_ids)
            target = e.source.model_copy(deep=True, update={
                'id':'fresh-assignment', 'execution_id':reserved.execution_id,
                'execution_workspace_id':'fresh-workspace', 'fence':1,
                'subject':ExecutionSubject(kind='thread_bootstrap', ref=reserved.bootstrap_id),
                'lease':e.source.lease.model_copy(update={'fence':1}),
                'created_at':time.time(), 'updated_at':time.time()})
            e.backend.record_apply(e.worker_store.assignment_namespace,
                upserts={target.id:target.model_dump(mode='json')})
            e.index.upsert(IndexedThread(id='fresh-thread', project_id=e.project.id, name='Fresh scoped recipient'))
            e.bootstraps.bind(bootstrap_id=reserved.bootstrap_id, thread_id='fresh-thread',
                execution_id=reserved.execution_id, assignment_id=target.id,
                execution_workspace_id=target.execution_workspace_id, actor=e.actor)
            if during_creation:
                during_creation()
            if lose_reply:
                raise TimeoutError('controlled reply loss after actual canonical binding commit')
            return {'thread':{'id':'fresh-thread'}}
        e.recovery.threads = SimpleNamespace(create=AsyncMock(side_effect=create))
        return e.recovery.threads.create

    async def recover(self, e):
        return await e.recovery.recover(e.attempt.id, project=e.project, actor=e.actor,
            expected_snapshot_sha256=e.snapshot_sha, reason='Recover the SAME retained Work original')

    async def test_creation_reply_loss_reconciles_once_and_native_start_ack_keep_original_pin(self):
        e = await self.retired_context()
        create = self.creation(e, lose_reply=True)
        with self.assertRaises(TimeoutError):
            await self.recover(e)
        self.assertEqual(e.preflight.store.get(e.attempt.id).bootstrap_recovery.state, 'pending')
        self.assertIsNone(e.index.replacement_thread_id(e.queued.thread_id))
        result = await self.recover(e)
        self.assertEqual(result['threadId'], 'fresh-thread')
        self.assertEqual(create.await_count, 1)
        retained, retry, acquired = e.preflight.claim_retry(e.attempt.id, actor=e.actor)
        self.assertTrue(acquired)
        e.preflight.queue_retry_committed(retained.id, claim_id=retry)
        _, claim = e.preflight.claim_queued_admission(retained.id)
        self.assertTrue(e.preflight.begin_queued_start(retained.id, claim_id=claim))
        e.f.service.validate_native(e.pin, thread_id='fresh-thread', project=e.project,
            work_item_ref=e.queued.work_item_ref, handoff_requested_at=e.queued.handoff_requested_at,
            agent_profile_id=e.queued.agent_profile_id, agent_profile_revision=e.queued.agent_profile_revision,
            agent_profile_actor_id=e.actor.identity_id, writable_repository_resource_ids=('platform',),
            queued_admission_attempt_id=retained.id)
        target = e.worker_store.assignment('fresh-assignment')
        capture = QueuedNativeStartCapture(e.preflight, retained.id, claim,
            e.queued.execution_id, 'fresh-thread', e.actor.identity_id).bind_assignment(target,
                worker_id=target.assigned_worker_id, fence=target.fence,
                validator=lambda: e.worker_store.assignment(target.id))
        correlation = capture.prepare(generation='fresh-runtime', rpc_request_id='exact-rpc',
            native_session_id='fresh-thread')
        # Controlled external native response; the canonical capture validates
        # its exact RPC/assignment/original linkage and projects the START.
        self.assertTrue(capture.observe(correlation, generation='fresh-runtime', message={
            'id':'exact-rpc', 'result':{'thread':{'id':'fresh-thread'},
                'turn':{'id':'actual-test-turn','threadId':'fresh-thread','status':'inProgress'}}}))
        started = e.preflight.store.get(retained.id)
        self.assertEqual(started.status, 'started')
        self.assertEqual(started.queued_turn_snapshot, e.snapshot)
        e.f.service.delivery_recorder = e.f.continuity.record_handoff_delivery
        self.assertTrue(e.f.service.project_native_start(started))
        state = e.f.f.env.states.get(e.queued.work_item_ref)
        self.assertEqual(state.handoff.delivery.thread_id, 'fresh-thread')
        self.assertEqual(state.handoff.requested_at, e.queued.handoff_requested_at)
        e.f.f.env.machine._structured_ack(state.ref, WorkItemAckCreate(actor='orchestrator',
            expected_handoff_requested_at=e.queued.handoff_requested_at))
        accepted = e.f.f.env.states.get(state.ref)
        self.assertEqual(accepted.handoff.status, 'accepted')
        self.assertEqual(e.preflight.store.get(retained.id).queued_turn_snapshot['owner_routing_pin'],
            e.pin.model_dump(mode='json'))
        selected = e.f.service._accepted_owner_recipient(accepted, e.f.f.binding,
            'orchestrator', {'platform'})
        self.assertEqual(selected[0].thread_id, 'fresh-thread')
        current = e.worker_store.assignment(target.id)
        changed = current.model_copy(update={'fence':current.fence+1,
            'lease':current.lease.model_copy(update={'fence':current.fence+1})})
        e.backend.record_apply(e.worker_store.assignment_namespace,
            upserts={changed.id:changed.model_dump(mode='json')})
        from codex_web.owner_routing import OwnerRoutingError
        with self.assertRaises(OwnerRoutingError):
            e.f.service._accepted_owner_recipient(accepted, e.f.f.binding, 'orchestrator', {'platform'})
        # Current custody denial cannot erase the real historical START.
        self.assertEqual(e.preflight.store.get(retained.id).queued_start_receipt, started.queued_start_receipt)

    async def test_raw_error_detail_and_legacy_event_do_not_mint_claim_bound_proof(self):
        e = await self.retired_context(seal=False)
        public = e.preflight.public(e.attempt, actor=e.actor)
        self.assertFalse(public['can_recover_bootstrap'])
        self.assertEqual(public['native_preinput_recovery']['reason'], 'claim_bound_controller_witness_missing')
        from codex_web.services.event_journal import EventJournal
        journal = EventJournal(e.root / 'bot-events.jsonl', clock=lambda: 10)
        journal.append({'type':'queued_turn_retained_for_recovery', 'thread_id':e.queued.thread_id,
            'queued_id':e.queued.id, 'preflight_attempt_id':e.attempt.id, 'known_unstarted':True})
        # A real old journal observation cannot bind a later claim, even when
        # the wall clock is equal/backward and attempt_number is unchanged.
        e.preflight.clock = lambda: 9
        e.preflight.queued_disposition(e.attempt.id, status='queued',
            claim_id=e.attempt.queued_admission_claim_id)
        _, claim = e.preflight.claim_queued_admission(e.attempt.id)
        e.preflight.begin_queued_start(e.attempt.id, claim_id=claim)
        e.preflight.queued_disposition(e.attempt.id, status='blocked', claim_id=claim,
            detail={'code':'native_context_delivery_limit', 'message':'same prose',
                'native_preinput_refusal':e.observation.proof.model_dump(mode='json')}, preinput_refusal=True)
        with self.assertRaises(HTTPException) as denied:
            e.recovery.preview(e.attempt.id, project=e.project, actor=e.actor)
        self.assertEqual(denied.exception.detail['code'], 'bootstrap_recovery_unavailable')

    async def test_stale_renderer_claim_cannot_seal_a_new_claim(self):
        e = await self.retired_context(change_claim=True)
        self.assertIsNone(e.preflight.recorded_native_preinput_refusal(e.attempt))
        with self.assertRaises(HTTPException):
            e.recovery.preview(e.attempt.id, project=e.project, actor=e.actor)

    async def test_source_custody_change_during_creation_keeps_pending_without_route(self):
        e = await self.retired_context()
        def drift():
            current = e.worker_store.assignment(e.source.id)
            e.backend.record_apply(e.worker_store.assignment_namespace,
                upserts={current.id:current.model_copy(update={'fence':current.fence+1}).model_dump(mode='json')})
        create = self.creation(e, during_creation=drift)
        with self.assertRaises(HTTPException):
            await self.recover(e)
        self.assertEqual(create.await_count, 1)
        self.assertIsNone(e.index.replacement_thread_id(e.queued.thread_id))
        self.assertEqual(e.preflight.store.get(e.attempt.id).bootstrap_recovery.state, 'pending')

    async def test_concurrent_recovery_creates_one_physical_generation(self):
        e = await self.retired_context()
        from codex_web.services.coordination import StateStoreCoordinationBackend
        from codex_web.services.replicated_ownership import ReplicatedOwnershipService
        e.recovery.ownership = ReplicatedOwnershipService(StateStoreCoordinationBackend(e.backend),
            instance_id='recovery-controller')
        locks = {}
        e.recovery.queue_lock = lambda name: locks.setdefault(name, asyncio.Lock())
        create = self.creation(e)
        results = await asyncio.gather(self.recover(e), self.recover(e))
        self.assertTrue(all(item['threadId'] == 'fresh-thread' for item in results))
        self.assertEqual(create.await_count, 1)
        self.assertEqual(e.preflight.store.get(e.attempt.id).queued_turn_snapshot, e.snapshot)

    async def test_changed_source_authority_or_native_capture_refuses_before_creation(self):
        e = await self.retired_context()
        original = e.worker_store.assignment(e.source.id)
        changes = ({'created_by':'another-actor'}, {'resource_ids':('another-repository',)},
            {'agent_profile':original.agent_profile.model_copy(update={'profile_revision':99})},
            {'lease':e.source.lease}, {'fence':original.fence+1})
        for change in changes:
            with self.subTest(change=tuple(change)):
                e.backend.record_apply(e.worker_store.assignment_namespace,
                    upserts={original.id:original.model_copy(update=change).model_dump(mode='json')})
                with self.assertRaises(HTTPException):
                    e.recovery.preview(e.attempt.id, project=e.project, actor=e.actor)
        e.backend.record_apply(e.worker_store.assignment_namespace,
            upserts={original.id:original.model_dump(mode='json')})
        # Real canonical capture preparation wins over a stale local refusal;
        # no response or absent START is reinterpreted as nonstart.
        self.creation(e)
        await self.recover(e)
        _, retry, acquired = e.preflight.claim_retry(e.attempt.id, actor=e.actor)
        self.assertTrue(acquired)
        e.preflight.queue_retry_committed(e.attempt.id, claim_id=retry)
        _, claim = e.preflight.claim_queued_admission(e.attempt.id)
        e.preflight.begin_queued_start(e.attempt.id, claim_id=claim)
        target = e.worker_store.assignment('fresh-assignment')
        capture = QueuedNativeStartCapture(e.preflight, e.attempt.id, claim,
            e.queued.execution_id, 'fresh-thread', e.actor.identity_id).bind_assignment(
                target, worker_id=target.assigned_worker_id, fence=target.fence,
                validator=lambda:e.worker_store.assignment(target.id))
        capture.prepare(generation='original-runtime', rpc_request_id='possible-start',
            native_session_id='fresh-thread')
        updated = e.preflight.queued_disposition(e.attempt.id, status='blocked', claim_id=claim,
            detail={'code':'native_context_delivery_limit','message':'same prose'}, preinput_refusal=True,
            native_preinput_observation=e.observation)
        self.assertEqual(updated.status, 'reconciliation_required')
        self.assertIsNone(e.preflight.recorded_native_preinput_refusal(updated))
        with self.assertRaises(HTTPException):
            e.recovery.preview(e.attempt.id, project=e.project, actor=e.actor)

    async def test_work_nonce_change_during_creation_does_not_publish_recovery_edge(self):
        e = await self.retired_context()
        def change_work():
            state = e.f.f.env.states.get(e.queued.work_item_ref)
            state.handoff.requested_at += 1
            e.f.f.env.states.put(state.ref, state)
        create = self.creation(e, during_creation=change_work)
        with self.assertRaises(HTTPException):
            await self.recover(e)
        self.assertEqual(create.await_count, 1)
        self.assertIsNone(e.index.replacement_thread_id(e.queued.thread_id))
        self.assertEqual(e.preflight.store.get(e.attempt.id).bootstrap_recovery.state, 'pending')


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(RetiredNativePrestartOriginalRecoveryTests)
