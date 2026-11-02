from __future__ import annotations

import asyncio
import base64
import json
import socket
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
from codex_web.execution_profiles import (
    ExecutionProfileContract, validate_execution_profile_catalog,
    validate_administrative_execution_profile_catalog,
    validate_coordination_execution_profile_catalog, validate_home_execution_profile_catalog,
)
from codex_web.execution_workers import WorkerCapability, ExecutionRuntimeBinding
from codex_web.services.agent_model_egress import (
    AssignmentBoundAgentModelEgressBroker,
    AgentRuntimeModelEgressEndpoint,
)
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
from codex_web.services.turn_execution_binding import TurnExecutionBindingError
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from tests import test_turn_execution_binding as binding_fixture


def payload():
    value = execution_profile_catalog_seed_payload()
    value['profiles'].append({
        **value['profiles'][0], 'id': 'dependency-preparation',
        'name': 'Dependency preparation', 'description': 'Approved package downloads',
        'required_worker_capabilities': ['git', 'command_execution', 'network'],
        'dependency_egress': {'allowed_hosts': ['pypi.org', 'files.pythonhosted.org']},
    })
    return value


def publish(service, project_id, value=None, scope='project'):
    registry = service.registry
    draft = registry.create_draft(DefinitionDraftCreate(
        kind='execution-profile-catalog', definition_id='execution.profiles.default',
        definition_schema_version='1.4', payload=value or payload(), actor='fixture',
        scope_type=scope, scope_id=project_id if scope == 'project' else None))
    registry.publish(draft.record_id, DefinitionPublishRequest(actor='fixture'))
    return draft


