"""Canonical same-head event -> retained native admission -> governed second run.

Private integration only. Provider HTTP, native RPC/model execution and physical
process observations are synthetic external edges. Admission, the process-session
attestor, event normalization/routing, durable queue/capture, broker, publisher,
Evidence derivation and procedure start all execute their production owners.
"""
import asyncio
from dataclasses import replace
import json
import os
import time
from pathlib import Path
import subprocess
from types import SimpleNamespace as NS
import unittest

from tests import test_governed_mr_procedure_runs as governed
from tests import test_turn_execution as turns
from codex_web.action_intents import (ActionDecisionSnapshot, ActionIntent, ActionIntentReceipt,
    ActionIntentRetryPolicy, ActionIntentVerificationReceipt)
from codex_web.action_providers import ActionDefinition, ActionRequest, ActionResult, ActionVerification
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.agent_providers import AgentProviderUpsert
from codex_web.services.agent_runtime import AgentRuntimeRegistry
from codex_web.services.agent_providers import AgentProviderService
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter
from codex_web.storage.agent_providers import AgentProviderStore
from codex_web.local_execution_backend import BubblewrapExecutionBackend
from codex_web.models import (BotBinding, GitLabRoutingSettings, GitLabProjectRoutingSettings,
    Project, WorkItemProgressUpdate)
from codex_web.security import ExecutionSecurityPolicy, SecurityTrustDecision
from codex_web.resources import ResourceUpdate, ResourceProvenance
from codex_web.services.bot_event_dispatch import BotEventDispatchService
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.services.control_plane_broker import AssignmentBoundControlPlaneBroker
from codex_web.services.execution_preflight import ExecutionPreflightService
from codex_web.services.gitlab import GitLabService
from codex_web.services.gitlab_artifact_events import GitLabArtifactEventProjector
from codex_web.services.procedure_discovery import NativeProcedureAdmissionService, ProcedureExecutionCapabilityService
from codex_web.services.turn_queue_policy import TurnQueuePolicy
from codex_web.services.verified_publication_events import VerifiedPublicationEventRouting
from codex_web.services.work_item_dependencies import gitlab_project_issue_ref
from codex_web.services.work_item_state import WorkItemStateMachine
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.execution_preflight import ExecutionPreflightStore
from codex_web.storage.thread_history import ThreadHistoryRepository
from codex_web.storage.turn_queue import TurnQueueRepository


class MREventContinuationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Composition reuses the existing fixture without importing/inheriting
        # its TestCase class into discovery as another exported test suite.
        self.g = governed.GovernedMRProcedureRunTests()
        await self.g.asyncSetUp()
        self.addAsyncCleanup(self.cleanup_fixture)

    async def cleanup_fixture(self):
        while self.g._cleanups:
            callback,args,kwargs=self.g._cleanups.pop()
            value=callback(*args,**kwargs)
            if asyncio.iscoroutine(value):
                await value

    def seed_verified_publication(self):
        """Typed settled history locates the existing Work, never grants a run.

        This is fixture preparation, as in VerifiedPublicationEventTests.pair;
        current observation/provider/action execution below uses real owners.
        """
        g = self.g
        binding = next(b for b in g.intents.execution.registry.list_bindings(g.admin)
            if b.enabled and b.provider_type == 'gitlab')
        state = g.intents.store.load()
        for action, parameters, output in (
            ('code-host.branch.publish', {'branch':'codex/existing-owned', 'head_revision':'a'*40,
                'execution_workspace_id':g.assignment.execution_workspace_id},
                {'branch':'codex/existing-owned', 'head_revision':'a'*40}),
            ('code-host.pull-request.upsert', {'head':'codex/existing-owned', 'base':'main'},
                {'head':'codex/existing-owned', 'change_request_number':33}),
        ):
            intent = ActionIntent(organization_id=g.admin.organization_id, workspace_id=g.admin.workspace_id,
                project_id='project-a', work_item_ref=g.ref, execution_id=g.assignment.execution_id,
                binding_id=binding.id, provider_type='gitlab', provider_instance=binding.provider_instance,
                action_id=action, action_definition=ActionDefinition(action_id=action, title=action),
                request=ActionRequest(action_id=action, organization_id=g.admin.organization_id,
                    workspace_id=g.admin.workspace_id, project_id='project-a', resource_ids=(g.target.id,),
                    parameters=parameters), authority_decision=ActionDecisionSnapshot(),
                policy_decision=ActionDecisionSnapshot(), security_policy=ExecutionSecurityPolicy(),
                security_decision=SecurityTrustDecision(outcome='allow', risk_class='medium',
                    sandbox='workspace-write', network_enabled=False, authority_source='private-prior-history',
                    policy_source='private-prior-history'), resource_ids=(g.target.id,),
                idempotency_key='prior-'+action, timeout_seconds=30, retry_policy=ActionIntentRetryPolicy(),
                status='succeeded', attempt=1, correlation_id='private-prior-history',
                requested_by=g.admin.identity_id, created_at=1, updated_at=2, completed_at=2)
            receipt = ActionIntentReceipt(intent_id=intent.id, attempt=1, provider_type='gitlab',
                provider_instance=binding.provider_instance, action_id=action, idempotency_key=intent.idempotency_key,
                correlation_id='private-prior-provider', result=ActionResult(provider_binding_id=binding.id,
                    action_id=action, status='succeeded', started_at=1, completed_at=2,
                    output={**output, 'resource_id':g.target.id, 'repository':'group/private'}))
            verification = ActionIntentVerificationReceipt(intent_id=intent.id, verified=True,
                provider_verification=ActionVerification(verified=True))
            intent = intent.model_copy(update={'last_receipt_id':receipt.id, 'last_verification_id':verification.id})
            state.intents.append(intent); state.receipts.append(receipt); state.verifications.append(verification)
        g.intents.store.update(lambda _:state)

    async def test_changed_same_head_event_enters_second_run_through_real_queue_and_broker(self):
        g = self.g
        g.target = g.resources.update(g.target.id, ResourceUpdate(provenance=ResourceProvenance(
            provider='gitlab', provider_instance='gitlab.example', external_id='group/private')), actor=g.admin)
        project = Project(id='project-a', name='Private event continuation', path=str(Path(g.temp.name)/'repo'))
        # Keep the same real assignment and fence; add only the external runtime
        # identity required by the existing native transport adapter.
        runtime_binding = ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex', capability_revision=1)
        g.resources.bind_project(project=project, resource_id=g.target.id, actor=g.admin)
        target = g.resources.resolve_repository_target(project, actor=g.admin,
            work_item_resource_ids=(g.target.id,), work_item_ref=g.ref, explicit_repository_id=g.target.id)
        g.workers.store.update_assignment(g.worker.id, g.assignment.id,
            lambda s:s.model_copy(update={'assignments':[a.model_copy(update={'runtime_binding':runtime_binding, 'repository_target':target})
                if a.id==g.assignment.id else a for a in s.assignments]}))
        session_owner = g.broker.validator.__self__
        session_owner.runtime_binding = runtime_binding
        current = g.workers.store.assignment(g.assignment.id)
        session_owner.delegation = session_owner.credential_provider.issue(current,
            worker_id=g.worker.id, fence=current.fence, actor=g.worker_actor)
        # External process/isolation probe only; actual attestor and policy
        # remain unchanged. The outer test itself is physically network-isolated.
        backend = BubblewrapExecutionBackend(executable='/usr/bin/bwrap',
            probe_runner=lambda args, **kw:subprocess.CompletedProcess(args, 0, '', ''))
        session_owner.local_worker.backend = backend
        session_owner._native_preparation_backend = backend
        ready = asyncio.Event(); ready.set()
        # The test process hosts the synthetic model edge; its actual RSS and
        # the actual private workspace are checked by the unchanged owner.
        session_owner.runtime = NS(proc=NS(poll=lambda:None, pid=os.getpid()), ready=ready)
        session_owner.workspace_path = session_owner.local_worker._workspace_path(current)
        session_owner.started_monotonic = time.monotonic()
        session_owner._bound_assignment = current
        await g.broker.stop()
        g.broker = AssignmentBoundControlPlaneBroker(g.service, assignment=current, worker_id=g.worker.id,
            worker_actor=g.worker_actor, fence=current.fence, validator=session_owner._validate_current_authority)
        await g.broker.start()
        session_owner.control_plane_broker = g.broker
        self.assertEqual(session_owner.validate_current().id, current.id)
        runtimes = AgentRuntimeRegistry()
        adapter = CodexAgentRuntimeAdapter(session_owner)
        runtimes.register(adapter, capability_revision=1)
        providers = AgentProviderService(AgentProviderStore(g.sqlite))
        providers.upsert(AgentProviderUpsert(id=adapter.provider_id, display_name='Private native transport',
            declared_capabilities=adapter.capabilities, granted_capabilities=adapter.capabilities,
            health='healthy'), actor=g.admin)
        g.discovery.execution_capabilities = ProcedureExecutionCapabilityService(g.workers,runtimes,providers)
        first = await g.publish('b')
        baseline = await g.start_snapshot(first['procedure_input']['id'])
        for _ in range(2):
            baseline = await g.advance_run(baseline.procedure_checkpoint.id)
        self.assertEqual(baseline.procedure_checkpoint.status, 'completed')
        before = baseline.procedure_checkpoint.model_dump(mode='json')
        self.seed_verified_publication()
        original = g.items.get(g.ref).model_copy(update={'current_owner':'james'})
        g.items.put(g.ref, original)
        current = g.workers.store.assignment(g.assignment.id)
        host, provisioner, sessions, runtime = turns.TurnExecutionStartTests._service(self)
        thread = 'private-existing-mr-owner'
        host._project = lambda _id:project
        host._release_stale_active_turn = lambda *_:False
        host.DATA_DIR = Path(g.temp.name)
        host._load_work_item_states = g.items.load
        host._save_work_item_states = g.items.save
        host._load_projects = lambda:[project]
        host._resource_ids_for_project = lambda *_args, **_kw:[g.target.id]
        host._load_gitlab_routing_settings = lambda:GitLabRoutingSettings(enabled=True,
            projects={'project-a':GitLabProjectRoutingSettings(project_paths=['group/private'],route_agents=['james'])})
        host.GITLAB_API_BASE = 'https://gitlab.example/api/v4'
        host._project_issue_ref = gitlab_project_issue_ref
        host._gitlab_label_names = lambda payload:[]
        host._gitlab_owner_agents = lambda *_:[]
        host._work_item_state = g.items.get
        host._get_work_item_state_record = g.items.get
        host._put_work_item_state_record = lambda state:g.items.put(state.ref,state)
        machine = WorkItemStateMachine(host, store=g.sqlite)
        normalized = machine._structured_progress(g.ref, WorkItemProgressUpdate(
            actor=g.admin.identity_id, current_owner='james', next_owner='james',
            current_stage='implementation_active'))
        self.assertEqual(machine._work_item_split_brain_findings(normalized), [])
        projector = GitLabArtifactEventProjector(host, machine)
        host._upsert_work_item_state_from_gitlab_event = projector.project
        host._work_item_split_brain_findings = machine._work_item_split_brain_findings
        host._coerce_owner = machine._coerce_owner
        runtime.control_actor = g.admin
        runtime.actor_resolver = lambda *_:g.admin
        binding = NS(bootstrap_id='private-existing-bootstrap', thread_id=thread,
            assignment_id=current.id, execution_id=current.execution_id,
            execution_workspace_id=current.execution_workspace_id, initial_turn_pending=False)
        runtime.bootstrap_bindings.get_by_thread = lambda *_args:binding
        provisioner.prepare = provisioner.prepare_bootstrap = lambda **_kw:NS(
            assignment_id=current.id, workspace_id=current.execution_workspace_id, execution_id=current.execution_id)
        sessions.local_worker = session_owner.local_worker
        runtime.session_managers[('openai','codex')] = sessions
        sessions.get = lambda aid:sessions.session if aid==current.id else None
        sessions.session.validate_current = session_owner.validate_current
        sessions.session.status = session_owner.status
        sessions.session.workspace_path = Path(g.workspaces.get(current.execution_workspace_id,g.admin).path)
        runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(g.sqlite))
        runtime.skill_context_resolver = lambda refs,p,objective:g.skills.context_for_refs_scoped(refs,
            organization_id=p.organization_id,workspace_id=p.workspace_id,project_id=p.id,objective=objective)
        admission = NativeProcedureAdmissionService(g.discovery,g.workers,ThreadHistoryRepository(g.sqlite),
            g.admin,g.profiles)
        admission.procedure_execution = g.runner
        admission.native_session_attestor = session_owner.attest_native_provider_preparation
        runtime.procedure_admission = admission
        repository = TurnQueueRepository(g.sqlite,Path(g.temp.name)/'queue.json')
        host._thread_queue_record = repository.get
        host._thread_queue = repository.get
        host._update_thread_queue_record = repository.update
        scheduled = []
        runtime.schedule_queue_drain = scheduled.append
        bot = BotBinding(id='existing-james', provider='slack', external_conversation_id='private-existing',
            thread_id=thread, project_id='project-a', sandbox='workspace-write', approval_policy='on-request',
            created_at=1,updated_at=1)
        dispatch = BotEventDispatchService(projects=NS(get=lambda _:project),
            settings=NS(get=lambda _:NS(model=None,reasoning_effort=None,execution_profile_id='repository-write')),
            targets=NS(event_reply_target=lambda _:None,conversation_target=lambda _:None),
            bindings=NS(upsert=lambda _:None),execution=runtime,
            queue_policy=TurnQueuePolicy(lambda:{thread:repository.get(thread)},get_queue=repository.get),
            recovery=NS(release_stale_active_turn=lambda *_:False),
            resume=NS(is_timeout_error=lambda e:isinstance(e,TimeoutError),is_stale_thread_error=lambda _:False),
            telemetry=NS(append=host._append_bot_event),publish_event=host.hub.publish,
            binding_name=lambda _:'james',agent_profile_resolver=lambda _:(
                NS(profile_id=current.agent_profile.profile_id,revision=current.agent_profile.profile_revision,
                    execution_profile_id=current.execution_profile_id),g.admin))
        host._binding_for_agent = lambda agent,p:bot if agent=='james' and p=='project-a' else None
        host._dispatch_event_to_binding = dispatch.dispatch
        ingestion = CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(g.sqlite)))
        ingress = GitLabService(host,canonical_events=ingestion)
        resolver = VerifiedPublicationEventRouting(g.intents.store,get_state=g.items.get,
            project_resources=lambda *_args,**_kw:[g.target.id],project_lookup=lambda _:project,
            action_registry=g.intents.execution.registry,resources=g.resources)
        ingress.work_items = replace(ingress.work_items,publication_event_target=resolver)
        ingress.operations = replace(ingress.operations,dispatch_scoped_event=dispatch.dispatch)
        # The model/native transport may make only normal assignment-bound
        # requests. It is released AFTER durable START has been observed below.
        model_released = asyncio.Event()
        model_tasks = []
        published = []
        started = []
        request = sessions.session.request
        async def external_model():
            await model_released.wait()
            result = await g.publish('d')
            published.append(result)
            started.append(await g.start_snapshot(result['procedure_input']['id']))
        async def native_rpc(method,params=None):
            result = await request(method,params)
            if method=='turn/start':model_tasks.append(asyncio.create_task(external_model()))
            return result
        sessions.session.request = native_rpc
        # Genuine changed fact at the sole mocked provider HTTP edge, not an
        # observation/request nonce supplied to the canonical input publisher.
        g.mr['head_pipeline'].update(id=10, status='failed')
        payload={'object_kind':'pipeline','project':{'id':42,'path_with_namespace':'group/private'},
            'object_attributes':{'id':10,'iid':10,'status':'failed','ref':'codex/existing-owned',
                'sha':'a'*40,'source_branch':'codex/existing-owned'}}
        class Webhook:
            headers={'X-Gitlab-Event-UUID':'private-changed-ci-event'}
            async def json(self):return payload
        result = await ingress.handle_event(Webhook())
        self.assertTrue(result['accepted'],result)
        queued, = repository.get(thread)
        self.assertEqual(queued.source,'gitlab')
        self.assertEqual(queued.work_item_ref,g.ref)
        self.assertIsNotNone(queued.provider_notification_pin)
        event=ingestion.bus.store.event(queued.provider_notification_pin.event_id)
        self.assertTrue(queued.provider_notification_pin.matches(queued,event))
        self.assertEqual(event.payload['provider_payload']['head_revision'],'a'*40)
        self.assertEqual(scheduled,[thread])
        self.assertEqual(published,[])
        observed_assignment = runtime._assignment_record(current.id)
        self.assertEqual(observed_assignment.agent_profile, current.agent_profile)
        self.assertEqual(observed_assignment.repository_target.mutable_repository_id, g.target.id)
        await runtime.drain_thread_queue(thread)
        retained=runtime.queued_preflight.store.get('preflight-queued-'+queued.id)
        closed = {
            'status':retained.status, 'last_error':retained.last_error,
            'blockers':[{k:v for k,v in b.model_dump(mode='json').items()
                if k in {'code','message','retryable','phase','remediation_route','procedure_discovery'}}
                for b in retained.blockers],
            'telemetry':[{k:v for k,v in e.items() if k in {'type','code','reason','queued_id'}}
                for e in host.events],
        }
        self.assertEqual(retained.status,'started',json.dumps(closed,sort_keys=True))
        self.assertEqual(retained.queued_start_correlation.assignment_id,current.id)
        self.assertEqual(retained.queued_start_correlation.fence,current.fence)
        self.assertIsNotNone(retained.queued_start_receipt)
        self.assertEqual(published,[])
        model_released.set()
        await asyncio.gather(*model_tasks)
        self.assertEqual(len(published),1)
        second, = started
        self.assertNotEqual(second.procedure_checkpoint.id,before['id'])
        self.assertEqual(second.procedure_history[-1].model_dump(mode='json'),before)
        first_bytes = g.content(first['procedure_input']['id'])
        second_bytes = g.content(published[0]['procedure_input']['id'])
        first_values, second_values = json.loads(first_bytes), json.loads(second_bytes)
        self.assertNotEqual(first_bytes, second_bytes)
        for values in (first_values, second_values):
            self.assertEqual(values['pull_request_number'], 33)
            self.assertEqual(values['expected_head_sha'], 'a'*40)
        self.assertNotEqual(first_values['observed_requirements_fingerprint'],
            second_values['observed_requirements_fingerprint'])
        self.assertEqual(second.procedure_checkpoint.definition, baseline.procedure_checkpoint.definition)
        self.assertEqual(second.procedure_checkpoint.input_snapshot_id, published[0]['procedure_input']['id'])
        self.assertEqual(second.procedure_checkpoint.status, 'ready')
        for field in ('id','execution_id','assigned_worker_id','fence','lease','limits','deadline_at'):
            self.assertEqual(getattr(second,field),getattr(current,field))
        self.assertEqual(repository.get(thread),[])
        duplicate=await ingress.handle_event(Webhook())
        self.assertTrue(duplicate.get('ignored'),duplicate)
        self.assertEqual(repository.get(thread),[])
        self.assertEqual(len(model_tasks),1)
        self.assertEqual([m for m,_ in sessions.session.requests],['thread/resume','turn/start'])
        g.runner.models.invoke.assert_not_called()
