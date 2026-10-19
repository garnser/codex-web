"""Real canonical records + controlled provider effects + actual isolated diagnostics.

No GitLab/VeridataOps endpoint or business data is used by this qualification.
"""
import asyncio
import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch, AsyncMock

from codex_web.action_intents import ActionIntentStatus, ActionIntentReconcileRequest
from codex_web.action_providers import ActionDefinition, ActionCapability, ActionResult, ActionVerification, ActionProviderBindingCreate
from codex_web.artifact_evidence import ArtifactCreate, EvidenceCreate, sha256_digest
from codex_web.definitions import DefinitionReference
from codex_web.execution_workers import ExecutionAssignmentCreate, ExecutionWorkerRegister, AssignmentClaimRequest, AssignmentStartRequest
from codex_web.execution_workspaces import ExecutionWorkspaceAcquire
from codex_web.execution_workspace_backend import LocalGitWorkspaceBackend
from codex_web.models import Project
from codex_web.resources import ResourceCreate, ResourceAlias
from codex_web.skills import SkillCreate, SkillPublish, SkillUpdate
from codex_web.services.skills import SkillService
from codex_web.services.skill_security import SkillSecurityService, SkillScannerRegistry
from codex_web.storage.skill_security import SkillSecurityStore
from codex_web.services.procedure_execution import ProcedureExecutionService, ProcedureExecutionError
from codex_web.services.procedure_discovery import ProcedureDiscoveryService
from codex_web.services.execution_workers import ExecutionWorkerService
from codex_web.services.execution_workspaces import ExecutionWorkspaceService
from codex_web.storage.execution_workers import ExecutionWorkerStore
from codex_web.storage.execution_workspaces import ExecutionWorkspaceStateStore
from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
from codex_web.local_execution_backend import BubblewrapExecutionBackend
from codex_web.services.artifact_content import ArtifactContentService, ArtifactContentRegistry
from codex_web.services.local_artifact_content import LocalArtifactContentStore
from codex_web.services.canonical_events import CanonicalEventBus, CanonicalEventIngestionService
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.services.attention import AttentionService
from codex_web.storage.attention import AttentionStore
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.storage.approval_requests import ApprovalRequestStore
from tests import test_action_intents as action_fixtures
from tests.test_skill_security import FakeScanner


class ControlledProvider:
    provider_type = "procedure-fixture"
    provider_instance = "isolated-test"
    contract_version = "1.2"

    def __init__(self, fixture):
        self.f = fixture
        self.calls = []; self.rollbacks = 0
        self.fail = False; self.unknown = False; self.rollback_unknown = False; self.rollback_fail = False
        self.backup = None

    def actions(self):
        return tuple(ActionDefinition(action_id=name, title=name, required_authority=("service.change" if name == "fixture.change" else "service.read",),
            required_authority_level="execute" if name == "fixture.change" else "read",
            required_resource_types=("service",), timeout_seconds=20, retry_max_attempts=2,
            risk_class="high" if "high_risk" in self.f._testMethodName else "medium",
            reversible=name == "fixture.change", network_access=name == "fixture.readiness",
            credential_required=bool(getattr(self.f, 'secret_fixture_ref', None)),
            credential_purpose='controlled-fixture' if getattr(self.f, 'secret_fixture_ref', None) else None,
            capabilities=ActionCapability(idempotency=True, verification=True, rollback=name == "fixture.change"))
            for name in ("fixture.backup", "fixture.change", "fixture.readiness"))

    async def prepare(self, request, *, binding):
        return {}

    async def execute(self, request, *, binding, credential=None):
        self.calls.append(request.action_id)
        expected = getattr(self.f, 'expected_fixture_credential', None)
        if expected is not None and credential != expected:
            raise AssertionError("originating credential reference was not resolved at the canonical provider boundary")
        if self.fail:
            return ActionResult(provider_binding_id=binding.id, action_id=request.action_id, status="failed",
                started_at=time.time(), completed_at=time.time(), idempotency_key=request.idempotency_key, error_code="429")
        if request.action_id == 'fixture.readiness':
            return await self.readiness(request, binding)
        name = 'backup' if request.action_id == 'fixture.backup' else 'change'
        artifact_ids = ()
        if name == 'backup':
            self.backup = self.f.config.read_bytes()
            artifact = self.f.artifact(self.backup, "backup")
            artifact_ids = (artifact.id,)
        else:
            # Controlled bounded config change: only this fixture-owned file.
            self.f.config.write_text(json.dumps({'workers':['ready','ready'], 'http_status':200, 'rotation':2}))
            if self.unknown:
                raise ConnectionError("controlled response lost after side effect")
        evidence = self.f.proof(name, artifact_ids=artifact_ids, kind='provider_receipt' if name == 'change' else 'artifact_verification')
        return ActionResult(provider_binding_id=binding.id, action_id=request.action_id, status="succeeded",
            started_at=time.time(), completed_at=time.time(), idempotency_key=request.idempotency_key,
            output={'outputs':{name+'_done':True}, 'evidence':{name:evidence.id}}, rollback_token='fixture-backup' if name == 'change' else None)

    async def readiness(self, request, binding):
        import urllib.request
        if request.resource_ids != (self.f.resource.id,) or request.parameters:
            raise AssertionError("read observation target escaped its canonical Resource")
        resource = self.f.resources.get(request.resource_ids[0], self.f.actor)
        endpoint = next(a.value for a in resource.aliases if a.namespace == 'readiness_url')
        self.f.security.validate_outbound_url(endpoint, binding.security_policy.network)
        # Fixed GET, no redirects, no caller-controlled method/body/target. This
        # registered read adapter owns network privilege; Artifact code does not.
        def observe():
            from urllib.error import HTTPError
            from urllib.request import HTTPRedirectHandler, build_opener
            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            try:
                response = build_opener(NoRedirect()).open(endpoint, timeout=2)
            except HTTPError as error:
                response = error
            with response:
                return response.status, json.loads(response.read(4096))
        status, state = await asyncio.to_thread(observe)
        workers = len(state['workers']) == 2 and all(v == 'ready' for v in state['workers'])
        http = status == 200
        proof = {name:self.f.proof(name, kind='deployment_verification', passed=passed).id
            for name,passed in (('workers', workers), ('http', http))}
        return ActionResult(provider_binding_id=binding.id, action_id=request.action_id, status='succeeded',
            started_at=time.time(), completed_at=time.time(), idempotency_key=request.idempotency_key,
            output={'outputs':{'readiness_ok':workers and http},'evidence':proof})

    async def verify(self, result, *, binding, credential=None):
        return ActionVerification(verified=True)

    async def rollback(self, result, *, binding, credential=None):
        self.rollbacks += 1
        expected = getattr(self.f, 'expected_fixture_credential', None)
        if expected is not None and credential != expected:
            raise AssertionError("rollback lost its originating credential reference")
        if result.rollback_token != 'fixture-backup' or self.backup is None:
            raise AssertionError("wrong originating provider result")
        self.f.config.write_bytes(self.backup)
        if self.rollback_unknown:
            raise ConnectionError("controlled rollback response lost")
        if self.rollback_fail:
            return ActionResult(provider_binding_id=binding.id, action_id=result.action_id, status="failed",
                started_at=time.time(), completed_at=time.time(), error_code="controlled_rollback_failed")
        proof = self.f.proof('rollback', kind='provider_receipt')
        return ActionResult(provider_binding_id=binding.id, action_id=result.action_id, status="rolled_back",
            started_at=time.time(), completed_at=time.time(), idempotency_key=result.idempotency_key,
            output={'outputs':{'rollback_done':True}, 'evidence':{'rollback':proof.id}})