class DependencyProfileTests(unittest.TestCase):
    def test_explicit_dependency_profile_is_representable_without_host_network(self):
        self.assertIn('dependency_egress', ExecutionProfileContract.model_fields)
        profile = ExecutionProfileContract.model_validate(payload()['profiles'][-1])
        self.assertFalse(profile.network_enabled)
        self.assertFalse(profile.host_mutation)

    def test_legacy_schemas_reject_opt_in_and_preserve_seed_bytes(self):
        for validator in (validate_execution_profile_catalog,
                validate_administrative_execution_profile_catalog,
                validate_coordination_execution_profile_catalog,
                validate_home_execution_profile_catalog):
            with self.subTest(validator=validator.__name__):
                self.assertEqual(validator(execution_profile_catalog_seed_payload()),
                                 execution_profile_catalog_seed_payload())
                with self.assertRaisesRegex(ValueError, 'schema 1.4'):
                    validator(payload())
                explicit_null = execution_profile_catalog_seed_payload()
                explicit_null['profiles'][0]['dependency_egress'] = None
                with self.assertRaisesRegex(ValueError, 'schema 1.4'):
                    validator(explicit_null)

    def test_project_published_policy_is_explicit_and_default_stays_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            service = install_execution_profile_definitions(DefinitionRegistryService(
                DefinitionRegistryStore(SQLiteStateStore(Path(directory) / 'state.sqlite'))))
            old, old_ref = service.resolve(None, project_id='project-a')
            publish(service, 'project-a')
            default, _ = service.resolve(None, project_id='project-a')
            profile, reference = service.resolve('dependency-preparation', project_id='project-a')
            self.assertIsNone(old.dependency_egress)
            self.assertIsNone(default.dependency_egress)
            self.assertNotEqual(reference, old_ref)
            self.assertFalse(profile.network_enabled)
            self.assertFalse(profile.host_mutation)
            self.assertEqual(profile.public()['dependencyEgress']['allowed_hosts'],
                             ['pypi.org', 'files.pythonhosted.org'])
            with self.assertRaisesRegex(ValueError, 'unknown execution profile'):
                service.resolve('dependency-preparation', project_id='project-b')
            publish(service, None, scope='global')
            with self.assertRaisesRegex(ValueError, 'exact Project-scoped'):
                service.resolve('dependency-preparation', project_id='project-b')

    def test_capability_scope_and_host_policy_cannot_be_smuggled(self):
        base = payload()['profiles'][-1]
        for changes in (
            {'required_worker_capabilities': ['git', 'command_execution']},
            {'dependency_egress': None}, {'network_enabled': True}, {'host_mutation': True},
            {'workspace_mode': 'scratch', 'repository_access': 'none'},
            {'repository_access': 'read-only'}, {'id': 'orchestration-admin'},
            {'dependency_egress': {'allowed_hosts': ['localhost']}},
            {'dependency_egress': {'allowed_hosts': ['127.0.0.1']}},
            {'dependency_egress': {'allowed_hosts': ['*.pypi.org']}},
            {'dependency_egress': {'allowed_hosts': ['pypi.org', 'PYPI.ORG']}},
            {'dependency_egress': {'allowed_hosts': []}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ExecutionProfileContract.model_validate(base | changes)

    def test_real_binding_preserves_private_namespace_and_requires_supported_inner_runtime(self):
        fixture = binding_fixture.TurnExecutionBindingTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture._publish_secret()
        publish(fixture.execution_profiles, fixture.project.id)
        prepare = lambda **kwargs: fixture.service.prepare(thread_id='thread-123', project_id=fixture.project.id, approval_policy='on-request', **kwargs)
        binding = prepare(execution_id='dependency-preparation',
            sandbox='danger-full-access', execution_profile_id='dependency-preparation')
        assignment = fixture.workers.assignment_for_execution(binding.execution_id, actor=fixture.actor)
        self.assertFalse(assignment.network.enabled)
        self.assertIn(WorkerCapability.NETWORK, assignment.required_capabilities)
        self.assertEqual(assignment.execution_profile_definition, binding.execution_profile_definition)
        for sandbox in ('workspace-write', 'read-only'):
            with self.subTest(sandbox=sandbox), self.assertRaises(TurnExecutionBindingError):
                prepare(execution_id='unsupported-' + sandbox, sandbox=sandbox,
                                 execution_profile_id='dependency-preparation')
        with self.assertRaisesRegex(TurnExecutionBindingError, 'Codex runtime'):
            prepare(execution_id='unsupported-runtime', sandbox='danger-full-access',
                execution_profile_id='dependency-preparation', runtime_binding=ExecutionRuntimeBinding(
                    provider_id='anthropic', runtime_id='claude-code', capability_revision=1))

    def test_only_separate_dependency_capability_enters_child_policy(self):
        from codex_web.services.agent_process_session import _with_dependency_proxy
        command = ('codex', '--config', 'shell_environment_policy.inherit="none"',
            '--config', 'shell_environment_policy.set={PATH="/usr/bin",HOME="/tmp/child"}')
        proxy = 'http://dependency-download:dependency-only@127.0.0.1:8789'
        actual = _with_dependency_proxy(command, proxy)
        policy = tomllib.loads(next(item for item in actual if item.startswith('shell_environment_policy.set=')))
        environment = policy['shell_environment_policy']['set']
        self.assertEqual(environment['HTTPS_PROXY'], proxy)
        self.assertEqual(environment['https_proxy'], proxy)
        self.assertEqual(environment['PATH'], '/usr/bin')
        self.assertNotIn('CODEX_ACCESS_TOKEN', environment)
        self.assertIn('shell_environment_policy.inherit="none"', actual)
        self.assertNotIn('agent-runtime', json.dumps(environment))
        with self.assertRaisesRegex(RuntimeError, 'explicit child environment'):
            _with_dependency_proxy(('other-runtime',), proxy)

    def test_actual_managed_validation_subprocess_inherits_approved_proxy_without_logging_capability(self):
        import subprocess
        import sys
        from codex_web.runtime import managed_validation_runner
        from codex_web.services.agent_process_session import _with_dependency_proxy
        proxy = 'http://dependency-download:private-test-capability@127.0.0.1:8789'
        command = _with_dependency_proxy(('codex', 'shell_environment_policy.set={PATH="/usr/bin"}'), proxy)
        environment = tomllib.loads(command[1])['shell_environment_policy']['set']
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'source.txt').write_text('attributed test input')
            reports = root / 'reports'
            reports.mkdir()
            env = {'PATH': '/usr/bin:/bin', 'CODEX_WEB_MANAGED_VALIDATION_DIR': str(reports),
                   **environment}
            result = subprocess.run([sys.executable, str(Path(managed_validation_runner.__file__).resolve()),
                '--source', 'source.txt', '--', sys.executable, '-c',
                'import os; assert os.environ["HTTPS_PROXY"] == os.environ["https_proxy"]; '
                'assert "dependency-download:" in os.environ["HTTPS_PROXY"]; '
                'assert not any(k in os.environ for k in ("CODEX_ACCESS_TOKEN","OPENAI_API_KEY","CONTAINER_HOST"))'],
                cwd=root, env=env, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            report = json.loads(next(reports.glob('report-*.json')).read_text())
            self.assertEqual(report['exit_code'], 0)
            self.assertEqual(report['state'], 'finished')
            self.assertNotIn('private-test-capability', json.dumps(report))
            self.assertEqual(result.stdout, b'')


class DependencyBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.validator = Mock()
        from codex_web.services.agent_model_egress import AssignmentBoundDependencyEgressBroker
        self.broker = AssignmentBoundDependencyEgressBroker(('pypi.org',), validator=self.validator)
        await self.broker.start()

    async def asyncTearDown(self):
        await self.broker.stop()

    async def request(self, target, capability=None, username='dependency-download'):
        reader, writer = await asyncio.open_unix_connection(str(self.broker.socket_path))
        auth = base64.b64encode(f'{username}:{capability or self.broker.capability}'.encode()).decode()
        writer.write(f'CONNECT {target} HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n'.encode())
        await writer.drain()
        result = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
        writer.close()
        await writer.wait_closed()
        return result

    async def test_separate_capability_exact_host_443_and_stale_authority_deny_before_connect(self):
        model = AssignmentBoundAgentModelEgressBroker((AgentRuntimeModelEgressEndpoint('pypi.org'),))
        self.addAsyncCleanup(model.stop)
        self.assertNotEqual(model.mount_destination, self.broker.mount_destination)
        self.assertNotEqual(model.capability, self.broker.capability)
        with patch.object(self.broker, '_open_upstream', new_callable=AsyncMock) as connect:
            for target, token, username, expected in (
                ('pypi.org:443', model.capability, 'agent-runtime', b'407'),
                ('127.0.0.1:443', None, 'dependency-download', b'403'),
                ('pypi.org:80', None, 'dependency-download', b'403'),
                ('evil.pypi.org:443', None, 'dependency-download', b'403'),
            ):
                self.assertIn(expected, await self.request(target, token, username))
            self.validator.side_effect = RuntimeError('expired fence')
            self.assertIn(b'403', await self.request('pypi.org:443'))
            connect.assert_not_awaited()

    async def test_public_resolution_is_pinned_and_private_answers_never_reach_connect(self):
        from codex_web.public_https_destination import PublicHTTPSDestinationDenied
        endpoint = AgentRuntimeModelEgressEndpoint('pypi.org')
        routes = ((socket.AF_INET, socket.IPPROTO_TCP, ('151.101.0.223', 443)),)
        with patch('codex_web.public_https_destination.resolve_public_https_destination', return_value=routes), \
             patch('asyncio.open_connection', new_callable=AsyncMock, side_effect=OSError('controlled refusal')) as connect:
            self.broker.upstream_connect_attempts = 1
            with self.assertRaises(RuntimeError):
                await self.broker._open_upstream(endpoint)
            self.assertEqual(connect.call_args.args, ('151.101.0.223', 443))
        with patch('codex_web.public_https_destination.resolve_public_https_destination',
                   side_effect=PublicHTTPSDestinationDenied('private address')), \
             patch('asyncio.open_connection', new_callable=AsyncMock) as connect:
            with self.assertRaises(PublicHTTPSDestinationDenied):
                await self.broker._open_upstream(endpoint)
            connect.assert_not_awaited()

    async def test_post_dns_authority_recheck_and_shutdown_are_owned(self):
        await self.broker._validate_current_async()
        self.validator.side_effect = RuntimeError('policy republished')
        with self.assertRaises(RuntimeError):
            await self.broker._validate_connected()
        path = self.broker.socket_path
        await self.broker.stop()
        self.assertFalse(path.exists())
        self.assertFalse(self.broker._handler_tasks)
        self.assertIsNone(self.broker.server)

    async def test_child_connection_ceiling_and_shutdown_join_all_actual_handlers(self):
        entered = asyncio.Queue()
        original = self.broker._handle
        clients = []
        async def observed(reader, writer):
            entered.put_nowait(True)
            await original(reader, writer)
        with patch.object(self.broker, '_handle', side_effect=observed):
            try:
                for _ in range(self.broker.MAX_HANDLER_TASKS):
                    reader, writer = await asyncio.open_unix_connection(str(self.broker.socket_path))
                    clients.append(writer)
                    await asyncio.wait_for(entered.get(), 2)
                self.assertEqual(len(self.broker._handler_tasks), 16)
                extra_reader, extra_writer = await asyncio.open_unix_connection(str(self.broker.socket_path))
                clients.append(extra_writer)
                self.assertEqual(await asyncio.wait_for(extra_reader.read(1), 2), b'')
                self.assertEqual(len(self.broker._handler_tasks), 16)
                self.assertEqual(self.broker.denied_connections, 1)
                self.assertTrue(entered.empty())
                await self.broker.stop()
                self.assertEqual(self.broker._handler_tasks, set())
                self.assertEqual(self.broker._writers, set())
            finally:
                for writer in clients:
                    writer.close()
                    await writer.wait_closed()


class DependencySessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_session_composes_distinct_brokers_child_policy_and_private_backend(self):
        from tests import test_codex_worker_session as session_fixture
        from codex_web.services.codex_worker_session import AssignmentBoundCodexSession
        from codex_web.execution_workers import ExecutionWorkerRegister
        from codex_web.local_execution_backend import BubblewrapExecutionBackend
        from types import SimpleNamespace
        fixture = session_fixture.AssignmentBoundCodexSessionTests()
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.asyncTearDown)
        with tempfile.TemporaryDirectory() as raw:
            profiles = install_execution_profile_definitions(DefinitionRegistryService(
                DefinitionRegistryStore(SQLiteStateStore(Path(raw) / 'profiles.sqlite'))))
            publish(profiles, 'home')
            _, reference = profiles.resolve('dependency-preparation', project_id='home')
            fixture.local_worker.execution_profiles = profiles
            worker = fixture.worker_service.register(ExecutionWorkerRegister(
                service_identity_id=fixture.worker_actor.identity_id, pool='dependency', version='test',
                capabilities=(WorkerCapability.GIT, WorkerCapability.COMMAND_EXECUTION, WorkerCapability.NETWORK)),
                actor=fixture.admin)
            fixture.local_worker.worker = worker
            assignment = fixture._create_assignment(sandbox='danger-full-access',
                execution_profile_id='dependency-preparation', execution_profile_definition=reference,
                runtime_binding=ExecutionRuntimeBinding(provider_id='openai', runtime_id='codex', capability_revision=1),
                required_capabilities=(WorkerCapability.GIT, WorkerCapability.COMMAND_EXECUTION, WorkerCapability.NETWORK))
            session = AssignmentBoundCodexSession(fixture.local_worker, SimpleNamespace(), assignment.id,
                runtime_factory=session_fixture._FakeCodexRuntime,
                egress_endpoints_resolver=lambda: (AgentRuntimeModelEgressEndpoint('api.openai.com'),),
                clock=lambda: fixture.delegation.now, monotonic=lambda: fixture.delegation.now)
            await session.start()
            try:
                launch = fixture.backend.spawned[-1]
                dependency = session.dependency_egress_broker
                model = session.egress_broker
                self.assertIsNotNone(dependency)
                self.assertFalse(session._bound_assignment.network.enabled)
                policy = tomllib.loads(next(arg for arg in launch['argv']
                    if arg.startswith('shell_environment_policy.set=')))['shell_environment_policy']['set']
                self.assertEqual(policy['HTTPS_PROXY'], dependency.proxy_url)
                self.assertEqual(launch['environment']['HTTPS_PROXY'], model.proxy_url)
                self.assertNotEqual(model.capability, dependency.capability)
                self.assertNotIn(model.capability, json.dumps(policy))
                self.assertNotIn(dependency.capability, ' '.join(session.runtime.command))
                self.assertNotIn(dependency.capability, json.dumps(session.status().public()))
                self.assertIn('[dependency-capability]', ' '.join(session.runtime.command))
                backend = BubblewrapExecutionBackend(executable='/usr/bin/bwrap',
                    probe_runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=''))
                command = backend.build_command(session._bound_assignment, argv=launch['argv'],
                    workspace_path=fixture.workspace_path,
                    trusted_readonly_mounts=launch['trusted_readonly_mounts'],
                    trusted_writable_mounts=launch['trusted_writable_mounts'])
                self.assertIn('--unshare-net', command)
                self.assertNotIn('/etc/resolv.conf', command)
                self.assertFalse(any('podman.sock' in arg for arg in command))
                self.assertNotIn('CONTAINER_HOST', launch['environment'])
                # Current policy drift revokes future tunnels and watchdog authority.
                changed = payload()
                changed['profiles'][-1]['dependency_egress']['allowed_hosts'] = ['files.pythonhosted.org']
                publish(profiles, 'home', changed)
                with self.assertRaisesRegex(RuntimeError, 'policy changed'):
                    session._validate_dependency_egress()
            finally:
                await session.stop()
            self.assertIsNone(session.dependency_egress_broker)
            self.assertFalse(dependency.socket_path.exists())


class DependencyNativeAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def prepare(self):
        import copy
        from tests import test_native_procedure_credential_custody as custody_fixture
        from tests import test_codex_worker_session as process_fixture
        from codex_web.local_execution_backend import BubblewrapExecutionBackend
        from codex_web.services.local_execution_worker import LocalExecutionWorkerRuntime
        from codex_web.services.codex_worker_session import AssignmentBoundCodexSession
        from types import SimpleNamespace
        env = custody_fixture.NativeProcedureCredentialCustodyTests()
        await env.asyncSetUp()
        self.addAsyncCleanup(env.asyncTearDown)
        f = env.f
        active = f.profiles.record(organization_id=f.actor.organization_id,
            workspace_id=f.actor.workspace_id, project_id=f.project.id)
        policy = copy.deepcopy(active.payload)
        for profile in policy['profiles']:
            if profile['id'] == 'repository-write':
                profile['dependency_egress'] = {'allowed_hosts': ['pypi.org']}
                profile['required_worker_capabilities'].append('network')
        publish(f.profiles, f.project.id, policy)
        original = env.builder.prepare_bootstrap
        def prepare(**options):
            return original(**(options | {'sandbox': 'danger-full-access'}))
        with patch.object(env.builder, 'prepare_bootstrap', side_effect=prepare):
            env.bootstrap()
        assignment = env.assignment
        workspace = f.workspaces.get(assignment.execution_workspace_id, f.actor)
        path = Path(workspace.path)
        (path / '.venv/bin').mkdir(parents=True)
        for directory in ('lib', 'lib64'):
            (path / f'.venv/{directory}/python3.14/site-packages').mkdir(parents=True)
        (path / '.venv/.codex-web-execution-venv').write_text('test\n')
        transport = process_fixture._FakeBackend()
        backend = BubblewrapExecutionBackend(executable='/usr/bin/bwrap',
            probe_runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=''))
        backend.spawn_interactive = transport.spawn_interactive
        # PID/resource observations are the process transport edge, not policy.
        backend.process_tree_rss_bytes = lambda pid: 0
        credentials = process_fixture._FakeDelegationService()
        local = LocalExecutionWorkerRuntime(f.workers, f.workspaces, backend,
            worker=env.worker, worker_actor=env.worker_actor, control_actor=f.actor,
            codex_auth_delegation=credentials, execution_profiles=f.profiles)
        session = AssignmentBoundCodexSession(local, SimpleNamespace(), assignment.id,
            credential_provider=credentials,
            runtime_factory=process_fixture._FakeCodexRuntime,
            clock=lambda: credentials.now, monotonic=lambda: credentials.now)
        await session.start()
        self.addAsyncCleanup(session.stop)
        # The broker transport is inert; its identity and current snapshot are
        # exact canonical values. Actual session/worker/workspace/profile and
        # native attestation logic remain production owners.
        control = SimpleNamespace(_stopping=False,
            server=SimpleNamespace(is_serving=lambda: True), snapshot=session._bound_assignment,
            service=SimpleNamespace(assignment_reasoning=SimpleNamespace(procedures=env.owner)),
            worker_id=session.worker_id, fence=session.fence,
            _validate_current=lambda: session.validate_current())
        session.control_plane_broker = control
        self.addAsyncCleanup(self.clear_control, session)
        env.admission.native_session_attestor = Mock(wraps=session.attest_native_provider_preparation)
        return env, session

    async def clear_control(self, session):
        session.control_plane_broker = None

    async def test_zero_candidate_preparation_and_final_input_guard_accept_declared_proxy(self):
        env, session = await self.prepare()
        f = env.f
        f.definition_registry.store.update(lambda rows: [row for row in rows if row.kind != 'agent.skill'])
        discovery, _ = await env.admit()
        self.assertTrue(discovery.admitted)
        self.assertEqual(discovery.selected, ())
        self.assertIn('dependency-egress:1.0', discovery.policy_source)
        env.admission.native_session_attestor.assert_not_called()
        current = env.current()
        self.assertIsNotNone(current.native_credential_custody.admitted_at)
        self.assertEqual(current.native_credential_custody.procedures, ())
        from tests import test_turn_execution as turn_fixture
        from codex_web.services.execution_preflight import ExecutionPreflightService
        from codex_web.storage.execution_preflight import ExecutionPreflightStore
        from types import SimpleNamespace
        host, binding, sessions, runtime = turn_fixture.TurnExecutionStartTests._service(self)
        host._project = lambda _id: f.project
        runtime.control_actor = f.actor
        runtime.actor_resolver = lambda *_args: f.actor
        binding.prepare = binding.prepare_bootstrap = lambda **kwargs: SimpleNamespace(
            assignment_id=current.id, workspace_id=current.execution_workspace_id,
            execution_id=current.execution_id)
        sessions.session.validate_current = session.validate_current
        sessions.session.status = session.status
        sessions.session.workspace_path = session.workspace_path
        sessions.local_worker = session.local_worker
        sessions.get = lambda assignment_id: sessions.session if assignment_id == current.id else None
        runtime.session_managers = {('openai', 'codex'): sessions}
        runtime.bootstrap_bindings.get_by_thread = lambda thread_id, actor: SimpleNamespace(
            bootstrap_id='credential-custody-bootstrap', thread_id=thread_id,
            execution_id=current.execution_id, assignment_id=current.id,
            execution_workspace_id=current.execution_workspace_id, initial_turn_pending=False)
        runtime.procedure_admission = env.admission
        # Existing native send protocol validates this actual session owner;
        # no artificial selected-procedure attestation is attached to no-match.
        runtime.native_input_guard_factory = lambda **kwargs: session.validate_current
        runtime.queued_preflight = ExecutionPreflightService(ExecutionPreflightStore(f.sqlite))
        runtime.schedule_queue_drain = Mock()
        original = runtime.enqueue_turn(thread_id='native-provider-thread', project_id=f.project.id,
            message='Acquire approved dependencies for local validation',
            sandbox=current.sandbox, approval_policy='on-request',
            execution_profile_id='repository-write', agent_profile_actor_id=f.actor.identity_id,
            repository_resource_id=f.repo.id,
            execution_id='dependency-original-input')
        await runtime.drain_thread_queue(original.thread_id)
        saved = runtime.queued_preflight.store.get('preflight-queued-' + original.id)
        self.assertEqual(saved.status, 'started', {'last_error': saved.last_error, 'blockers': [(b.code, b.message) for b in saved.blockers]})
        self.assertIsNotNone(saved.queued_start_receipt)
        self.assertTrue(any(method == 'turn/start' for method, _ in sessions.session.requests))
        self.assertIsNotNone(session.dependency_egress_broker)
        self.assertEqual(f.ops.calls, [])

    async def test_selected_no_egress_procedure_is_refused_by_real_session_attestor(self):
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        env, session = await self.prepare()
        # Establish that all pre-existing no-egress attestation guards pass.
        # The private fixture alone temporarily hides its inert proxy handle;
        # production never changes or weakens the attestation contract.
        dependency = session.dependency_egress_broker
        session.dependency_egress_broker = None
        try:
            session.attest_native_provider_preparation(env.current(), env.owner)
        finally:
            session.dependency_egress_broker = dependency
        with self.assertRaises(ExecutionPrestartRefusal) as caught:
            await env.admit()
        self.assertEqual(caught.exception.detail['code'], 'procedure_unsupported_enforcement')
        env.admission.native_session_attestor.assert_called()
        self.assertIsNone(env.current().native_credential_custody.admitted_at)
        self.assertEqual(env.f.ops.calls, [])


