"""Private real reasoning-session startup; fake external process/credential ports only."""
import asyncio, time, unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from codex_web.agent_profiles import AgentProfileCreate
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.local_execution_backend import BubblewrapExecutionBackend
from codex_web.services.agent_profiles import AgentProfileService
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.services.native_reasoning_runtime import ReasoningCodexSession
from codex_web.services.codex_worker_session import AssignmentBoundCodexSession
from codex_web.local_execution_backend import LocalExecutionUnavailableError
from codex_web.services.codex_reasoning_policy import validate_reasoning_rpc, NativeReasoningPolicyError
from codex_web.storage.agent_profiles import AgentProfileStore
from tests import test_turn_execution_binding as binding_fixtures
from tests.test_codex_worker_session import _FakeProcess, _FakeDelegationService

class ReasoningColdstartProvisioningTests(unittest.IsolatedAsyncioTestCase):
 async def test_reasoning_start_omits_operational_python_and_validation_slot(self):
  await self.run_session(ReasoningCodexSession, operational=False)

 async def test_operational_session_keeps_python_and_validation_slot(self):
  await self.run_session(AssignmentBoundCodexSession, operational=True)

 async def test_reasoning_still_refuses_unavailable_isolation_before_claim_or_spawn(self):
  await self.run_session(ReasoningCodexSession, operational=False, isolation_ready=False)

 async def test_reasoning_runtime_failure_positively_stops_original_process(self):
  await self.run_session(ReasoningCodexSession, operational=False, runtime_failure=True)

 async def run_session(self, session_type, *, operational, isolation_ready=True, runtime_failure=False):
  f=binding_fixtures.TurnExecutionBindingTests();f.setUp();self.addCleanup(f.tearDown)
  f.service._clock=time.time;f.workers.identity=f.identity
  f.service.local_session_probe=lambda:True  # Same explicit fake authentication port as canonical native bridge fixture.
  actor=f.identity.bootstrap_service_actor(identity_id=f.worker.service_identity_id,name='Private worker',scope=f.actor.tenant,service_scopes=('execution-worker:run',))
  registry=f.execution_profiles.registry
  payload=f.execution_profiles.record(organization_id=f.actor.organization_id,workspace_id=f.actor.workspace_id).payload.copy()
  payload['profiles']=[*payload['profiles'],dict(id='reasoning-only',name='Private reasoning',description='No operational tools',workspace_mode='scratch',repository_access='none',network_enabled=False,required_worker_capabilities=['command_execution'],control_plane_operations=[])]
  record=registry.create_draft(DefinitionDraftCreate(definition_id='execution.profiles.default',kind='execution-profile-catalog',definition_schema_version='1.3',scope_type='global',payload=payload,actor=f.actor.identity_id))
  registry.publish(record.record_id,DefinitionPublishRequest(actor=f.actor.identity_id))
  profiles=AgentProfileService(AgentProfileStore(f.sqlite),definitions=registry,authority=install_authority_roles(registry,f.resources),execution_profiles=f.execution_profiles)
  f.execution_profiles.agent_profiles=profiles
  profile=profiles.create(AgentProfileCreate(profile_id='private-reasoning',name='Private reasoning',execution_profile_id='reasoning-only',sandbox_requirement='read-only'),actor=f.actor)
  binding=ExecutionRuntimeBinding(provider_id='openai',runtime_id='codex-reasoning',capability_revision=1,sandbox_profiles=('read-only',),authentication_mode='trusted_local_session')
  bound=f.service.prepare_bootstrap(bootstrap_id='private-cold-reasoning',execution_id='thread-bootstrap-private-cold-reasoning',project_id=f.project.id,sandbox='read-only',approval_policy='never',execution_profile_id='reasoning-only',agent_profile=profiles.binding_for(profile),runtime_binding=binding)
  backend=BubblewrapExecutionBackend(executable='/usr/bin/bwrap',probe_runner=lambda *a,**k: (_ for _ in ()).throw(AssertionError('no probe subprocess')))
  backend.probe=lambda:NS(ready=isolation_ready,reason='private isolation unavailable' if not isolation_ready else None)
  process=_FakeProcess();spawned=[]
  def spawn(assignment,**kwargs):
   spawned.append({'assignment_id':assignment.id,'environment_keys':sorted(kwargs.get('environment',{})),'argv_count':len(kwargs['argv'])})
   return process
  backend.spawn_interactive=spawn;backend.terminate_process=lambda p:p.terminate()
  registrations=[]
  evidence=NS(content=object(),create_artifact=lambda value,**kw:registrations.append(value)or NS(id='private-registered-slot'))
  local=LocalExecutionWorkerRuntime(f.workers,f.workspaces,backend,worker=f.worker,worker_actor=actor,control_actor=f.actor,artifact_evidence=evidence)
  durations=[];resolve=local.python_environment
  def python_env(assignment):
   start=time.monotonic()
   try:return resolve(assignment)
   finally:durations.append(time.monotonic()-start)
  local.python_environment=python_env
  class Wire:
   def __init__(self,*a,**k):self.proc=process;self.ready=asyncio.Event()
   async def start(self):
    if runtime_failure:raise RuntimeError('private runtime startup failure')
    self.ready.set()
   async def stop(self):self.ready.clear();process.terminate();process.wait()
  credentials=_FakeDelegationService()
  session=session_type(local,NS(),bound.assignment_id,runtime_factory=Wire,credential_provider=credentials,runtime_binding=binding)
  session._start_egress_broker=AsyncMock(return_value=None)
  start=time.monotonic()
  try:
   if not isolation_ready:
    with self.assertRaises(LocalExecutionUnavailableError):await session.start()
    self.assertEqual(f.workers.store.assignment(bound.assignment_id).status.value,'pending')
    self.assertEqual(durations,[]);self.assertEqual(registrations,[]);self.assertEqual(spawned,[])
    return
   if runtime_failure:
    with self.assertRaisesRegex(RuntimeError,'private runtime startup failure'):await session.start()
    self.assertEqual(len(spawned),1);self.assertIsNotNone(process.poll())
    self.assertEqual(durations,[]);self.assertEqual(registrations,[])
    return
   await session.start()
   assignment=f.workers.store.assignment(bound.assignment_id)
   self.assertEqual(assignment.status.value,'running');self.assertGreater(assignment.fence,0)
   self.assertEqual(assignment.resource_ids,());self.assertFalse(assignment.network.enabled)
   self.assertEqual(assignment.execution_profile_id,'reasoning-only')
   if session_type is ReasoningCodexSession:self.assertIs(session._reasoning_launch_backend,backend)
   self.assertEqual(len(spawned),1);self.assertEqual(credentials.use_calls,1)
   self.assertEqual(len(durations),2 if operational else 0)
   self.assertEqual(len(registrations),1 if operational else 0)
   self.assertEqual((session.workspace_path/'.venv/.codex-web-execution-venv').is_file(),operational)
   self.assertEqual('CODEX_WEB_MANAGED_VALIDATION_RUNNER' in spawned[0]['environment_keys'],operational)
   self.assertEqual('VIRTUAL_ENV' in spawned[0]['environment_keys'],operational)
   if operational:self.assertEqual(registrations[0].source,'native-validation-registration')
   for method in ('command/exec','fs/readFile','process/spawn','turn/resume'):
    with self.assertRaises(NativeReasoningPolicyError):validate_reasoning_rpc({'method':method,'params':{}})
   self.last_measurement={'session_start_seconds':time.monotonic()-start,'python_environment_calls':len(durations),'python_environment_seconds':durations,'validation_registrations':len(registrations),'spawn_calls':len(spawned),'operational':operational}

  finally:
   if session.watchdog_task:
    session.watchdog_task.cancel();await asyncio.gather(session.watchdog_task,return_exceptions=True)
   await session.stop()
   session._supervision_executor.shutdown(wait=True,cancel_futures=True)
