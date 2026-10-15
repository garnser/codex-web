"""Controlled process + canonical stores/routes for runner failure regressions.

Test-only composition. No model, native provider or host application is started.
"""
import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI

from codex_web.agent_runtime import AgentSession
from codex_web.api.context import build_context_router
from codex_web.services.context import ContextCompactionService
from codex_web.api.threads import build_threads_router
from codex_web.local_execution_backend import BubblewrapExecutionBackend
from codex_web.models import IndexedThread, Project
from codex_web.services.agent_runtime import AgentRuntimeRegistry, AgentSessionService
from codex_web.services.artifact_evidence import ArtifactEvidenceService
from codex_web.services.projects import ProjectService
from codex_web.services.thread_bootstrap_bindings import ThreadBootstrapBindingService
from codex_web.services.thread_scope import ThreadScopeService
from codex_web.services.threads import ThreadService
from codex_web.storage.agent_sessions import AgentSessionStore
from codex_web.storage.artifact_evidence import ArtifactEvidenceStore
from codex_web.storage.thread_bootstrap_bindings import ThreadBootstrapBindingStore
from codex_web.storage.thread_history import ThreadHistoryRepository
from codex_web.storage.thread_index import ThreadIndexRepository
from codex_web.thread_bootstrap import ThreadBootstrapBinding
from tests.test_codex_worker_session import AssignmentBoundCodexSessionTests


class RunnerFailureFixture:
    _create_assignment = AssignmentBoundCodexSessionTests._create_assignment
    _session = AssignmentBoundCodexSessionTests._session

    async def start(self):
        await AssignmentBoundCodexSessionTests.asyncSetUp(self)
        self.sqlite = self.worker_service.store.store
        self.evidence = ArtifactEvidenceService(ArtifactEvidenceStore(self.sqlite))
        self.local_worker.artifact_evidence = self.evidence
        # Launch only an inert bounded child. Canonical session supervision,
        # identity, lease/fence, failure persistence, scope and routes stay real.
        def spawn(*args, **kwargs):
            return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                cwd=self.workspace_path, env={}, start_new_session=True)
        self.backend.spawn_interactive = spawn
        self.backend.terminate_process = BubblewrapExecutionBackend._kill_process_group
        self.gate = asyncio.Event()
        self.session = await self._session(self._create_assignment(), self.gate)
        self.process = self.session._owned_process
        # Use actual Git membership/ignore state, not suggestive filenames.
        subprocess.run(['git', 'init', str(self.workspace_path)], check=True, capture_output=True)
        (self.workspace_path/'tracked.py').write_text('original tracked source\n')
        (self.workspace_path/'.gitignore').write_text('.venv/\nignored/\n')
        subprocess.run(['git', '-C', str(self.workspace_path), 'add', 'tracked.py', '.gitignore'],
            check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.workspace_path), '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false',
            'commit', '-m', 'Controlled source baseline'], check=True, capture_output=True)
        self.files = {'tracked.py': 'partial tracked source\n', 'untracked.py': 'partial new source\n',
                      'ignored/report.json': '{"state":"running","outcome":"unknown"}\n'}
        for name, content in self.files.items():
            path = self.workspace_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git_status_before = self.git_status()
        if not all(marker in self.git_status_before for marker in [' M tracked.py', '?? untracked.py', '!! ignored/']):
            raise AssertionError('fixture must contain real tracked/untracked/ignored source')
        assignment = self.worker_service.store.assignment(self.session.assignment_id)
        self.actor = self.admin
        self.project = Project(id='home', name='Home', path=str(self.workspace_path),
            organization_id=assignment.organization_id, workspace_id=assignment.workspace_id)
        projects = ProjectService(SimpleNamespace(load=lambda: [self.project]))
        sessions = AgentSessionService(AgentSessionStore(self.sqlite), AgentRuntimeRegistry())
        sessions.store.upsert(AgentSession(organization_id=assignment.organization_id,
            workspace_id=assignment.workspace_id, project_id='home', provider_id='openai',
            runtime_id='codex', runtime_type='app-server', provider_native_session_id='failure-thread'))
        index = ThreadIndexRepository(self.sqlite, Path(self.temp.name) / 'threads.json')
        index.upsert(IndexedThread(id='failure-thread', name='Failure fixture', cwd=str(self.workspace_path), project_id='home'))
        history = ThreadHistoryRepository(self.sqlite)
        history.start_turn('failure-thread', turn_id='retained-turn', message='Retained unfinished work',
            provider_id='openai', runtime_id='codex')
        bindings = ThreadBootstrapBindingStore(self.sqlite)
        binding = ThreadBootstrapBinding(id='failure-binding', organization_id=assignment.organization_id,
            workspace_id=assignment.workspace_id, created_by=self.admin.identity_id,
            bootstrap_id='failure-bootstrap', thread_id='failure-thread', assignment_id=assignment.id,
            execution_id=assignment.execution_id, execution_workspace_id=assignment.execution_workspace_id)
        bindings.update(lambda state: state.model_copy(update={'bindings': [binding]}))
        self.scope = ThreadScopeService(projects, sessions, index)
        self.service = ThreadService(runtime_transport=None, runtime_request_for_thread=None, event_sink=lambda _: None,
            thread_index=index, thread_history=history, bootstrap_bindings=ThreadBootstrapBindingService(bindings),
            binding_service=SimpleNamespace(workers=self.worker_service), control_actor=self.admin,
            session_manager=SimpleNamespace(get=lambda id: self.session if id == assignment.id else None),
            active_turn_getter=lambda _: SimpleNamespace(assignment_id=assignment.id, fence=assignment.fence, turn_id='retained-turn'))
        self.app = FastAPI()
        @self.app.middleware('http')
        async def actor(request, call_next):
            request.state.identity_actor = self.actor
            return await call_next(request)
        self.app.include_router(build_threads_router(self.service, self.scope))
        async def no_runtime(*args):
            raise AssertionError('context observation must never start a runtime')
        context = ContextCompactionService(request_for_thread=no_runtime, pending_approvals=lambda: {},
            approval_thread_id=lambda request: request.get('threadId'),
            queue_depth=lambda _: int(self.sqlite.record_get('fixture-context', 'queue-depth') or 0),
            thread_is_active=lambda _: False, raise_if_thread_replaced=lambda _: None, publish_event=no_runtime)
        self.app.include_router(build_context_router(context, self.scope))
        return self

    def git_status(self):
        return subprocess.check_output(['git', '-C', str(self.workspace_path),
            'status', '--porcelain=v1', '--ignored'], text=True)

    async def fail(self, *, diagnostic_unavailable=True):
        self.backend.disk_bytes = 2 * 1024**3
        if diagnostic_unavailable:
            self.sqlite._write_lock.acquire()
        try:
            self.gate.set()
            await asyncio.wait_for(self.session.watchdog_task, 3)
        finally:
            if diagnostic_unavailable:
                self.sqlite._write_lock.release()
        for _ in range(100):
            if not self.session._executor_work:
                break
            await asyncio.sleep(.005)

    async def close(self):
        try:
            await self.session.stop()
            pending = list(self.session._executor_work)
            if pending:
                await asyncio.wait_for(asyncio.gather(*[asyncio.wrap_future(work) for work in pending],
                    return_exceptions=True), 2)
            if self.session._executor_work:
                raise AssertionError('fixture storage still has an owned executor')
            self.process.wait(timeout=2)
        finally:
            await AssignmentBoundCodexSessionTests.asyncTearDown(self)