class DependencyAuthorityTests(unittest.TestCase):
    def setUp(self):
        from codex_web.agent_profiles import AgentProfileCreate
        from codex_web.services.agent_profiles import AgentProfileService
        from codex_web.storage.agent_profiles import AgentProfileStore
        from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSession
        from types import SimpleNamespace
        fixture = self.fixture = binding_fixture.TurnExecutionBindingTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture._publish_secret()
        fixture.workers.identity = fixture.identity
        publish(fixture.execution_profiles, fixture.project.id)
        self.profiles = AgentProfileService(AgentProfileStore(fixture.sqlite),
            definitions=fixture.execution_profiles.registry,
            execution_profiles=fixture.execution_profiles,
            usage_loader=lambda *_args: {'available': True, 'blocking_count': 0, 'items': []})
        fixture.execution_profiles.agent_profiles = self.profiles
        self.profile = self.profiles.create(AgentProfileCreate(profile_id='package-preparation',
            name='Preparation profile'), actor=fixture.actor)
        self.binding = fixture.service.prepare(thread_id='dep-authority', execution_id='dep-authority',
            project_id=fixture.project.id, sandbox='danger-full-access', approval_policy='on-request',
            execution_profile_id='dependency-preparation', agent_profile=self.profiles.binding_for(self.profile))
        self.assignment = fixture.workers.assignment_for_execution(self.binding.execution_id, actor=fixture.actor)
        self.session = AssignmentBoundAgentProcessSession.__new__(AssignmentBoundAgentProcessSession)
        self.session.local_worker = SimpleNamespace(worker_service=fixture.workers,
            execution_profiles=fixture.execution_profiles)

    def policy(self):
        return self.session._dependency_policy(self.assignment)

    def test_current_requester_membership_revocation_denies_dependency_policy(self):
        self.assertIsNotNone(self.policy())
        self.assertIsNone(self.assignment.resource_context)
        identity = self.fixture.identity
        membership = next(item for item in identity.state().memberships
            if item.identity_id == self.assignment.created_by and item.revoked_at is None)
        identity.revoke_membership(membership.id, actor=self.fixture.actor)
        from codex_web.services.identity import IdentityError
        with self.assertRaises(IdentityError):
            self.policy()

    def test_current_profile_lifecycle_revocation_denies_but_old_invokable_revision_remains_valid(self):
        from codex_web.agent_profiles import AgentProfileUpdate, AgentProfileLifecycle, AgentProfileLifecycleChange
        from codex_web.services.agent_profiles import AgentProfileAccessDenied
        self.assertIsNotNone(self.policy())
        # A normal newer configuration does not revoke the explicitly pinned
        # historical revision while current logical access remains active.
        current = self.profiles.update(self.profile.profile_id,
            AgentProfileUpdate(name='Updated preparation name', reason='Private configuration successor'), actor=self.fixture.actor)
        self.assertGreater(current.revision, self.profile.revision)
        self.assertIsNotNone(self.policy())
        self.profiles.lifecycle(self.profile.profile_id, AgentProfileLifecycle.DISABLED,
            AgentProfileLifecycleChange(expected_revision=current.revision, reason='Revoke future acquisition'),
            actor=self.fixture.actor)
        with self.assertRaises(AgentProfileAccessDenied):
            self.policy()

    def test_wrong_pinned_record_and_missing_profile_owner_fail_closed(self):
        from codex_web.services.agent_process_session import AssignmentBoundAgentProcessSessionStaleError
        original = self.assignment
        self.assignment = original.model_copy(update={'agent_profile': original.agent_profile.model_copy(
            update={'profile_record_id': 'unrelated-profile-record'})})
        with self.assertRaisesRegex(AssignmentBoundAgentProcessSessionStaleError, 'pin changed'):
            self.policy()
        self.assignment = original
        self.fixture.execution_profiles.agent_profiles = None
        with self.assertRaisesRegex(AssignmentBoundAgentProcessSessionStaleError, 'authority unavailable'):
            self.policy()
