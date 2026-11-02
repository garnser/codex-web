"""Real private bootstrap/session persistence; only native process transport is synthetic."""
import unittest
import subprocess
from pathlib import Path

from codex_web.agent_runtime import AgentSession, AgentSessionStatus
from codex_web.execution_workers import AssignmentCancelRequest
from codex_web.resources import ResourceAlias, ResourceCreate, ResourceType
from codex_web.services.agent_runtime import AgentRuntimeRegistry, AgentSessionService
from codex_web.storage.agent_sessions import AgentSessionStore
from tests.thread_bootstrap_composition_harness import build_real_thread_create


class BootstrapResourceCustodyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = build_real_thread_create(self)
        self.sessions = AgentSessionStore(self.f.sqlite)
        self.f.thread_service.agent_sessions = AgentSessionService(self.sessions, AgentRuntimeRegistry())

    async def create(self, **kwargs):
        response = await self.f.thread_service.create(project_id=self.f.project.id, **kwargs)
        session = self.sessions.get(response['agentSessionId'],
            organization_id=self.f.actor.organization_id, workspace_id=self.f.actor.workspace_id)
        assignment = self.f.workers.store.assignment(session.assignment_id)
        return session, assignment

    def assert_custody(self, session, assignment):
        self.assertEqual(session.resource_ids, tuple(sorted(assignment.resource_ids)))
        self.assertEqual((session.organization_id, session.workspace_id, session.project_id,
                          session.assignment_id, session.execution_id,
                          session.execution_workspace_id, session.worker_id),
                         (assignment.organization_id, assignment.workspace_id, assignment.project_id,
                          assignment.id, assignment.execution_id,
                          assignment.execution_workspace_id, assignment.assigned_worker_id))
        self.assertEqual(session.status, AgentSessionStatus.READY)
        self.assertIsNone(session.failure)
        # A provider thread/start creates a conversation, never a model turn.
        self.assertEqual([method for method, _ in self.f.process_session.requests], ['thread/start'])

    async def test_repository_bootstrap_persists_actual_admitted_resources_before_input(self):
        session, assignment = await self.create(repository_resource_id=self.f.repository.id)
        self.assertEqual(assignment.resource_ids, (self.f.repository.id,))
        self.assert_custody(session, assignment)

    async def test_cancel_before_first_input_preserves_exact_session_resource_custody(self):
        session, assignment = await self.create(repository_resource_id=self.f.repository.id)
        before = session.model_dump(mode='json')
        cancelled = self.f.workers.cancel_bootstrap(assignment.id,
            AssignmentCancelRequest(expected_fence=assignment.fence, reason='private idle cancellation'),
            actor=self.f.actor)
        retained = self.sessions.native_custody(session.provider_native_session_id)
        self.assertEqual(cancelled.status, 'cancelled')
        self.assertEqual(cancelled.fence, assignment.fence + 1)
        self.assertIsNone(cancelled.lease)
        self.assertEqual(retained.model_dump(mode='json'), before)
        # This exact positive join is required by cancelled-recipient admission;
        # no historical Session rewrite or first model turn repairs it.
        self.assert_custody(retained, cancelled)

    async def test_native_session_receives_both_admitted_writable_and_readonly_resources(self):
        source = Path(self.f.temp.name)/'readonly-source'
        source.mkdir()
        for args in [('init',), ('config','user.email','private@example.invalid'),
                     ('config','user.name','Private fixture'), ('commit','--allow-empty','-m','Private base')]:
            subprocess.run(['git',*args],cwd=source,check=True,capture_output=True)
        resource = self.f.resources.create(ResourceCreate(resource_type=ResourceType.REPOSITORY,
            name='Private readonly dependency',
            aliases=[ResourceAlias(namespace='filesystem',value=str(source))]), actor=self.f.actor)
        self.f.resources.bind_project(project=self.f.project, resource_id=resource.id, actor=self.f.actor)
        session, assignment = await self.create(repository_resource_id=self.f.repository.id,
            read_only_repository_resource_ids=(resource.id,))
        self.assertEqual(set(assignment.resource_ids), {self.f.repository.id, resource.id})
        self.assert_custody(session, assignment)

    async def test_existing_historical_session_is_not_rewritten_by_bootstrap_adoption(self):
        original = self.sessions.upsert(AgentSession(organization_id=self.f.actor.organization_id,
            workspace_id=self.f.actor.workspace_id, project_id=self.f.project.id,
            provider_id='openai', runtime_id='codex', runtime_type='codex-app-server',
            provider_native_session_id='thread-created', status=AgentSessionStatus.READY,
            resource_ids=(), assignment_id='historical-assignment'))
        session, _ = await self.create(repository_resource_id=self.f.repository.id)
        self.assertEqual(session.model_dump(mode='json'), original.model_dump(mode='json'))
