"""Private canonical completion regression; no model, runtime, or provider call."""
import unittest, time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from tests import test_turn_execution_binding as binding_fixture
from tests import test_action_intents as intent_fixture
from codex_web.agent_profiles import AgentProfileCreate
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.storage.agent_profiles import AgentProfileStore
from codex_web.execution_workspace_backend import LocalGitWorkspaceBackend
from codex_web.execution_workspaces import ExecutionWorkspaceAcquire
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionManager
from codex_web.action_intents import ActionIntent, ActionIntentStatus, ReadinessExecutionOrigin
from codex_web.execution_subjects import ExecutionSubject, ExecutionSubjectKind
from codex_web.execution_workers import AssignmentClaimRequest, AssignmentStartRequest, AssignmentCompleteRequest, ExecutionRuntimeBinding
from codex_web.services.execution_workers import AssignmentReadinessIndexProgressing, AssignmentReadinessOriginError
from codex_web.storage.action_intents import ActionIntentStore, ReadinessOriginIndexProgressing, _ORIGIN_INDEX_EPOCH

class EmptyReasoningCompletionIndexTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  f=binding_fixture.TurnExecutionBindingTests()
  f.setUp();self.addCleanup(f.tearDown)
  f.service._clock=time.time;f.workers.identity=f.identity
  worker_actor=f.identity.bootstrap_service_actor(identity_id=f.worker.service_identity_id,
   name='Private completion worker',scope=f.actor.tenant,service_scopes=('execution-worker:run',))
  self.w=NS(service=f.workers,worker=f.worker,worker_actor=worker_actor,admin=f.actor)
  registry=f.execution_profiles.registry
  authority=install_authority_roles(registry,f.resources)
  profiles=AgentProfileService(AgentProfileStore(f.sqlite),definitions=registry,
   authority=authority,execution_profiles=f.execution_profiles)
  f.execution_profiles.agent_profiles=profiles
  payload=f.execution_profiles.record(organization_id=f.actor.organization_id,workspace_id=f.actor.workspace_id).payload.copy()
  payload['profiles']=[*payload['profiles'],dict(id='reasoning-only',name='Reasoning only',
   description='Private tool-free reasoning',workspace_mode='scratch',repository_access='none',network_enabled=False,
   required_worker_capabilities=['command_execution'],control_plane_operations=[])]
  draft=registry.create_draft(DefinitionDraftCreate(definition_id='execution.profiles.default',
   kind='execution-profile-catalog',definition_schema_version='1.3',scope_type='global',payload=payload,actor=f.actor.identity_id))
  registry.publish(draft.record_id,DefinitionPublishRequest(actor=f.actor.identity_id))
  profile=profiles.create(AgentProfileCreate(profile_id='thinker',name='Private completion thinker',
   execution_profile_id='reasoning-only',sandbox_requirement='read-only'),actor=f.actor)
  f.service.local_session_probe=lambda:True
  f.workspaces.backend=LocalGitWorkspaceBackend(Path(f.temp.name)/'canonical-scratch')
  bound=f.service.prepare_bootstrap(bootstrap_id='private-reasoning-bootstrap',execution_id='private-reasoning-execution',
   project_id='home',sandbox='read-only',approval_policy='never',execution_profile_id='reasoning-only',
   agent_profile=profiles.binding_for(profile),runtime_binding=ExecutionRuntimeBinding(provider_id='openai',runtime_id='codex-reasoning',
    capability_revision=1,sandbox_profiles=('read-only',),authentication_mode='trusted_local_session'))
  self.assignment=f.workers.store.assignment(bound.assignment_id)
  self.ws=NS(service=f.workspaces)
  workspace=f.workspaces.get(self.assignment.execution_workspace_id,f.actor)
  self.assertEqual(workspace.writable_repository_ids,())
  self.assertEqual(self.assignment.execution_profile_id,'reasoning-only')
  self.assertEqual(self.assignment.agent_profile,profiles.binding_for(profile))
  self.assertEqual(tuple(c.value for c in self.assignment.required_capabilities),('command_execution',))
  self.assertFalse(self.assignment.network.enabled)
  seed_fixture=intent_fixture.ActionIntentTests('test_point_updates_and_empty_claim_do_not_read_historical_collections')
  await seed_fixture.asyncSetUp();self.addAsyncCleanup(seed_fixture.asyncTearDown)
  seed=seed_fixture._create()
  self.history=[seed.model_copy(update={'id':f'historical-intent-{i:05d}',
    'idempotency_key':f'historical-key-{i:05d}','status':ActionIntentStatus.SUCCEEDED,
    'execution_id':f'historical-execution-{i:05d}','readiness_origin':None})for i in range(1025)]
  self.intents=ActionIntentStore(self.w.service.store.store)
  def populate(state):state.intents=list(self.history);return state
  self.intents.update(populate)
  claimed=self.w.service.claim(self.w.worker.id,AssignmentClaimRequest(lease_seconds=120),actor=self.w.worker_actor,assignment_id=self.assignment.id)
  self.assertEqual(claimed.id,self.assignment.id)
  self.assignment=self.w.service.start(self.w.worker.id,claimed.id,
   AssignmentStartRequest(lease_token=claimed.lease.lease_token,fence=claimed.fence),actor=self.w.worker_actor)
  self.assertEqual((self.assignment.status.value,self.assignment.resource_ids,self.assignment.secret_refs),('running',(),()))
  self.assertTrue(all(row.readiness_origin is None for row in self.history))
  self.marker='ready-origin-index:1.0:'+_ORIGIN_INDEX_EPOCH
  self.assertIsNone(self.intents.store.record_get(self.intents.records_namespace,self.marker))
  local=LocalExecutionWorkerRuntime.__new__(LocalExecutionWorkerRuntime)
  local.worker_service=self.w.service;local.worker_actor=self.w.worker_actor
  local.control_actor=self.w.admin;local.workspace_service=self.ws.service
  def fenced_current():
   current=self.w.service.store.assignment(self.assignment.id)
   worker=self.w.service.store.worker(self.w.worker.id)
   self.w.service._validate_lease(current,worker,actor=self.w.worker_actor,
    fence=self.assignment.fence,token=self.assignment.lease.lease_token,now=time.time())
   self.assertEqual(current.status.value,'running')
   return current
  self.stop=AsyncMock()
  self.session=NS(worker_id=self.w.worker.id,fence=self.assignment.fence,
   validate_current=fenced_current,stop=self.stop)
  self.manager=AssignmentBoundAgentProcessSessionManager(local,NS(),runtime_factory=None,credential_provider=None)
  self.manager.sessions[self.assignment.id]=self.session
  self.assertEqual(local.checkpoint_assignment(self.assignment.id),())
 def complete(self):
  return self.w.service.complete(self.w.worker.id,self.assignment.id,
   AssignmentCompleteRequest(lease_token=self.assignment.lease.lease_token,fence=self.assignment.fence,succeeded=True),
   actor=self.w.worker_actor)
 async def test_managed_empty_reasoning_completion_prepares_before_sole_completion(self):
  before=self.w.service.store.assignment(self.assignment.id)
  with patch.object(self.w.service,'complete',wraps=self.w.service.complete) as terminal:
   completed=await self.manager.complete(self.assignment.id,succeeded=True)
  self.assertEqual(terminal.call_count,1)
  self.assertEqual(completed.status.value,'succeeded');self.stop.assert_awaited_once()
 async def test_direct_worker_bounded_progress_is_intended_and_retains_origin(self):
  before=self.w.service.store.assignment(self.assignment.id)
  with self.assertRaises(AssignmentReadinessIndexProgressing):self.complete()
  self.assertEqual(self.w.service.store.assignment(self.assignment.id),before)
  self.stop.assert_not_awaited()
 async def test_existing_explicit_preparation_allows_same_original_managed_completion(self):
  with self.assertRaises(ReadinessOriginIndexProgressing):
   self.w.service.prepare_completion_index(self.w.worker.id,actor=self.w.worker_actor)
  self.w.service.prepare_completion_index(self.w.worker.id,actor=self.w.worker_actor)
  self.assertEqual(self.intents.unsettled_readiness_origin(self.assignment.id,prepare=False),())
  completed=await self.manager.complete(self.assignment.id,succeeded=True)
  self.assertEqual(completed.status.value,'succeeded');self.assertIsNone(completed.lease)
  self.stop.assert_awaited_once();self.assertNotIn(self.assignment.id,self.manager.sessions)

 async def test_real_unsettled_origin_still_blocks_final_transition(self):
  origin=ReadinessExecutionOrigin(assignment_id=self.assignment.id,repository='private-origin',
   binding_checksum='a'*64,fence=self.assignment.fence,worker_id=self.w.worker.id,
   execution_profile_definition=self.assignment.execution_profile_definition)
  payload=self.history[0].model_dump(mode='python')
  payload.update(action_id='code-host.pull-request.ready',readiness_origin=origin,
   status=ActionIntentStatus.UNCERTAIN,attempt=1,execution_started_at=time.time(),
   organization_id=self.assignment.organization_id,workspace_id=self.assignment.workspace_id,
   project_id=self.assignment.project_id,execution_id=self.assignment.execution_id)
  intent=ActionIntent.model_validate(payload)
  def replace(state):state.intents=[intent,*self.history[1:]];return state
  self.intents.update(replace)
  before=self.w.service.store.assignment(self.assignment.id)
  with patch.object(self.w.service,'complete',wraps=self.w.service.complete) as terminal:
   with self.assertRaises(AssignmentReadinessOriginError):await self.manager.complete(self.assignment.id,succeeded=True)
   self.assertEqual(terminal.call_count,1)
  self.assertEqual(self.w.service.store.assignment(self.assignment.id),before)
  self.assertEqual(self.intents.unsettled_readiness_origin(self.assignment.id,prepare=False),(intent,))
  self.stop.assert_not_awaited()