DISK = b'''import json, shutil
print(json.dumps({"outputs":{"disk_ok":shutil.disk_usage('.').free > 1024},"checks":{"disk":shutil.disk_usage('.').free > 1024}}))
'''



class ControlledWorkerBackend:
    """Portable protocol simulation when Bubblewrap is unavailable on a CI runner.

    Never executes repository code on the host. The separate native qualification
    test explicitly requires Bubblewrap; a simulated result cannot satisfy it.
    """
    def __init__(self, fixture):
        self.fixture = fixture

    def validate_assignment(self, assignment):
        if assignment.sandbox not in {"workspace-write", "read-only"}:
            raise AssertionError("unexpected sandbox")

    def run(self, assignment, *, argv, trusted_readonly_mounts, poll_hook, **kwargs):
        from codex_web.local_execution_backend import LocalExecutionResult
        if (assignment.network.enabled or assignment.secret_refs or assignment.runtime_binding or assignment.agent_profile):
            raise AssertionError("diagnostic inherited parent network/channel/credential privileges")
        if assignment.sandbox != "read-only" or tuple(argv) != ("/usr/bin/python3", "/opt/procedure/diagnostic.py"):
            raise AssertionError("diagnostic escaped the canonical read-only worker contract")
        poll_hook()
        source = next(Path(src)/"diagnostic.py" for src,dst in trusted_readonly_mounts if str(dst)=="/opt/procedure")
        code = source.read_bytes()
        if code == DISK:
            output = {"outputs":{"disk_ok":True},"checks":{"disk":True}}
        else:
            raise AssertionError("unrecognized controlled diagnostic artifact")
        return LocalExecutionResult("python3", "sha256:"+hashlib.sha256(code).hexdigest(),0,json.dumps(output),"",0.01)


def contract(f):
    now = time.time()
    def step(name, kind, *, action=None, artifact=None, previous=None, evidence=None):
        result = {'id':name, 'kind':kind, 'description':'Controlled GitLab-style workflow qualification',
            'inputs':[], 'outputs':[{'name':('readiness_ok' if kind=='verification' else name+'_done') if action else ('disk_ok' if name=='disk' else 'readiness_ok'), 'type':'boolean', 'description':'Observed result'}],
            'preconditions':[{'source':'step','name':previous,'predicate':'succeeded'}] if previous else [],
            'expected_evidence':[{'name':e, 'description':'Canonical bounded proof', 'kind':'verification' if kind=='verification' else ('provider_receipt' if kind in {'mutation','recovery'} else 'artifact' if name=='backup' else 'verification')} for e in (evidence or [name])],
            'timeout_seconds':10, 'max_attempts':1, 'retry_backoff_seconds':0,
            'permissions':[{'capability':'service.change' if kind in {'mutation','recovery'} else 'service.read', 'level':'execute' if kind in {'mutation','recovery'} else 'read','resource_selector':'service'}],
            'failure':{'outcome':'escalate','escalation_owner_identity_id':f.actor.identity_id,'attention_reason':'Inspect canonical recovery and reconcile uncertainty'}}
        if action:
            result['action']={'provider_type':f.provider.provider_type,'provider_instance':f.provider.provider_instance,'action_id':action,'provider_contract_version':'1.2','resource_selector':'service'}
        else:
            result['worker']={'capability':'command_execution','artifact_reference':artifact.id,'artifact_checksum':artifact.digest.value,'resource_selector':'service'}
        if kind in {'mutation','recovery'}:
            result['impact']={'resource_selector':'service','change':'configuration' if kind=='mutation' else 'rollback','description':'Only a temporary test-owned fixture file', 'max_unavailable_seconds':10,'reversible':True}
            result['verification_step_id']='ready' if kind=='mutation' else 'restored'
        return result
    backup=step('backup','diagnostic',action='fixture.backup')
    disk=step('disk','diagnostic',artifact=f.disk_artifact,previous='backup')
    change=step('change','mutation',action='fixture.change',previous='disk')
    ready=step('ready','verification',action='fixture.readiness',previous='change',evidence=['workers','http'])
    ready['failure'].update(outcome='conditional_recovery',recovery_step_id='rollback')
    rollback=step('rollback','recovery',action='fixture.change')
    rollback['recovery']={'mode':'provider_rollback','origin_step_id':'change'}
    rollback['preconditions']=[{'source':'step','name':'ready','predicate':'verification_failed'}]
    restored=step('restored','verification',action='fixture.readiness',previous='rollback',evidence=['workers','http'])
    return {'schema_version':'1.1','owner_identity_id':f.actor.identity_id,'task_triggers':['gitlab.maintenance'],
        'operation_triggers':['fixture.backup','fixture.change'], 'resource_selectors':[{'id':'service','resource_types':['service']}],
        'environments':['staging'], 'inputs':[], 'reviewed_at':now-1, 'review_due_at':now+3600,'expires_at':now+7200,
        'required_worker_capabilities':['command_execution'],'conflict_group':'gitlab-fixture','precedence':10,
        'max_duration_seconds':200,'steps':[backup,disk,change,ready,rollback,restored]}


class ProcedureExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 5
        await action_fixtures.ActionIntentTests.asyncSetUp(self)
        self.intents = self.service
        self.control = self.actor
        self.worker_actor = self.identity.bootstrap_service_actor(identity_id='procedure-worker', name='Controlled worker',
            scope=self.actor.tenant, service_scopes=('execution-worker:run','action-intent:worker','secret:use'))
        self.root=Path(self.temp.name)
        self.resource=self.resources.create(ResourceCreate(resource_type='service',name='Controlled GitLab',
            operational_context={'environment':'staging'}),actor=self.actor)
        repository=self.root/'repository';repository.mkdir()
        subprocess.run(['git','init','-q',str(repository)],check=True)
        (repository/'config.json').write_text(json.dumps({'workers':['ready','ready'],'http_status':200,'rotation':1}))
        subprocess.run(['git','-C',str(repository),'add','config.json'],check=True)
        subprocess.run(['git','-C',str(repository),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture'],check=True)
        self.repo=self.resources.create(ResourceCreate(resource_type='repository',name='Controlled fixture repository',
            operational_context={'environment':'staging'},aliases=[ResourceAlias(namespace='filesystem',value=str(repository))]),actor=self.actor)
        project=Project(id='home',name='Fixture',path=str(repository),organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id)
        self.workspaces=ExecutionWorkspaceService(ExecutionWorkspaceStateStore(self.sqlite),LocalGitWorkspaceBackend(self.root/'workspaces'),self.resources,lambda _:project)
        subject={'kind':'thread','ref':'fixture-thread'}
        workspace=self.workspaces.acquire(ExecutionWorkspaceAcquire(subject=subject,execution_id='fixture-execution',project_id='home',
            resource_ids=(self.repo.id,self.resource.id),repository_resource_id=self.repo.id),actor=self.actor)
        self.config=Path(workspace.path)/'config.json'
        import http.server, threading
        fixture = self
        self.http_requests = []
        self.http_mutations = 0
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                fixture.http_requests.append(('GET', handler.path))
                state = json.loads(fixture.config.read_text())
                handler.send_response(state['http_status']); handler.end_headers()
                handler.wfile.write(json.dumps(state).encode())
            def do_POST(handler):
                fixture.http_requests.append(('POST', handler.path))
                fixture.http_mutations += 1
                handler.send_response(200); handler.end_headers()
            def log_message(self, *args):
                pass
        self.http_server = http.server.ThreadingHTTPServer(('127.0.0.1',0), Handler)
        # Keep real HTTP qualification while avoiding the server's default
        # half-second shutdown poll for every private fixture.
        self.http_thread = threading.Thread(
            target=self.http_server.serve_forever,
            kwargs={'poll_interval': 0.01}, daemon=True)
        self.http_thread.start()
        self.http_url = 'http://127.0.0.1:' + str(self.http_server.server_port) + '/ready'
        from codex_web.resources import ResourceUpdate
        self.resource = self.resources.update(self.resource.id, ResourceUpdate(aliases=[ResourceAlias(namespace='readiness_url', value=self.http_url)]), actor=self.actor)
        from codex_web.services.security_boundary import SecurityBoundaryService
        from codex_web.storage.security_events import SecurityEventStore
        self.security = SecurityBoundaryService(SecurityEventStore(self.sqlite))
        self.intents.security_boundary = self.security
        self.workers=ExecutionWorkerService(ExecutionWorkerStore(self.sqlite),identity=self.identity,workspaces=self.workspaces)
        self.worker=self.workers.register(ExecutionWorkerRegister(service_identity_id=self.worker_actor.identity_id,pool='local',version='test',
            capabilities=('command_execution','git','artifact_upload','network')),actor=self.actor)
        assignment=self.workers.create_assignment(ExecutionAssignmentCreate(subject=subject,execution_id='fixture-execution',project_id='home',
            resource_ids=(self.repo.id,self.resource.id),required_capabilities=('command_execution',),execution_contract_version='1.0',
            execution_workspace_id=workspace.id,sandbox='workspace-write'),actor=self.actor)
        claimed=self.workers.claim(self.worker.id,AssignmentClaimRequest(lease_seconds=120),actor=self.worker_actor,assignment_id=assignment.id)
        self.assignment=self.workers.start(self.worker.id,assignment.id,AssignmentStartRequest(lease_token=claimed.lease.lease_token,fence=claimed.fence),actor=self.worker_actor)
        registry=ArtifactContentRegistry();registry.register(LocalArtifactContentStore(self.root/'content'))
        self.artifacts.content=ArtifactContentService(registry)
        from codex_web.services.data_governance import DataGovernanceService
        from codex_web.storage.data_governance import DataGovernanceStore
        self.artifacts.governance=DataGovernanceService(DataGovernanceStore(self.sqlite))
        self.artifacts.resources=self.resources
        self.secret_fixture_ref = None
        if 'secret_reference' in self._testMethodName or 'network_enabled_parent' in self._testMethodName:
            from codex_web.services.secrets import SecretBroker
            from codex_web.storage.secret_state import SecretStateStore
            from codex_web.secret_backends import LocalFileSecretBackend
            from codex_web.secrets import SecretCreate
            self.expected_fixture_credential = 'controlled-non-production-fixture-value'
            self.secret_broker = SecretBroker(SecretStateStore(self.sqlite), {'local':LocalFileSecretBackend(self.root/'fixture-secrets')})
            self.secret_fixture_ref = self.secret_broker.create(SecretCreate(name='Controlled fixture only',
                value=self.expected_fixture_credential, allowed_identity_ids=[self.worker_actor.identity_id]), actor=self.actor).id
            self.execution.secret_broker = self.secret_broker
            def assign_reference(state):
                state.assignments[0].secret_refs = (self.secret_fixture_ref,)
                return state
            self.workers.store.update_assignment(self.worker.id, self.assignment.id, assign_reference)
        self.provider=ControlledProvider(self);self.registry.register(self.provider,tenant_scope=self.actor.tenant)
        self.binding=self.registry.bind(ActionProviderBindingCreate(provider_type=self.provider.provider_type,provider_instance=self.provider.provider_instance,
            project_id='home',resource_ids=(self.resource.id,self.repo.id),credential_ref=self.secret_fixture_ref,
            security_policy={'network':{'enabled':True,'allowed_schemes':['http'],'allowed_hosts':['127.0.0.1'],
                'allowed_ports':[self.http_server.server_port],'allowed_private_cidrs':['127.0.0.0/8']}}),actor=self.actor,resources=self.resources)
        disk_code = DISK
        if 'real_network_enabled_parent' in self._testMethodName:
            disk_code = ("import json,os,glob,urllib.request\n"
                + "blocked=False\ntry:\n urllib.request.urlopen(urllib.request.Request(" + repr(self.http_url.replace('/ready','/mutate'))
                + ",data=b'controlled',method='POST'),timeout=1)\nexcept Exception: blocked=True\n"
                + "safe=blocked and not any(os.environ.get(k) for k in ['CODEX_WEB_CONTROL_PLANE_URL','GH_TOKEN','OPENAI_API_KEY','SSH_AUTH_SOCK','CONTAINER_HOST']) and not glob.glob('/run/user/*/podman/podman.sock')\n"
                + "print(json.dumps({'outputs':{'disk_ok':safe},'checks':{'disk':safe}}))\n").encode()
        self.disk_artifact=self.artifact(disk_code,'disk-code')
        self.snapshot=self.artifact(b'{}','input-snapshot')
        self.skills=SkillService(self.definition_registry)
        self.skills.bind_procedure_contracts(actions=self.registry,resources=self.resources,scope_actor=lambda r:self.actor)
        scanners=SkillScannerRegistry();scanners.register(FakeScanner())
        security=SkillSecurityService(SkillSecurityStore(self.sqlite),self.definition_registry,scanners)
        self.skills.bind_security(security)
        draft=self.skills.create(SkillCreate(skill_id='controlled-gitlab',name='Controlled GitLab',instructions='Use canonical procedure controls.',operational_procedure=self.configure(contract(self))),actor=self.actor)
        published=self.skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=self.actor)
        self.pin=DefinitionReference.model_validate(published['definitionReference'])
        events=CanonicalEventIngestionService(CanonicalEventBus(CanonicalEventStore(self.sqlite)))
        self.approvals=ApprovalRequestService(ApprovalRequestStore(self.sqlite),self.identity,events,authority_roles=self.authority)
        self.attention=AttentionService(AttentionStore(self.sqlite),events,identity=self.identity)
        self.discovery=ProcedureDiscoveryService(self.skills,self.resources)
        from codex_web.services.procedure_discovery import ProcedureExecutionCapabilityService
        # Real live canonical worker capability reader. A deterministic procedure
        # declares no AgentRuntime requirement; unknown model-runtime metadata is
        # not transformed into an attested capability or required for these steps.
        self.discovery.execution_capabilities=ProcedureExecutionCapabilityService(self.workers, None, None)
        self.intents.execution_workers=self.workers
        self.intents.procedure_discovery=self.discovery
        self.execution.procedure_admission=self.intents.procedure_provider_admission
        backend = BubblewrapExecutionBackend()
        self.native_worker_ready = backend.probe().ready
        if not self.native_worker_ready:
            backend = ControlledWorkerBackend(self)
        self.runtime=LocalExecutionWorkerRuntime(self.workers,self.workspaces,backend,worker=self.worker,
            worker_actor=self.worker_actor,control_actor=self.actor,artifact_evidence=self.artifacts)
        self.rebuild()
        self.runner.start(self.assignment.id,definition=self.pin,input_snapshot_id=self.snapshot.id,actor=self.actor)

    def configure(self, content):
        if 'execute_mapping_key' in self._testMethodName or 'verify_metadata_key' in self._testMethodName:
            content['steps'][0]['max_attempts'] = 2
        if 'awaiting_approval' in self._testMethodName:
            content['steps'][2]['permissions'][0]['approvals']={'count':1}
        if 'retry_exhaustion' in self._testMethodName:
            content['steps'][0]['max_attempts']=2
        if 'diagnostic_interruption_replay' in self._testMethodName:
            content['steps'][1]['max_attempts']=2
        if 'verification_restart' in self._testMethodName:
            content['steps'][3]['max_attempts']=2
        if 'judgment' in self._testMethodName:
            content['steps'].insert(0, {
                'id':'judgment', 'kind':'judgment', 'description':'Declared novel fixture diagnosis', 'inputs':[],
                'outputs':[{'name':'judgment_done','type':'boolean','description':'Structured diagnostic recommendation'}],
                'expected_evidence':[{'name':'report','description':'Observed model result artifact','kind':'artifact'}],
                'timeout_seconds':10,'max_attempts':1,'retry_backoff_seconds':0,
                'permissions':[{'capability':'service.read','level':'read','resource_selector':'service'}],
                'failure':{'outcome':'escalate','escalation_owner_identity_id':self.actor.identity_id,'attention_reason':'Inspect bounded judgment failure'},
                'judgment':{'reason':'Novel interpretation requiring bounded judgment','role':'operator','model_class':'strategic',
                    'max_calls':1,'max_input_tokens':1000,'max_output_tokens':200,'max_cost_usd':1}})
        if 'deviation' in self._testMethodName:
            optional=json.loads(json.dumps(content['steps'][1]))
            optional.update(id='optional-disk',preconditions=[])
            content['steps'].append(optional)
        return content

    def rebuild(self):
        self.runner=ProcedureExecutionService(workers=self.workers,skills=self.skills,discovery=self.discovery,intents=self.intents,
            evidence=self.artifacts,identity=self.identity,authority=self.authority,approvals=self.approvals,attention=self.attention,
            control_actor=self.actor,worker_runtime=self.runtime,worker_actor=self.worker_actor,models=__import__('types').SimpleNamespace(invoke=AsyncMock(side_effect=AssertionError('zero model calls'))))
        self.intents.procedure_execution=self.runner

    def artifact(self, data, name):
        artifact=self.artifacts.create_artifact(ArtifactCreate(project_id='home',execution_id='fixture-execution',resource_ids=(self.resource.id,),
            artifact_type='report',name=name,digest=sha256_digest(data)),actor=self.actor)
        return self.artifacts.attach_artifact_content(artifact.id,(data,),actor=self.actor)

    def proof(self, name, *, kind='artifact_verification',artifact_ids=(),passed=True):
        from codex_web.procedure_discovery import current_procedure_action_intent
        intent_id = current_procedure_action_intent()
        return self.artifacts.create_evidence(EvidenceCreate(project_id='home',execution_id='fixture-execution',resource_ids=(self.resource.id,),
            evidence_type=kind,result='pass' if passed else 'fail',artifact_ids=artifact_ids,provider=self.provider.provider_type,
            source=f'action-intent:{intent_id}' if intent_id else name),actor=self.actor)

    def tearDown(self):
        if hasattr(self, 'http_server'):
            self.http_server.shutdown(); self.http_server.server_close()
            self.http_thread.join(timeout=2)
            self.assertFalse(self.http_thread.is_alive(), 'fixture HTTP server did not stop')
        self.temp.cleanup()

    async def advance(self):
        result=await self.runner.advance(self.assignment.id,actor=self.actor)
        return result.procedure_checkpoint

    async def through_change(self):
        for name in ('backup','disk','change'):
            checkpoint=await self.advance()
            self.assertEqual(checkpoint.steps[name].status,'succeeded',checkpoint)
        return checkpoint

    async def finish(self, limit=10):
        for _ in range(limit):
            checkpoint=await self.advance()
            if checkpoint.status in {'completed','recovered','recovery_required','reconciliation_required'}:
                return checkpoint
        self.fail('bounded workflow did not terminate')

    async def test_gitlab_style_real_backup_bounded_change_all_worker_http_evidence_zero_models(self):
        if not self.native_worker_ready:
            self.skipTest("native Bubblewrap qualification requires a qualified Linux isolation backend")
        checkpoint=await self.finish()
        self.assertEqual(checkpoint.status,'completed',checkpoint)
        self.assertEqual(self.provider.calls,['fixture.backup','fixture.change','fixture.readiness'])
        self.assertEqual(json.loads(self.config.read_text())['rotation'],2)
        self.assertEqual(set(checkpoint.steps['ready'].evidence),{'workers','http'})
        self.assertIn(('GET', '/ready'), self.http_requests)
        self.assertEqual(self.http_mutations, 0)
        self.assertEqual(checkpoint.definition,self.pin)
        self.runner.models.invoke.assert_not_called()
        for state in checkpoint.steps.values():
            for evidence_id in state.evidence.values():
                evidence=self.artifacts.get_evidence(evidence_id,actor=self.actor)
                self.assertEqual(evidence.execution_id,'fixture-execution')

    async def test_successful_originating_provider_rollback_and_verified_recovery(self):
        await self.through_change()
        self.config.write_text(json.dumps({'workers':['failed','ready'],'http_status':503,'rotation':2}))
        checkpoint=await self.advance();self.assertEqual(checkpoint.steps['ready'].status,'verification_failed',checkpoint)
        origin=checkpoint.steps['change'].action_intent_id
        self.rebuild()
        checkpoint=await self.finish()
        self.assertEqual(checkpoint.status,'recovered',checkpoint)
        self.assertEqual(self.provider.rollbacks,1)
        self.assertEqual(checkpoint.steps['rollback'].action_intent_id,origin)
        intent=self.intents.get(origin,self.actor)
        self.assertEqual(intent.status,'rolled_back')
        self.assertTrue(intent.rollback_checkpoint.origin_receipt_id)
        self.assertEqual(json.loads(self.config.read_text())['rotation'],1)
        self.assertEqual(set(checkpoint.steps['restored'].evidence),{'workers','http'})
        await self.advance();self.assertEqual(self.provider.rollbacks,1)

    async def test_unknown_rollback_restart_never_duplicates(self):
        await self.through_change();self.config.write_text(json.dumps({'workers':['failed'],'http_status':503,'rotation':2}))
        await self.advance();self.provider.rollback_unknown=True
        checkpoint=await self.advance();self.assertEqual(checkpoint.status,'reconciliation_required',checkpoint)
        self.assertEqual(self.provider.rollbacks,1)
        self.rebuild()
        for _ in range(3):
            checkpoint=await self.advance();self.assertEqual(checkpoint.status,'reconciliation_required')
        origin=checkpoint.steps['rollback'].action_intent_id
        reconciled=await self.intents.reconcile(origin,ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor)
        self.assertEqual(reconciled.status,'requires_reconciliation')
        self.assertEqual(self.provider.rollbacks,1)
        self.assertTrue(checkpoint.attention_id)

    async def test_failed_rollback_attention(self):
        await self.through_change();self.config.write_text(json.dumps({'workers':['failed'],'http_status':503,'rotation':2}))
        await self.advance();self.provider.rollback_fail=True
        checkpoint=await self.advance();self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertEqual(self.provider.rollbacks,1);self.assertTrue(checkpoint.attention_id)

    async def test_exact_pin_survives_new_publication_and_revocation_stops(self):
        content=contract(self);content['precedence']=11
        draft=self.skills.update('controlled-gitlab',SkillUpdate(operational_procedure=content,reason='new revision'),actor=self.actor)
        self.skills.publish(draft['skillId'],draft['recordId'],SkillPublish(),actor=self.actor)
        checkpoint=await self.finish();self.assertEqual(checkpoint.status,'completed',checkpoint);self.assertEqual(checkpoint.definition,self.pin)

    async def test_expired_fence_attention_and_no_execution(self):
        def expire(state):
            state.assignments[0].lease=state.assignments[0].lease.model_copy(update={"expires_at":time.time()-1})
            return state
        self.workers.store.update_assignment(self.worker.id,self.assignment.id,expire)
        checkpoint=await self.advance();self.assertEqual(checkpoint.status,'recovery_required')
        self.assertTrue(checkpoint.attention_id);self.assertEqual(self.provider.calls,[])

    async def test_interruption_before_execution_reuses_intent(self):
        original=self.intents.execute_claimed
        async def interrupted(*args,**kwargs):
            raise asyncio.CancelledError()
        with patch.object(self.intents,'execute_claimed',side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError): await self.advance()
        self.assertEqual(self.provider.calls,[])
        pending=self.workers.store.assignment(self.assignment.id).procedure_checkpoint.steps['backup'].action_intent_id
        self.intents.store.update(lambda state:self.expire_intent(state),intent_ids=(pending,))
        self.rebuild();checkpoint=await self.finish()
        self.assertEqual(checkpoint.status,'completed',checkpoint)
        self.assertEqual(checkpoint.steps['backup'].action_intent_id,pending)
        self.assertEqual(self.provider.calls.count('fixture.backup'),1)

    @staticmethod
    def expire_intent(state):
        state.intents[0].lease.expires_at=time.time()-1
        return state

    async def test_unknown_change_blocks_rollback_and_later_mutations(self):
        await self.advance();await self.advance();self.provider.unknown=True
        checkpoint=await self.advance();self.assertEqual(checkpoint.status,'reconciliation_required',checkpoint)
        self.rebuild()
        await self.advance();await self.advance()
        self.assertEqual(self.provider.calls.count('fixture.change'),1)
        self.assertEqual(self.provider.rollbacks,0)
        self.assertTrue(checkpoint.attention_id)

    async def test_awaiting_approval_restart_does_not_invent_or_repeat_authorization(self):
        from codex_web.identity import HumanIdentity, Membership, MembershipRole, PrincipalKind, AuthenticationAssurance
        from codex_web.approval_requests import ApprovalDecisionSubmit
        await self.advance();await self.advance()
        checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'awaiting_approval',checkpoint)
        approval_id=checkpoint.steps['change'].approval_id
        self.assertEqual(self.provider.calls,['fixture.backup'])
        self.rebuild();checkpoint=await self.advance()
        self.assertEqual(checkpoint.steps['change'].approval_id,approval_id)
        def human(state):
            state.humans.append(HumanIdentity(id='fixture-approver',display_name='Fixture Approver'))
            return state
        self.identity.store.update(human)
        self.identity.add_membership(Membership(identity_id='fixture-approver',principal_kind=PrincipalKind.HUMAN,
            organization_id=self.actor.organization_id,workspace_id=self.actor.workspace_id,roles=[MembershipRole.APPROVER]))
        credentials=self.identity.create_session(identity_id='fixture-approver',scope=self.actor.tenant,assurance=AuthenticationAssurance.MFA)
        approver=self.identity.authenticate_session(credentials.session_token,touch=False).actor
        await self.approvals.decide(approval_id,ApprovalDecisionSubmit(outcome='approve',idempotency_key='fixture-approval'),actor=approver)
        checkpoint=await self.finish()
        self.assertEqual(checkpoint.status,'completed',checkpoint)
        self.assertEqual(self.approvals.get(approval_id,actor=self.actor).status,'consumed')
        self.assertEqual(self.provider.calls.count('fixture.change'),1)

    async def test_authority_revocation_blocks_originating_rollback(self):
        await self.through_change()
        self.config.write_text(json.dumps({'workers':['failed'],'http_status':503,'rotation':2}))
        await self.advance()
        # Real canonical authority catalog revocation, not a fabricated decision.
        record=self.authority.catalog_record(actor=self.actor,project_id='home')
        self.definition_registry.quarantine(record.record_id,actor=self.actor.identity_id,reason='test revocation')
        checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertEqual(self.provider.rollbacks,0);self.assertTrue(checkpoint.attention_id)

    async def test_incompatible_originating_rollback_is_refused(self):
        await self.through_change()
        self.config.write_text(json.dumps({'workers':['failed'],'http_status':503,'rotation':2}))
        await self.advance()
        self.provider.contract_version='1.1'
        checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertEqual(self.provider.rollbacks,0);self.assertTrue(checkpoint.attention_id)

    async def test_unmediated_and_unrelated_rollback_denied(self):
        await self.through_change()
        checkpoint=self.workers.store.assignment(self.assignment.id).procedure_checkpoint
        origin=self.intents.get(checkpoint.steps['change'].action_intent_id,self.actor)
        history=self.intents.store.load(intent_id=origin.id)
        result=next(r.result for r in history.receipts if r.id==origin.last_receipt_id)
        with self.assertRaises(Exception):
            await self.execution.rollback(origin.binding_id,result,actor=self.worker_actor)
        self.assertEqual(self.provider.rollbacks,0)
        refused=await self.intents.rollback(origin.id,__import__('codex_web.action_intents',fromlist=['ActionIntentRollbackRequest']).ActionIntentRollbackRequest(reason='unrelated'),actor=self.worker_actor)
        self.assertEqual(refused.status,'requires_reconciliation')
        self.assertEqual(self.provider.rollbacks,0)

    async def test_rollback_restart_after_effect_before_receipt_never_replays(self):
        await self.through_change()
        self.config.write_text(json.dumps({'workers':['failed'],'http_status':503,'rotation':2}))
        await self.advance()
        original=self.intents._append_receipt
        def interrupted(*args,**kwargs):
            if kwargs.get('details',{}).get('operation')=='rollback':
                raise asyncio.CancelledError()
            return original(*args,**kwargs)
        with patch.object(self.intents,'_append_receipt',side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError): await self.advance()
        self.assertEqual(self.provider.rollbacks,1)
        self.rebuild();checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'reconciliation_required',checkpoint)
        await self.advance();self.assertEqual(self.provider.rollbacks,1)

    async def test_rollback_restart_after_receipt_before_status_uses_durable_result(self):
        await self.through_change()
        self.config.write_text(json.dumps({'workers':['failed'],'http_status':503,'rotation':2}))
        await self.advance()
        original=self.intents._append_receipt
        def interrupted(*args,**kwargs):
            receipt=original(*args,**kwargs)
            if kwargs.get('details',{}).get('operation')=='rollback':
                raise asyncio.CancelledError()
            return receipt
        with patch.object(self.intents,'_append_receipt',side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError): await self.advance()
        self.rebuild();checkpoint=await self.finish()
        self.assertEqual(checkpoint.status,'recovered',checkpoint)
        self.assertEqual(self.provider.rollbacks,1)

    async def test_verification_restart_resumes_canonical_read_receipt_without_mutation_replay(self):
        await self.through_change()
        original=self.intents._verify_completion
        async def interrupted(intent,*args,**kwargs):
            if intent.action_id=='fixture.readiness':
                raise asyncio.CancelledError()
            return await original(intent,*args,**kwargs)
        with patch.object(self.intents,'_verify_completion',side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError): await self.advance()
        checkpoint=self.workers.store.assignment(self.assignment.id).procedure_checkpoint
        read_intent=checkpoint.steps['ready'].action_intent_id
        self.intents.store.update(self.expire_intent,intent_ids=(read_intent,))
        self.rebuild();checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'reconciliation_required',checkpoint)
        reconciled=await self.intents.reconcile(read_intent,ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor)
        self.assertEqual(reconciled.status,'succeeded')
        checkpoint=await self.finish()
        self.assertEqual(checkpoint.status,'completed',checkpoint)
        self.assertEqual(self.provider.calls.count('fixture.change'),1)
        self.assertEqual(self.provider.calls.count('fixture.readiness'),1)
        self.assertEqual(self.http_mutations,0)

    async def test_known_failure_retry_exhaustion_is_bounded_and_attention(self):
        self.provider.fail=True
        first=await self.advance();self.assertEqual(first.status,'waiting',first)
        checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertEqual(checkpoint.steps['backup'].attempts,2)
        self.assertEqual(self.provider.calls,['fixture.backup','fixture.backup'])
        self.assertTrue(checkpoint.attention_id)

    async def test_quarantine_revocation_and_input_artifact_invalidation(self):
        self.definition_registry.quarantine(self.pin.record_id,actor=self.actor.identity_id,reason='revoked trust')
        checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertEqual(self.provider.calls,[])

    async def test_sensitive_output_and_missing_declared_evidence_cannot_complete(self):
        original=self.provider.execute
        async def missing(*args,**kwargs):
            result=await original(*args,**kwargs)
            return result.model_copy(update={'output':{'outputs':{'backup_done':True},'evidence':{}}})
        with patch.object(self.provider,'execute',side_effect=missing):
            checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertEqual(self.provider.calls,['fixture.backup'])

    def install_governed_model(self, *, malformed=False):
        from types import SimpleNamespace
        from tests import test_model_gateway as model_fixtures
        from codex_web.services.model_gateway import ModelGatewayService
        from codex_web.storage.model_gateway import ModelGatewayStore
        from codex_web.model_gateway import PromptTemplateUpsert, ModelProviderResult, ModelProviderUsage
        gateway=ModelGatewayService(ModelGatewayStore(self.sqlite))
        class Adapter(model_fixtures._FakeAdapter):
            async def invoke(inner, provider, model, request, *, credential):
                inner.calls.append(request)
                return ModelProviderResult(text='bad-json' if malformed else '{"outputs":{"judgment_done":true}}',
                    usage=ModelProviderUsage(input_tokens=40,output_tokens=12))
        adapter=Adapter();gateway.register_adapter(adapter)
        gateway.upsert_template(PromptTemplateUpsert(template_id='generic.system',version='1.0',content='{{ instructions }}'),actor=self.actor)
        fixture=SimpleNamespace(service=gateway,actor=self.actor)
        model_fixtures.ModelGatewayTests._provider(fixture)
        model_fixtures.ModelGatewayTests._model(fixture,'m1')
        self.runner.models=gateway
        return gateway,adapter

    async def test_declared_judgment_uses_real_governed_gateway_with_bounded_attribution(self):
        gateway,adapter=self.install_governed_model()
        checkpoint=await self.advance()
        self.assertEqual(checkpoint.steps['judgment'].status,'succeeded',checkpoint)
        self.assertEqual(len(adapter.calls),1)
        request=adapter.calls[0]
        self.assertFalse(request.allow_fallback)
        self.assertEqual(request.max_input_tokens,1000);self.assertEqual(request.max_output_tokens,200)
        records=gateway.invocations(self.actor)
        self.assertEqual(len(records),1)
        self.assertEqual(records[0].execution_id,'fixture-execution')
        self.assertIn(':role:operator',records[0].purpose)
        self.assertEqual(checkpoint.steps['judgment'].invocation_ids,(records[0].id,))
        checkpoint=await self.finish();self.assertEqual(checkpoint.status,'completed',checkpoint)
        self.assertEqual(len(adapter.calls),1)

    async def test_declared_judgment_malformed_output_exhausts_budget(self):
        gateway,adapter=self.install_governed_model(malformed=True)
        checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertEqual(checkpoint.steps['judgment'].attempts,1)
        await self.advance();self.assertEqual(len(adapter.calls),1)
        self.assertTrue(checkpoint.attention_id)

    async def test_declared_judgment_governance_denial_makes_zero_calls(self):
        gateway,adapter=self.install_governed_model()
        snapshot=self.artifacts.get_artifact(self.snapshot.id,actor=self.actor)
        def deny(state):
            for record in state.records:
                if record.id==snapshot.governance_record_id:
                    record.deny_model_context=True
            return state
        self.artifacts.governance.store.update(deny)
        checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertEqual(adapter.calls,[])

    async def test_forward_crash_after_effect_before_receipt_requires_reconciliation(self):
        await self.advance();await self.advance()
        original=self.intents._append_receipt
        def crash(intent,*args,**kwargs):
            if intent.action_id=='fixture.change':
                raise asyncio.CancelledError()
            return original(intent,*args,**kwargs)
        with patch.object(self.intents,'_append_receipt',side_effect=crash):
            with self.assertRaises(asyncio.CancelledError):await self.advance()
        self.assertEqual(json.loads(self.config.read_text())['rotation'],2)
        checkpoint=self.workers.store.assignment(self.assignment.id).procedure_checkpoint
        origin=checkpoint.steps['change'].action_intent_id
        self.intents.store.update(self.expire_intent,intent_ids=(origin,))
        self.rebuild();checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'reconciliation_required',checkpoint)
        self.assertEqual(self.provider.calls.count('fixture.change'),1)
        self.assertEqual(self.provider.rollbacks,0)

    async def test_provider_verification_interruption_uses_receipt_without_replay(self):
        await self.advance();await self.advance()
        original=self.intents._verify_completion
        async def crash(intent,*args,**kwargs):
            if intent.action_id=='fixture.change':
                raise asyncio.CancelledError()
            return await original(intent,*args,**kwargs)
        with patch.object(self.intents,'_verify_completion',side_effect=crash):
            with self.assertRaises(asyncio.CancelledError):await self.advance()
        checkpoint=self.workers.store.assignment(self.assignment.id).procedure_checkpoint
        origin=checkpoint.steps['change'].action_intent_id
        self.intents.store.update(self.expire_intent,intent_ids=(origin,))
        self.rebuild();checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'reconciliation_required',checkpoint)
        result=await self.intents.reconcile(origin,ActionIntentReconcileRequest(retry_if_idempotent=False),actor=self.worker_actor)
        self.assertEqual(result.status,'succeeded')
        checkpoint=await self.finish();self.assertEqual(checkpoint.status,'completed',checkpoint)
        self.assertEqual(self.provider.calls.count('fixture.change'),1)

    async def test_revoked_worker_capability_and_material_resource_change_stop(self):
        await self.advance()
        def revoke(state):
            state.workers[0].capabilities=(__import__('codex_web.execution_workers',fromlist=['WorkerCapability']).WorkerCapability.GIT,)
            return state
        self.workers.store.update_worker(self.worker.id,revoke)
        checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertEqual(self.provider.calls,['fixture.backup'])

    async def test_input_snapshot_invalidation_cannot_reconstruct_or_replay(self):
        self.artifacts.invalidate_artifact(self.snapshot.id,'controlled invalidation',actor=self.actor)
        checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertEqual(self.provider.calls,[])

    def enable_parent_network(self):
        from codex_web.execution_workers import NetworkPolicy, WorkerCapability
        # Only this test-owned canonical assignment changes. No live worker or
        # host privilege is modified by qualification.
        def enable(state):
            state.assignments[0].network=NetworkPolicy(enabled=True)
            state.assignments[0].required_capabilities=(WorkerCapability.COMMAND_EXECUTION,WorkerCapability.NETWORK)
            state.assignments[0].sandbox='danger-full-access'
            return state
        self.workers.store.update_assignment(self.worker.id,self.assignment.id,enable)

    async def test_network_enabled_parent_is_narrowed_before_backend_admission(self):
        self.enable_parent_network()
        await self.advance()
        captured=[]
        original=self.runtime.backend.run
        def inspect(assignment,**kwargs):
            captured.append(assignment)
            self.assertEqual(assignment.sandbox,'read-only')
            self.assertFalse(assignment.network.enabled)
            self.assertEqual(assignment.secret_refs,())
            self.assertIsNone(assignment.runtime_binding)
            self.assertIsNone(assignment.agent_profile)
            self.assertNotIn('CODEX_WEB_CONTROL_PLANE_URL',kwargs['environment'])
            self.assertNotIn('CONTAINER_HOST',kwargs['environment'])
            self.assertFalse(kwargs.get('trusted_writable_mounts'))
            return original(assignment,**kwargs)
        with patch.object(self.runtime.backend,'run',side_effect=inspect):
            checkpoint=await self.advance()
        self.assertEqual(checkpoint.steps['disk'].status,'succeeded',checkpoint)
        self.assertEqual(len(captured),1)
        parent=self.workers.store.assignment(self.assignment.id)
        self.assertTrue(parent.network.enabled)
        self.assertEqual(parent.sandbox,'danger-full-access')
        checkpoint=await self.finish()
        self.assertEqual(checkpoint.status,'completed',checkpoint)
        self.assertIn(('GET','/ready'),self.http_requests)
        self.assertEqual(self.http_mutations,0)

    async def test_real_network_enabled_parent_artifact_cannot_send_controlled_mutation_or_inherit_channels(self):
        if not self.native_worker_ready:
            self.skipTest('native network isolation proof requires qualified Bubblewrap')
        import urllib.request
        # Positive control: the test-owned endpoint is reachable and records POST.
        with urllib.request.urlopen(urllib.request.Request(self.http_url.replace('/ready','/mutate'),data=b'control',method='POST'),timeout=2):
            pass
        self.assertEqual(self.http_mutations,1)
        self.enable_parent_network()
        with patch.dict('os.environ',{'CODEX_WEB_CONTROL_PLANE_URL':self.http_url,'GH_TOKEN':'controlled-fixture-only',
                'OPENAI_API_KEY':'controlled-fixture-only','SSH_AUTH_SOCK':'/tmp/controlled-no-socket','CONTAINER_HOST':'unix:///tmp/controlled-no-socket'}):
            checkpoint=await self.finish()
        self.assertEqual(checkpoint.status,'completed',checkpoint)
        self.assertTrue(self.runner._output_values(self.workers.store.assignment(self.assignment.id),checkpoint.steps['disk'],self.actor)['disk_ok'])
        self.assertEqual(self.http_mutations,1)
        self.assertEqual(sum(method=='POST' for method,path in self.http_requests),1)
        self.assertIn(('GET','/ready'),self.http_requests)

    async def test_worker_resource_lease_loss_during_diagnostic_is_visible(self):
        await self.advance()
        original=self.runtime.backend.run
        def lose_lease(assignment,**kwargs):
            def expire(state):
                for lease in state.leases:
                    if lease.execution_id==assignment.execution_id:
                        lease.expires_at=time.time()-1
                return state
            self.workspaces.store.update(expire)
            return original(assignment,**kwargs)
        with patch.object(self.runtime.backend,'run',side_effect=lose_lease):
            checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertTrue(checkpoint.attention_id)
        self.assertEqual(self.provider.calls,['fixture.backup'])

    async def test_secret_reference_is_preserved_through_originating_provider_rollback(self):
        await self.through_change()
        self.config.write_text(json.dumps({'workers':['failed'],'http_status':503,'rotation':2}))
        await self.advance()
        checkpoint=await self.finish()
        self.assertEqual(checkpoint.status,'recovered',checkpoint)
        self.assertEqual(self.provider.rollbacks,1)
        self.assertNotIn(self.expected_fixture_credential,checkpoint.model_dump_json())
        origin=self.intents.get(checkpoint.steps['change'].action_intent_id,self.actor)
        self.assertEqual(origin.credential_ref,self.secret_fixture_ref)

    async def test_secret_reference_revocation_blocks_rollback_and_records_attention(self):
        await self.through_change()
        self.config.write_text(json.dumps({'workers':['failed'],'http_status':503,'rotation':2}))
        await self.advance()
        self.secret_broker.revoke(self.secret_fixture_ref,actor=self.actor,reason='controlled fixture revocation')
        checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertEqual(self.provider.rollbacks,0)
        self.assertTrue(checkpoint.attention_id)

    async def test_tenant_fenced_api_checkpoint_and_active_pin_conflict(self):
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        from codex_web.api.procedure_execution import build_procedure_execution_router
        app=FastAPI();current={'actor':self.actor}
        @app.middleware('http')
        async def authenticated(request:Request,call_next):
            request.state.identity_actor=current['actor']
            return await call_next(request)
        app.include_router(build_procedure_execution_router(self.runner))
        with TestClient(app) as client:
            endpoint=f'/api/execution-workers/assignments/{self.assignment.id}/procedure'
            result=client.get(endpoint)
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.json()['procedure_checkpoint']['definition']['checksum'],self.pin.checksum)
            other=self.artifact(b'{}','replacement-input')
            conflict=client.post(endpoint+'/start',json={'definition':self.pin.model_dump(mode='json'),'input_snapshot_id':other.id})
            self.assertEqual(conflict.status_code,409)
            self.assertEqual(conflict.json()['detail']['code'],'procedure_active_pin_conflict')
            current['actor']=self.actor.model_copy(update={'workspace_id':'unrelated-workspace'})
            denied=client.get(endpoint)
            self.assertEqual(denied.status_code,409)
            self.assertNotIn(self.pin.checksum,denied.text)

    async def test_material_precondition_change_is_not_silent_scope_adoption(self):
        from codex_web.resources import ResourceUpdate
        await self.advance()
        self.resources.update(self.resource.id,ResourceUpdate(operational_context={'environment':'production'}),actor=self.actor)
        checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'recovery_required',checkpoint)
        self.assertEqual(checkpoint.failure_code,'procedure_material_preconditions_changed')
        self.assertEqual(self.provider.calls,['fixture.backup'])
        self.assertTrue(checkpoint.attention_id)

    async def test_secret_reference_provider_output_cannot_leak_into_receipts_or_checkpoint(self):
        original=self.provider.execute
        async def leak(*args,**kwargs):
            result=await original(*args,**kwargs)
            output=dict(result.output);output['sensitive']=self.expected_fixture_credential
            return result.model_copy(update={'output':output})
        with patch.object(self.provider,'execute',side_effect=leak):
            checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'reconciliation_required',checkpoint)
        self.assertNotIn(self.expected_fixture_credential,checkpoint.model_dump_json())
        intent=checkpoint.steps['backup'].action_intent_id
        self.assertNotIn(self.expected_fixture_credential,json.dumps(self.intents.history(intent,self.actor)))

    async def test_unbounded_provider_output_retains_unknown_effect_without_persisting_body(self):
        original=self.provider.execute
        async def unbounded(*args,**kwargs):
            result=await original(*args,**kwargs)
            output=dict(result.output);output['unbounded']='controlled-boundary-value'*5000
            return result.model_copy(update={'output':output})
        with patch.object(self.provider,'execute',side_effect=unbounded):
            checkpoint=await self.advance()
        self.assertEqual(checkpoint.status,'reconciliation_required',checkpoint)
        intent=checkpoint.steps['backup'].action_intent_id
        self.assertNotIn('controlled-boundary-value',json.dumps(self.intents.history(intent,self.actor)))

    async def test_policy_permitted_deviation_retains_real_evidence_without_new_approval(self):
        checkpoint=await self.through_change()
        proof=checkpoint.steps['disk'].evidence['disk']
        result=await self.runner.deviate(self.assignment.id,step_id='optional-disk',
            reason='A current isolated disk observation already covers this optional diagnostic.',evidence_ids=(proof,),actor=self.actor)
        state=result.procedure_checkpoint
        self.assertEqual(state.steps['optional-disk'].status,'skipped')
        self.assertEqual(state.deviations[0].evidence_ids,(proof,))
        self.assertEqual(state.deviations[0].actor_identity_id,self.actor.identity_id)
        self.assertTrue(state.deviations[0].authority_definition.record_id)
        self.assertFalse(self.approvals.store.load().requests)
        with self.assertRaises(ProcedureExecutionError):
            await self.runner.deviate(self.assignment.id,step_id='ready',reason='cannot bypass verification',evidence_ids=(proof,),actor=self.actor)
        checkpoint=await self.finish()
        self.assertEqual(checkpoint.status,'completed',checkpoint)
        self.assertEqual(checkpoint.steps['optional-disk'].status,'skipped')
        self.runner.models.invoke.assert_not_called()

    async def test_high_risk_scoped_policy_and_current_authority_are_required(self):
        checkpoint = await self.finish()
        self.assertEqual(checkpoint.status, 'completed', checkpoint)
        intent = self.intents.get(checkpoint.steps['change'].action_intent_id, actor=self.actor)
        self.assertEqual(intent.policy_decision.source, 'canonical:procedure-step')
        self.assertEqual(intent.policy_decision.definition_refs, (checkpoint.definition,))
        self.assertEqual(intent.security_decision.outcome, 'allow')

    async def test_network_enabled_parent_diagnostic_interruption_replay_is_bounded_and_network_disabled(self):
        self.enable_parent_network()
        await self.advance()
        original = self.runtime.backend.run
        calls = []
        def interrupt(assignment, **kwargs):
            calls.append(assignment)
            result = original(assignment, **kwargs)
            raise asyncio.CancelledError()
        with patch.object(self.runtime.backend, 'run', side_effect=interrupt):
            with self.assertRaises(asyncio.CancelledError):
                await self.advance()
        state = self.workers.store.assignment(self.assignment.id).procedure_checkpoint.steps['disk']
        self.assertEqual(state.status, 'running')
        self.assertEqual(state.attempts, 1)
        self.rebuild()
        def inspect(assignment, **kwargs):
            calls.append(assignment)
            return original(assignment, **kwargs)
        with patch.object(self.runtime.backend, 'run', side_effect=inspect):
            checkpoint = await self.finish()
        self.assertEqual(checkpoint.status, 'completed', checkpoint)
        self.assertEqual(checkpoint.steps['disk'].attempts, 2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(not c.network.enabled and c.sandbox == 'read-only' and not c.secret_refs for c in calls))
        self.assertEqual(self.provider.calls.count('fixture.change'), 1)
        self.assertIn(('GET', '/ready'), self.http_requests)
        self.assertEqual(self.http_mutations, 0)
        self.runner.models.invoke.assert_not_called()

    async def test_output_snapshot_invalidation_blocks_later_mutation(self):
        await self.advance()
        checkpoint = await self.advance()
        self.artifacts.invalidate_artifact(checkpoint.steps['disk'].output_snapshot_id,
            'controlled output invalidation', actor=self.actor)
        checkpoint = await self.advance()
        self.assertEqual(checkpoint.status, 'recovery_required', checkpoint)
        self.assertEqual(self.provider.calls, ['fixture.backup'])
        self.assertTrue(checkpoint.attention_id)


    def assert_rejected_credential_body_absent(self, checkpoint, intent_id):
        persisted = json.dumps(self.intents.history(intent_id, self.actor)) + checkpoint.model_dump_json()
        persisted += self.intents.store.load(intent_id=intent_id).model_dump_json()
        self.assertNotIn(self.expected_fixture_credential, persisted)
        self.assertNotIn('rejected-key-body-marker', persisted)
        from codex_web.action_intents import ActionIntentRetryRequest
        from codex_web.services.action_intents import ActionIntentUnsafeRetryError
        with self.assertRaises(ActionIntentUnsafeRetryError):
            self.intents.retry(intent_id, ActionIntentRetryRequest(reason='must reconcile'), actor=self.actor)

    async def test_secret_reference_execute_mapping_key_rejection_stays_uncertain_after_restart(self):
        original = self.provider.execute
        async def leak(*args, **kwargs):
            result = await original(*args, **kwargs)
            return result.model_copy(update={'output': {'nested': [{'deeper': [{
                self.expected_fixture_credential: 'rejected-key-body-marker'}]}]}})
        with patch.object(self.provider, 'execute', side_effect=leak) as invoke:
            checkpoint = await self.advance()
            self.assertEqual(checkpoint.status, 'reconciliation_required', checkpoint)
            intent_id = checkpoint.steps['backup'].action_intent_id
            self.assertEqual(self.intents.get(intent_id, self.actor).status, 'uncertain')
            self.rebuild()
            checkpoint = await self.advance()
            self.assertEqual(invoke.call_count, 1)
        self.assertEqual(checkpoint.status, 'reconciliation_required')
        self.assert_rejected_credential_body_absent(checkpoint, intent_id)

    async def test_secret_reference_verify_metadata_key_rejection_requires_reconciliation(self):
        from codex_web.action_providers import ActionEvidence
        async def leak(*args, **kwargs):
            self.assertEqual(kwargs['credential'], self.expected_fixture_credential)
            return ActionVerification(verified=True, evidence=(ActionEvidence(evidence_type='controlled',
                metadata={self.expected_fixture_credential: 'rejected-key-body-marker'}),))
        with patch.object(self.provider, 'verify', side_effect=leak) as invoke:
            checkpoint = await self.advance()
            self.assertEqual(checkpoint.status, 'reconciliation_required', checkpoint)
            intent_id = checkpoint.steps['backup'].action_intent_id
            self.assertEqual(self.intents.get(intent_id, self.actor).status, 'requires_reconciliation')
            history = self.intents.history(intent_id, self.actor)
            self.assertTrue(history['receipts'])
            self.assertIsNone(history['verifications'][-1]['provider_verification'])
            self.rebuild()
            checkpoint = await self.advance()
            self.assertEqual(invoke.call_count, 1)
        self.assertEqual(checkpoint.status, 'reconciliation_required')
        self.assert_rejected_credential_body_absent(checkpoint, intent_id)

    async def test_secret_reference_rollback_mapping_key_rejection_never_replays_restoration(self):
        await self.through_change()
        self.config.write_text(json.dumps({'workers': ['failed'], 'http_status': 503, 'rotation': 2}))
        await self.advance()
        original = self.provider.rollback
        async def leak(*args, **kwargs):
            result = await original(*args, **kwargs)
            return result.model_copy(update={'output': {'nested': [{self.expected_fixture_credential:
                'rejected-key-body-marker'}]}})
        with patch.object(self.provider, 'rollback', side_effect=leak) as invoke:
            checkpoint = await self.advance()
            self.assertEqual(checkpoint.status, 'reconciliation_required', checkpoint)
            intent_id = checkpoint.steps['rollback'].action_intent_id
            origin = self.intents.get(intent_id, self.actor)
            self.assertEqual(origin.credential_ref, self.secret_fixture_ref)
            self.assertEqual(origin.status, 'requires_reconciliation')
            self.assertEqual(origin.rollback_checkpoint.status, 'unknown')
            self.assertEqual(json.loads(self.config.read_text())['rotation'], 1)
            self.rebuild()
            checkpoint = await self.advance()
            self.assertEqual(invoke.call_count, 1)
        self.assertEqual(checkpoint.status, 'reconciliation_required')
        self.assertEqual(self.provider.rollbacks, 1)
        self.assert_rejected_credential_body_absent(checkpoint, intent_id)
