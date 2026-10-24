"""Private real planner/workspace composition; external process transport only is simulated."""
import subprocess
from pathlib import Path
from types import SimpleNamespace
from tests import test_turn_execution_binding as planner_fixture
from tests import test_thread_bootstrap_routing as thread_fixture
from codex_web.execution_workspace_backend import LocalGitWorkspaceBackend
from codex_web.execution_workers import AssignmentClaimRequest,AssignmentStartRequest
from codex_web.identity import AuthenticationActor,PrincipalKind,AuthenticationAssurance
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from codex_web.services.threads import ThreadBootstrapPrestartRefusal

def build_real_thread_create(case):
    f=planner_fixture.TurnExecutionBindingTests();f.setUp();case.addCleanup(f.tearDown)
    f.service.clock=__import__('time').time
    repo=Path(f.project.path)
    for args in [('init',),('config','user.email','private@example.invalid'),('config','user.name','Private fixture'),('commit','--allow-empty','-m','Private base')]:
        subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True)
    f.workspaces.backend=LocalGitWorkspaceBackend(Path(f.temp.name)/'actual-workspaces')
    f._publish_secret();f.service.secrets=planner_fixture._AuthenticationSecrets()
    f.worker_actor=AuthenticationActor(identity_id='test-local-worker',principal_kind=PrincipalKind.SERVICE,
        organization_id=f.actor.organization_id,workspace_id=f.actor.workspace_id,assurance=AuthenticationAssurance.SERVICE_TOKEN,service_scopes=("execution-worker:run",))
    host=thread_fixture._Host();host.project=f.project
    session=thread_fixture._Session();claims=[]
    class Manager:
        async def start(_,aid):
            claim=f.workers.claim(f.worker.id,AssignmentClaimRequest(),actor=f.worker_actor,assignment_id=aid)
            actual=f.workers.start(f.worker.id,aid,AssignmentStartRequest(fence=claim.fence,lease_token=claim.lease.lease_token),actor=f.worker_actor)
            claims.append(actual)
            session.workspace_path=Path(f.workspaces.get(actual.execution_workspace_id,f.actor).path)
            session.status=lambda:SimpleNamespace(worker_id=f.worker.id,fence=actual.fence)
            return session
        async def complete(_,aid,**kwargs):raise AssertionError('unexpected failed worker cleanup')
    bound=ThreadBootstrapBindingService(ThreadBootstrapBindingStore(f.sqlite));bound.assignment_store=f.workers.store
    manager=Manager()
    service=thread_fixture._thread_service(host,binding_service=f.service,session_manager=manager,bootstrap_bindings=bound,control_actor=f.actor)
    service.session_managers[('openai','codex')]=manager
    f.thread_service=service;f.process_session=session;f.started_assignments=claims;f.bootstrap_bindings=bound;f.transport_host=host
    return f
