from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.agent_runtime import AgentRuntimeResult
from codex_web.execution_workers import ExecutionRuntimeBinding
from codex_web.models import Project, ThreadRunSettings
from codex_web.resources import RepositoryExecutionTarget, RepositoryTargetSource
from codex_web.services.threads import ThreadService
from codex_web.services.turn_execution_binding import (
    TurnExecutionBindingError,
)


class _Host:
    def __init__(self) -> None:
        self.project = Project(
            id="p1",
            name="Project",
            path="/control/project",
            sandbox="workspace-write",
            approval_policy="on-request",
            model="gpt-test",
        )
        self.codex = SimpleNamespace(request=AsyncMock())
        self.settings = []
        self.events = []

    def _project(self, project_id):
        if project_id not in {None, self.project.id}:
            raise LookupError("project not found")
        return self.project

    @staticmethod
    def _project_params(project, values):
        return {
            "cwd": project.path,
            **{key: value for key, value in values.items() if value is not None},
        }

    @staticmethod
    def _sandbox_policy(sandbox, cwd):
        return {"type": sandbox, "cwd": cwd}

    def _remember_thread_run_settings(self, thread_id, **kwargs):
        self.settings.append((thread_id, kwargs))

    def _append_bot_event(self, event):
        self.events.append(event)


class _ProjectRuntime:
    def __init__(self, host: _Host) -> None:
        self.host = host

    def get(self, project_id):
        return self.host._project(project_id)

    def find_by_cwd(self, cwd):
        return self.host.project if cwd == self.host.project.path else None

    def params(self, project, values=None):
        return self.host._project_params(project, values or {})

    def sandbox_policy(self, sandbox, cwd):
        return self.host._sandbox_policy(sandbox, cwd)


class _Settings:
    def __init__(self, host: _Host) -> None:
        self.host = host

    def get(self, thread_id):
        return ThreadRunSettings()

    def remember(self, thread_id, **kwargs):
        self.host._remember_thread_run_settings(thread_id, **kwargs)
        return ThreadRunSettings(**kwargs)


def _thread_service(host: _Host, **kwargs):
    async def request_for_thread(thread_id, method, params=None):
        return await host.codex.request(method, params or {})

    return ThreadService(
        runtime_transport=host.codex,
        runtime_request_for_thread=request_for_thread,
        event_sink=host._append_bot_event,
        project_runtime=_ProjectRuntime(host),
        settings=_Settings(host),
        **kwargs,
    )


class _BlockedBindingService:
    def prepare_bootstrap(self, **kwargs):
        raise TurnExecutionBindingError(
            "worker_capability_missing: command execution unavailable",
            code="worker_capability_missing",
            blocker={
                "code": "worker_capability_missing",
                "message": "command execution unavailable",
                "retryable": False,
                "required_capabilities": ["command_execution"],
                "available_capabilities": ["git"],
                "remediation": "Fix the local isolation probe.",
            },
        )


class _BindingService:
    def __init__(self) -> None:
        self.calls = []

    def prepare_bootstrap(self, **kwargs):
        self.calls.append(kwargs)
        target = RepositoryExecutionTarget(
            organization_id="local",
            workspace_id="default",
            project_id=kwargs["project_id"],
            mutable_repository_id=kwargs.get("explicit_repository_id") or "repo-default",
            read_only_repository_ids=kwargs.get("read_only_repository_ids") or (),
            source=(
                RepositoryTargetSource.EXPLICIT
                if kwargs.get("explicit_repository_id")
                else RepositoryTargetSource.SINGLE_REPOSITORY
            ),
        )
        return SimpleNamespace(
            execution_id=kwargs["execution_id"],
            assignment_id="assignment-bootstrap",
            workspace_id="execws-bootstrap",
            repository_resource_id=target.mutable_repository_id,
            repository_target=target,
        )


class _Session:
    def __init__(self, response=None, error=None) -> None:
        self.workspace_path = Path("/isolated/bootstrap")
        self.requests = []
        self.response = response or {"thread": {"id": "thread-created"}}
        self.error = error

    def status(self):
        return SimpleNamespace(worker_id="worker-1", fence=3)

    async def request(self, method, params=None):
        self.requests.append((method, params))
        if self.error is not None:
            raise self.error
        return self.response


class _SessionManager:
    def __init__(self, session) -> None:
        self.session = session
        self.started = []
        self.completed = []

    async def start(self, assignment_id):
        self.started.append(assignment_id)
        return self.session

    async def complete(self, assignment_id, **kwargs):
        self.completed.append((assignment_id, kwargs))
        return SimpleNamespace(id=assignment_id)


