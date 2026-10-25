"""Compose real cancelled-recipient context with canonical planner and allocator."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from tests import test_owner_cancelled_new_handoff as recipients
from tests import test_turn_execution_binding as planners
from tests.thread_bootstrap_composition_harness import build_real_thread_create
from codex_web.resources import ResourceCreate,ResourceType
from codex_web.agent_profiles import AgentProfileCreate,AgentProfileUpdate,AgentProfileExecutionBinding
from codex_web.execution_workers import ExecutionWorkerRegister
from codex_web.agent_providers import AgentProviderUpsert
from codex_web.services.agent_providers import AgentProviderService
from codex_web.storage.agent_providers import AgentProviderStore
from codex_web.services.agent_runtime import AgentRuntimeRegistry
from codex_web.services.agent_routing import AgentRoutingService
from codex_web.services.agent_routing_configuration import install_agent_routing_configuration
from codex_web.services.codex_agent_runtime import CodexAgentRuntimeAdapter

class RecipientAdmissionRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_current_intent_change_retains_canonical_routing_denial(self):
        cancelled=recipients.CancelledNewHandoffRecipientTests();cancelled.setUp();self.addCleanup(cancelled.doCleanups)
        n=cancelled.n;f=build_real_thread_create(self)
        profiles=cancelled.f.f.profiles
        profiles.execution_profiles=f.execution_profiles
        profile=profiles.create(AgentProfileCreate(profile_id='veridataops-james',name='Private James',role_id='james',execution_profile_id='repository-write',sandbox_requirement='danger-full-access'),actor=cancelled.f.f.actor)
        for index in range(5):profile=profiles.update(profile.profile_id,AgentProfileUpdate(description='Private revision '+str(index),reason='Private exactrevision reconstruction'),actor=cancelled.f.f.actor)
        self.assertEqual(profile.revision,6)
        profile_pin=AgentProfileExecutionBinding(profile_id=profile.profile_id,profile_revision=profile.revision,profile_record_id=profile.record_id,role_id='james',role_definition_ref=profile.role_definition_ref,execution_profile_id=profile.execution_profile_id)
        cancelled.f.f.profile=profile
        n.state.handoff.to_agent='james';n.state.handoff.from_agent='orchestrator';n.state.next_owner='james';n.state.current_owner='orchestrator'
        n.binding.route_prefix='James'
        n.binding.sandbox="danger-full-access"
        n.template.sandbox="danger-full-access";n.template.agent_profile=profile_pin
        for assignment in cancelled.f.assignments.values():assignment.sandbox="danger-full-access";assignment.agent_profile=profile_pin
        f.actor=cancelled.f.f.actor
        project=cancelled.f.f.env.env.projects.get(n.state.project_id,scope=f.actor.tenant).model_copy(update={'path':f.project.path})
        f.project=project;f.projects=planners._Projects(project)
        f.transport_host.project=project;f.workspaces.project_lookup=lambda _:project
        f.service.projects=f.projects;f.service.control_actor=f.actor
        f.thread_service.control_actor=f.actor
        f.repository=f.resources.create(ResourceCreate(resource_type=ResourceType.REPOSITORY,name='Actual private recipient repository'),actor=f.actor)
        f.resources.bind_project(project=project,resource_id=f.repository.id,actor=f.actor)
        f.worker=f.workers.register(ExecutionWorkerRegister(service_identity_id='test-local-worker',pool='local',version='private',capabilities=f.worker.capabilities,supported_execution_contract_versions=f.worker.supported_execution_contract_versions),actor=f.actor)
        f.worker_actor=f.worker_actor.model_copy(update={'organization_id':f.actor.organization_id,'workspace_id':f.actor.workspace_id})
        f._publish_secret(scope_id=project.id)
        resource=f.repository.id
        n.template.resource_ids=(resource,)
        n.template.repository_scope=n.template.repository_scope.model_copy(update={"writable_repository_ids":(resource,)})
        n.state.resource_ids=[resource];cancelled.f.f.env.states.put(n.state.ref,n.state)
        for assignment in cancelled.f.assignments.values():
            if 'platform' in assignment.resource_ids:
                assignment.resource_ids=tuple(resource if x=='platform' else x for x in assignment.resource_ids)
                assignment.repository_scope=assignment.repository_scope.model_copy(update={'writable_repository_ids':(resource,)})
        for session in n.store.load().sessions:
            if 'platform' in session.resource_ids:n.store.upsert(session.model_copy(update={'resource_ids':(resource,)}))
        f.thread_service.agent_profiles=cancelled.f.f.profiles
        provider=AgentProviderService(AgentProviderStore(f.sqlite))
        adapter=CodexAgentRuntimeAdapter(f.process_session)
        provider.upsert(AgentProviderUpsert(id='openai',display_name='Private canonical provider eligibility',declared_capabilities=adapter.capabilities,granted_capabilities=adapter.capabilities),actor=f.actor)
        runtimes=AgentRuntimeRegistry();runtimes.register(adapter,capability_revision=1,sandbox_profiles=('danger-full-access',),network_profiles=('brokered-model-egress',))
        install_agent_routing_configuration(f.configuration)
        f.thread_service.routing_service=AgentRoutingService(provider,runtimes,configuration=f.configuration,profiles=profiles)
        # Keep the canonical recipient guard/fence and intercept only successful
        # final response publication into the older routing fixture's store.
        prep=n.routing.recipient_preparer
        f.service.prepare_bootstrap=Mock(wraps=f.service.prepare_bootstrap)
        original=f.thread_service.create
        async def create(**kwargs):
            from codex_web.models import WorkItemProgressUpdate
            # A genuine canonical administrative supersession after initial
            # preparation must refuse, without entering workspace/provider start.
            cancelled.f.f.env.machine._structured_progress(n.state.ref,
                WorkItemProgressUpdate(actor='orchestrator', current_owner='orchestrator',
                    next_owner='orchestrator', current_stage='implementation_active',
                    expected_handoff_requested_at=n.state.handoff.requested_at,
                    next_action='Current independently authorized assessment'))
            return await original(**kwargs)
        prep.threads=SimpleNamespace(create=create)
        result=await cancelled.f.continuity.dispatch_structured_handoff(n.state,source='work-item-handoff')
        self.assertEqual(result,{'status':'owner_routing_blocked',
            'code':'owner_recipient_preparation_changed','dispatched':False})
        f.service.prepare_bootstrap.assert_not_called()
        self.assertEqual(f.started_assignments,[])
        current=cancelled.f.f.env.states.get(n.state.ref)
        self.assertIsNone(current.handoff)
        self.assertEqual(current.next_owner,'orchestrator')
        cancelled.f.fixture.dispatch.assert_not_awaited()
        self.assertIsNone(n.bound)


class RecipientAdmissionRefusalBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture=recipients.CancelledNewHandoffRecipientTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    async def test_unrelated_or_forged_prestart_error_is_not_routing_authority(self):
        from codex_web.owner_routing import OwnerRoutingError
        from codex_web.services.threads import ThreadBootstrapPrestartRefusal
        error=ThreadBootstrapPrestartRefusal(status_code=503,detail={
            'code':'bootstrap_prestart_failed',
            'bootstrap_preparation_stage':'admission_before_reservation',
            'bootstrap_exception_type':'ValueError','provider_start_called':False})
        # Even a closed-looking wrapper and matching error class are not the
        # exception produced by this invocation's actual admission callback.
        async def create(**kwargs):
            raise error from OwnerRoutingError('owner_recipient_preparation_changed')
        self.fixture.n.threads.create.side_effect=create
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
            await self.fixture.prepared()
        self.assertIs(caught.exception,error)

    async def test_unrelated_python_error_and_original_http_refusal_are_unchanged(self):
        from fastapi import HTTPException
        for error in (ValueError('not a typed routing denial'),
                HTTPException(status_code=403,detail={'code':'existing_authority_denied'})):
            self.fixture.n.threads.create.side_effect=error
            with self.assertRaises(type(error)) as caught:
                await self.fixture.prepared()
            self.assertIs(caught.exception,error)

    async def test_post_provider_http_error_never_restores_captured_guard_denial(self):
        from fastapi import HTTPException
        from codex_web.models import WorkItemProgressUpdate
        from codex_web.owner_routing import OwnerRoutingError
        error=HTTPException(status_code=503,detail={'code':'bootstrap_prestart_failed'})
        async def create(**kwargs):
            state=self.fixture.n.state
            self.fixture.f.f.env.machine._structured_progress(state.ref,
                WorkItemProgressUpdate(actor='orchestrator',current_owner='orchestrator',
                    next_owner='orchestrator',current_stage='implementation_active',
                    expected_handoff_requested_at=state.handoff.requested_at))
            try:
                kwargs['admission_guard']()
            except OwnerRoutingError as refusal:
                # ThreadService deliberately strips the typed pre-start wrapper
                # when a provider callback has already run. Retain that boundary.
                raise error from refusal
        self.fixture.n.threads.create.side_effect=create
        with self.assertRaises(HTTPException) as caught:
            await self.fixture.prepared()
        self.assertIs(caught.exception,error)

    async def test_captured_guard_denial_does_not_authorize_a_different_cause(self):
        from codex_web.models import WorkItemProgressUpdate
        from codex_web.owner_routing import OwnerRoutingError
        from codex_web.services.threads import ThreadBootstrapPrestartRefusal
        error=ThreadBootstrapPrestartRefusal(status_code=503,detail={
            'code':'bootstrap_prestart_failed',
            'bootstrap_preparation_stage':'admission_before_reservation',
            'bootstrap_exception_type':'ValueError','provider_start_called':False})
        async def create(**kwargs):
            state=self.fixture.n.state
            self.fixture.f.f.env.machine._structured_progress(state.ref,
                WorkItemProgressUpdate(actor='orchestrator',current_owner='orchestrator',
                    next_owner='orchestrator',current_stage='implementation_active',
                    expected_handoff_requested_at=state.handoff.requested_at))
            try:
                kwargs['admission_guard']()
            except OwnerRoutingError as captured:
                other=OwnerRoutingError(str(captured))
                self.assertIsNot(other,captured)
                raise error from other
        self.fixture.n.threads.create.side_effect=create
        with self.assertRaises(ThreadBootstrapPrestartRefusal) as caught:
            await self.fixture.prepared()
        self.assertIs(caught.exception,error)