class _AgentSessions:
    def __init__(self) -> None:
        self.calls = []

    def adopt(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id="agent-session-1")


class _BootstrapBindings:
    def __init__(self, *, error=None) -> None:
        self.calls = []
        self.error = error

    def bind(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(**kwargs)

class _RoutingService:
    def __init__(self, binding, *, agent_profile=None) -> None:
        self.binding = binding
        self.agent_profile = agent_profile
        self.calls = []

    async def route(self, request, *, actor):
        self.calls.append((request, actor))
        return SimpleNamespace(
            selected_runtime=SimpleNamespace(
                execution_binding=lambda: self.binding
            ),
            agent_profile=self.agent_profile,
        )


class _AgentProfiles:
    def __init__(self, profile) -> None:
        self.profile = profile
        self.calls = []

    def resolve_for_execution(self, profile_id, *, actor, project_id, revision=None):
        self.calls.append(
            {
                "profile_id": profile_id,
                "actor": actor,
                "project_id": project_id,
                "revision": revision,
            }
        )
        return self.profile, SimpleNamespace(allowed=True)


class _AlternateAdapter:
    provider_id = "anthropic"
    runtime_id = "claude-code"
    runtime_type = "claude-agent-sdk"
    capabilities = ()

    def __init__(self, session) -> None:
        self.session = session
        self.created = []

    async def create_session(self, request):
        self.created.append(request)
        return AgentRuntimeResult(
            provider_native_session_id="claude-native-session",
            payload={
                "type": "system",
                "subtype": "init",
                "session_id": "claude-native-session",
            },
        )


class ThreadBootstrapCreateTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, *, session=None, bindings=None, agent_sessions=None):
        host = _Host()
        binding_service = _BindingService()
        session = session or _Session()
        manager = _SessionManager(session)
        bindings = bindings or _BootstrapBindings()
        actor = SimpleNamespace(identity_id="control")
        service = _thread_service(
            host,
            binding_service=binding_service,
            session_manager=manager,
            bootstrap_bindings=bindings,
            control_actor=actor,
            agent_sessions=agent_sessions,
        )
        return host, binding_service, manager, bindings, service

    async def test_worker_preflight_blocker_is_structured_503(self) -> None:
        host = _Host()
        manager = _SessionManager(_Session())
        service = _thread_service(
            host,
            binding_service=_BlockedBindingService(),
            session_manager=manager,
            bootstrap_bindings=_BootstrapBindings(),
            control_actor=SimpleNamespace(identity_id="control"),
        )

        with self.assertRaises(HTTPException) as caught:
            await service.create(project_id="p1")

        self.assertEqual(caught.exception.status_code, 503)
        detail = caught.exception.detail
        self.assertEqual(detail["code"], "execution_preflight_blocked")
        self.assertFalse(detail["retryable"])
        self.assertEqual(
            detail["blockers"][0]["code"],
            "worker_capability_missing",
        )
        self.assertEqual(manager.started, [])
        host.codex.request.assert_not_awaited()

    async def test_create_routes_thread_start_only_through_isolated_bootstrap_session(self) -> None:
        host, planner, manager, bindings, service = self._service()

        response = await service.create(
            project_id="p1",
            sandbox="workspace-write",
            approval_policy="on-request",
            model="gpt-test",
            reasoning_effort="medium",
        )

        self.assertEqual(response["thread"]["id"], "thread-created")
        host.codex.request.assert_not_awaited()
        self.assertEqual(manager.started, ["assignment-bootstrap"])
        self.assertEqual(manager.completed, [])
        self.assertEqual(len(planner.calls), 1)
        self.assertTrue(planner.calls[0]["bootstrap_id"].startswith("bootstrap-"))
        self.assertTrue(
            planner.calls[0]["execution_id"].startswith("thread-bootstrap-")
        )
        self.assertEqual(planner.calls[0]["project_id"], "p1")

        self.assertEqual(len(manager.session.requests), 1)
        method, params = manager.session.requests[0]
        self.assertEqual(method, "thread/start")
        self.assertEqual(params["cwd"], "/isolated/bootstrap")
        self.assertEqual(
            params["sandboxPolicy"],
            {"type": "workspace-write", "cwd": "/isolated/bootstrap"},
        )
        self.assertNotEqual(params["cwd"], host.project.path)

        self.assertEqual(len(bindings.calls), 1)
        bound = bindings.calls[0]
        self.assertEqual(bound["thread_id"], "thread-created")
        self.assertEqual(bound["assignment_id"], "assignment-bootstrap")
        self.assertEqual(bound["execution_workspace_id"], "execws-bootstrap")
        self.assertEqual(bound["execution_id"], planner.calls[0]["execution_id"])
        self.assertEqual(host.settings[0][0], "thread-created")
        self.assertEqual(host.events[-1]["type"], "thread_bootstrap_bound")

    async def test_create_binds_exact_agent_profile_to_bootstrap(self) -> None:
        host = _Host()
        binding_service = _BindingService()
        manager = _SessionManager(_Session())
        bindings = _BootstrapBindings()
        actor = SimpleNamespace(identity_id="human-a")
        profile = SimpleNamespace(
            revision=7,
            sandbox_requirement="workspace-write",
            execution_profile_id=None,
        )
        execution_binding = AgentProfileExecutionBinding(
            profile_id="coder",
            profile_revision=7,
            profile_record_id="agent-profile-rev-7",
        )
        routing = _RoutingService(
            None,
            agent_profile=execution_binding,
        )
        profiles = _AgentProfiles(profile)
        service = _thread_service(
            host,
            binding_service=binding_service,
            session_manager=manager,
            bootstrap_bindings=bindings,
            control_actor=SimpleNamespace(identity_id="control"),
            agent_profiles=profiles,
            routing_service=routing,
        )

        response = await service.create(
            project_id="p1",
            actor=actor,
            agent_profile_id="coder",
            agent_profile_revision=7,
        )

        self.assertEqual(response["thread"]["id"], "thread-created")
        self.assertEqual(len(profiles.calls), 1)
        self.assertEqual(profiles.calls[0]["profile_id"], "coder")
        self.assertIs(profiles.calls[0]["actor"], actor)
        self.assertEqual(len(routing.calls), 1)
        routed_request, routed_actor = routing.calls[0]
        self.assertEqual(routed_request.agent_profile_id, "coder")
        self.assertEqual(routed_request.agent_profile_revision, 7)
        self.assertIs(routed_actor, actor)
        self.assertEqual(
            binding_service.calls[0]["agent_profile"],
            execution_binding,
        )
        self.assertEqual(
            host.events[-1]["agent_profile"]["profile_id"],
            "coder",
        )

    async def test_create_passes_and_persists_repository_target(self) -> None:
        host, planner, _manager, _bindings, service = self._service()

        await service.create(
            project_id="p1",
            repository_resource_id="repo-app",
            read_only_repository_resource_ids=("repo-docs",),
        )

        call = planner.calls[0]
        self.assertEqual(call["explicit_repository_id"], "repo-app")
        self.assertEqual(call["read_only_repository_ids"], ("repo-docs",))
        remembered = host.settings[0][1]
        self.assertEqual(remembered["repository_resource_id"], "repo-app")
        self.assertEqual(
            remembered["read_only_repository_resource_ids"],
            ("repo-docs",),
        )
        self.assertEqual(
            host.events[-1]["repository_target"]["mutable_repository_id"],
            "repo-app",
        )

    async def test_create_records_separate_canonical_agent_session_identity(self) -> None:
        agent_sessions = _AgentSessions()
        host, planner, manager, bindings, service = self._service(
            agent_sessions=agent_sessions
        )

        response = await service.create(project_id="p1")

        self.assertEqual(response["thread"]["id"], "thread-created")
        self.assertEqual(response["agentSessionId"], "agent-session-1")
        self.assertEqual(len(agent_sessions.calls), 1)
        adopted = agent_sessions.calls[0]
        self.assertEqual(adopted["provider_native_session_id"], "thread-created")
        self.assertEqual(adopted["provider_id"], "openai")
        self.assertEqual(adopted["runtime_id"], "codex")
        self.assertEqual(
            adopted["request"].assignment_id,
            "assignment-bootstrap",
        )
        self.assertEqual(
            host.events[-1]["agent_session_id"],
            "agent-session-1",
        )


    async def test_explicit_runtime_choice_is_forced_through_canonical_routing(self) -> None:
        host = _Host()
        planner = _BindingService()
        default_manager = _SessionManager(_Session())
        claude_manager = _SessionManager(_Session())
        bindings = _BootstrapBindings()
        actor = SimpleNamespace(identity_id="control")
        selected = ExecutionRuntimeBinding(
            provider_id="anthropic",
            runtime_id="claude-code",
            capability_revision=1,
        )
        routing = _RoutingService(selected)

        service = _thread_service(
            host,
            binding_service=planner,
            session_manager=default_manager,
            bootstrap_bindings=bindings,
            control_actor=actor,
            routing_service=routing,
            session_managers={
                ("openai", "codex"): default_manager,
                ("anthropic", "claude-code"): claude_manager,
            },
            runtime_adapter_factory=lambda _binding, session: _AlternateAdapter(session),
        )

        await service.create(
            project_id="p1",
            provider_id="anthropic",
            runtime_id="claude-code",
        )

        request, _actor = routing.calls[0]
        self.assertEqual(request.allowed_provider_ids, ("anthropic",))
        self.assertEqual(request.allowed_runtime_ids, ("claude-code",))
        self.assertEqual(request.preferred_provider_ids, ("anthropic",))
        self.assertEqual(request.preferred_runtime_ids, ("claude-code",))
        self.assertFalse(request.allow_fallback)
        self.assertEqual(
            planner.calls[0]["runtime_binding"],
            selected,
        )

    async def test_create_routes_selected_non_codex_runtime_without_using_default_manager(self) -> None:
        host = _Host()
        planner = _BindingService()
        default_manager = _SessionManager(_Session())
        claude_manager = _SessionManager(_Session())
        bindings = _BootstrapBindings()
        actor = SimpleNamespace(identity_id="control")
        selected = ExecutionRuntimeBinding(
            provider_id="anthropic",
            runtime_id="claude-code",
            capability_revision=1,
        )
        routing = _RoutingService(selected)
        adapters = []

        def factory(binding, session):
            self.assertEqual(binding, selected)
            adapter = _AlternateAdapter(session)
            adapters.append(adapter)
            return adapter

        service = _thread_service(
            host,
            binding_service=planner,
            session_manager=default_manager,
            bootstrap_bindings=bindings,
            control_actor=actor,
            routing_service=routing,
            session_managers={
                ("openai", "codex"): default_manager,
                ("anthropic", "claude-code"): claude_manager,
            },
            runtime_adapter_factory=factory,
        )

        response = await service.create(project_id="p1")

        self.assertEqual(default_manager.started, [])
        self.assertEqual(claude_manager.started, ["assignment-bootstrap"])
        self.assertEqual(planner.calls[0]["runtime_binding"], selected)
        self.assertEqual(response["thread"]["id"], "claude-native-session")
        self.assertEqual(bindings.calls[0]["thread_id"], "claude-native-session")
        self.assertEqual(len(adapters), 1)
        self.assertEqual(
            adapters[0].created[0].assignment_id,
            "assignment-bootstrap",
        )
        self.assertEqual(
            host.events[-1]["runtime_binding"],
            selected.model_dump(mode="json"),
        )

    async def test_thread_start_failure_fails_assignment_and_never_uses_global_runtime(self) -> None:
        session = _Session(error=RuntimeError("start failed"))
        host, _planner, manager, bindings, service = self._service(session=session)

        with self.assertRaisesRegex(RuntimeError, "start failed"):
            await service.create(project_id="p1")

        host.codex.request.assert_not_awaited()
        self.assertEqual(bindings.calls, [])
        self.assertEqual(len(manager.completed), 1)
        assignment_id, kwargs = manager.completed[0]
        self.assertEqual(assignment_id, "assignment-bootstrap")
        self.assertFalse(kwargs["succeeded"])
        self.assertEqual(kwargs["failure_code"], "codex_thread_start_failed")

    async def test_missing_returned_thread_id_fails_assignment(self) -> None:
        session = _Session(response={"thread": {}})
        host, _planner, manager, bindings, service = self._service(session=session)

        with self.assertRaises(HTTPException) as caught:
            await service.create(project_id="p1")

        self.assertEqual(caught.exception.status_code, 502)
        host.codex.request.assert_not_awaited()
        self.assertEqual(bindings.calls, [])
        self.assertEqual(
            manager.completed[0][1]["failure_code"],
            "codex_thread_id_missing",
        )

    async def test_binding_conflict_stops_private_session_and_fails_closed(self) -> None:
        bindings = _BootstrapBindings(error=RuntimeError("binding conflict"))
        host, _planner, manager, _bindings, service = self._service(
            bindings=bindings
        )

        with self.assertRaisesRegex(RuntimeError, "binding conflict"):
            await service.create(project_id="p1")

        host.codex.request.assert_not_awaited()
        self.assertEqual(
            manager.completed[0][1]["failure_code"],
            "codex_thread_binding_failed",
        )


if __name__ == "__main__":
    unittest.main()
