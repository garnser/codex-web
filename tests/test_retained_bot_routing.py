"""Route-level retention regressions using real canonical SQLite admission/recovery."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from codex_web.models import BotBinding, ThreadRunSettings
from codex_web.services.bot_routing import BotRoutingService
from tests.slack_ingress_recovery_fixture import compose, payload


def routing_fixture(root, *, bound=True, native_error=None, interrupt_error=None, scratch=False):
    f = compose(root)
    binding = BotBinding(id="fixture-binding", connection_id=f.connection.id,
        provider="slack", external_conversation_id="C1", thread_id="fixture-thread",
        project_id="fixture", sandbox="read-only", approval_policy="on-request",
        created_at=time.time(), updated_at=time.time())
    if bound:
        f.connections.save_bindings([binding])
    settings = ThreadRunSettings(repository_resource_id=None if scratch else "restricted-repo",
        writable_repository_resource_ids=() if scratch else ("restricted-repo",),
        read_only_repository_resource_ids=() if scratch else ("read-only-repo",),
        execution_profile_id="restricted-execution", model="restricted-model")
    captured = {"binding": {"thread_id":binding.thread_id, "project_id":"fixture",
        "sandbox":"read-only", "approval_policy":"on-request"},
        "settings":settings.model_dump(mode="json"),
        "definition":{"definition_id":"execution.profiles.default", "revision":1,"record_id":"fixture-execution-record", "checksum":"a"*64,"kind":"execution_profile_catalog", "definition_schema_version":"1.0"},
        "profile":{"profile_id":"restricted-agent", "revision":7, "execution_profile_id":"restricted-execution"},
        "execution_profile":{"id":"restricted-execution", "repository_access":"none" if scratch else "selected"}}
    snapshot = f.connections.ingress_snapshot
    def authority(event, actor):
        result = snapshot(event, actor)
        result.update(routing=[captured] if bound else [], new_binding={**captured,
            "binding":{"sandbox":"read-only", "approval_policy":"on-request"}} if not bound else None)
        return result
    f.service.authority_snapshot = authority
    host = f.execution.host
    host._project = f.connections._project
    host._thread_queue_depth = lambda thread: len(f.queue.get(thread))
    host._thread_run_settings = lambda thread: ThreadRunSettings(repository_resource_id="broad-repo",
        writable_repository_resource_ids=("broad-repo",), execution_profile_id="broad-execution")
    host._remember_thread_run_settings = Mock()
    f.execution.actor_resolver = lambda identity, project: f.connections.runtime_actor(project.id)
    f.execution.start_thread_turn_now = AsyncMock(side_effect=native_error,
        return_value={"turn":{"id":"controlled-native-turn"}})
    f.execution.request_for_thread = AsyncMock(side_effect=interrupt_error, return_value={})
    f.execution.schedule_queue_drain = Mock()
    async def start(request):
        f.connections.save_bindings([binding])
        return binding
    lifecycle = SimpleNamespace(has_single_master_binding=lambda _:False,
        is_top_level_external_message=lambda _:True,
        resolve=lambda bindings,message,**kwargs:(bindings[0] if bindings else None,message.text,False),
        cross_channel_for_message=lambda *args,**kwargs:(None,args[3].text,False),
        start=AsyncMock(side_effect=start), upsert=lambda binding:binding)
    f.details = AsyncMock(return_value={"actionIntentId":"fixture-action", "status":"pending"})
    f.routing = BotRoutingService(delivery=SimpleNamespace(admit_details=f.details), connections=f.connections,
        bindings=SimpleNamespace(for_connection=lambda *args:f.connections.load_bindings(),for_project=lambda *args:[]),
        binding_lifecycle=lifecycle, targets=SimpleNamespace(remember_reply_target=lambda *args:None),
        presentation=SimpleNamespace(is_details_command=lambda text:text=="details",
            format_prompt=lambda message,provider,text:text,truncate_text=lambda text,limit:text[:limit]),
        projects=SimpleNamespace(get=f.connections._project), settings=SimpleNamespace(get=host._thread_run_settings),
        recovery=SimpleNamespace(release_stale_active_turn=lambda *args:None),
        queue_policy=SimpleNamespace(depth=host._thread_queue_depth), execution=f.execution)
    f.service.route_handler = f.routing.route_normalized
    f.lifecycle = lifecycle
    f.captured = captured
    return f


class RetainedBotRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def retain_and_route(self, f, text):
        actor = f.connections.runtime_actor("fixture")
        raw = payload()
        raw["event"]["text"] = text
        receipt = (await f.service.retain_raw("slack", f.connection.id, raw, actor=actor,
            connection_id=f.connection.id, project_id="fixture"))[0]
        event = f.service.pending_events(actor=actor, connection_id=f.connection.id, project_id="fixture")[0]
        return await f.service.ingest(event, actor=actor), event, actor

    async def test_steer_dispatches_original_scope_not_broader_thread_or_active_owner(self):
        for scratch in (False, True):
            with self.subTest(scratch=scratch), tempfile.TemporaryDirectory(dir=self.root) as directory:
                f = routing_fixture(Path(directory), scratch=scratch)
                f.execution.mark_thread_active("fixture-thread", turn_id="broader-owner-turn")
                receipt, event, actor = await self.retain_and_route(f, "steer exact original")
                f.execution.request_for_thread.assert_awaited_once_with("fixture-thread", "turn/interrupt", {"threadId":"fixture-thread"})
                f.execution.start_thread_turn_now.assert_awaited_once()
                dispatched = f.execution.start_thread_turn_now.await_args.kwargs
                self.assertEqual(dispatched["agent_profile_id"], "restricted-agent")
                self.assertEqual(dispatched["agent_profile_revision"], 7)
                self.assertEqual(dispatched["agent_profile_actor_id"], actor.identity_id)
                self.assertEqual(dispatched["actor"].identity_id, actor.identity_id)
                self.assertEqual(dispatched["execution_profile_id"], "restricted-execution")
                self.assertEqual(dispatched["repository_resource_id"], None if scratch else "restricted-repo")
                self.assertEqual(dispatched["writable_repository_resource_ids"], () if scratch else ("restricted-repo",))
                self.assertEqual(dispatched["read_only_repository_resource_ids"], () if scratch else ("read-only-repo",))
                self.assertEqual(dispatched["sandbox"], "read-only")
                remembered = f.execution.host._remember_thread_run_settings.call_args.kwargs
                self.assertEqual(remembered["writable_repository_resource_ids"], dispatched["writable_repository_resource_ids"])
                await f.service.ingest(event, actor=actor)
                f.execution.start_thread_turn_now.assert_awaited_once()

    async def test_unknown_interrupt_does_not_admit_start_or_replay_after_restart(self):
        f = routing_fixture(self.root, interrupt_error=TimeoutError("controlled uncertain interrupt"))
        f.execution.mark_thread_active("fixture-thread", turn_id="old-turn")
        receipt, event, actor = await self.retain_and_route(f, "steer exact original")
        self.assertEqual(receipt.outcome.value, "requires_reconciliation")
        self.assertEqual(f.queue.get("fixture-thread"), [])
        f.execution.start_thread_turn_now.assert_not_awaited()
        restarted = routing_fixture(self.root)
        self.assertEqual(restarted.service.pending_events(actor=actor,connection_id=f.connection.id,project_id="fixture"), ())
        await restarted.service.ingest(event, actor=actor)
        restarted.execution.request_for_thread.assert_not_awaited()
        restarted.execution.start_thread_turn_now.assert_not_awaited()

    async def test_unknown_steer_start_is_owned_by_preflight_and_never_replayed(self):
        f = routing_fixture(self.root, native_error=TimeoutError("controlled uncertain start"))
        f.execution.mark_thread_active("fixture-thread", turn_id="old-turn")
        receipt, event, actor = await self.retain_and_route(f, "steer exact original")
        attempt = f.preflight.store.for_thread("fixture-thread")[0]
        self.assertEqual(attempt.status, "reconciliation_required")
        self.assertEqual(attempt.queued_turn_snapshot["agent_profile_revision"], 7)
        restarted = routing_fixture(self.root)
        await restarted.service.ingest(event, actor=actor)
        await restarted.execution.drain_thread_queue("fixture-thread")
        restarted.execution.start_thread_turn_now.assert_not_awaited()
        self.assertEqual(restarted.preflight.store.get(attempt.id).status, "reconciliation_required")

    async def test_first_conversation_uses_existing_auto_binding_and_admits_exact_captured_scope(self):
        f = routing_fixture(self.root, bound=False)
        receipt, event, actor = await self.retain_and_route(f, "first original")
        f.lifecycle.start.assert_awaited_once()
        self.assertEqual(f.lifecycle.start.await_args.args[0].sandbox, "read-only")
        queued = f.queue.get("fixture-thread")[0]
        self.assertEqual(queued.message, "first original")
        self.assertEqual(queued.agent_profile_revision, 7)
        self.assertEqual(queued.repository_resource_id, "restricted-repo")
        await f.service.ingest(event,actor=actor)
        f.lifecycle.start.assert_awaited_once()
        self.assertEqual(len(f.queue.get("fixture-thread")), 1)

    async def test_details_remains_a_canonical_action_admission_not_a_native_turn(self):
        f = routing_fixture(self.root)
        receipt, event, actor = await self.retain_and_route(f, "details")
        f.details.assert_awaited_once()
        self.assertEqual(receipt.routing_result["actionIntentId"], "fixture-action")
        self.assertEqual(f.queue.get("fixture-thread"), [])
        await f.service.ingest(event, actor=actor)
        f.details.assert_awaited_once()
        f.execution.start_thread_turn_now.assert_not_awaited()

    async def test_missing_existing_binding_capture_has_typed_reconciliation(self):
        f = routing_fixture(self.root)
        f.captured["binding"]["thread_id"] = "other-thread"
        receipt, event, actor = await self.retain_and_route(f, "ordinary original")
        self.assertEqual(receipt.outcome.value, "requires_reconciliation")
        self.assertTrue(receipt.reason.endswith(": selected_binding_authority_unavailable"))
        f.execution.start_thread_turn_now.assert_not_awaited()

    async def test_details_real_action_store_duplicate_and_unknown_send_do_not_replay(self):
        from codex_web.action_providers import ActionProviderBindingCreate
        from codex_web.action_intents import ActionIntentClaimRequest
        from codex_web.identity import AuthenticationActor, AuthenticationAssurance, PrincipalKind
        from codex_web.services.bot_delivery import BotDeliveryService
        from codex_web.services.conversation_details_action_provider import ConversationDetailsActionProvider
        from codex_web.services.action_intents import ActionIntentService
        from codex_web.services.action_providers import ActionExecutionService, ActionProviderRegistry
        from codex_web.services.authority_roles import install_authority_roles
        from codex_web.services.definitions import DefinitionRegistryService
        from codex_web.services.resources import ResourceCatalogService
        from codex_web.storage.action_intents import ActionIntentStore
        from codex_web.storage.action_providers import ActionProviderStateStore
        from codex_web.storage.definition_registry import DefinitionRegistryStore
        from codex_web.storage.resource_catalog import ResourceCatalogStore
        f = routing_fixture(self.root)
        # This independent fixture authenticates its already authorized local
        # administrator; production runtime identity/grants remain untouched.
        f.connections.runtime_actor = lambda project:f.admin
        registry = ActionProviderRegistry(ActionProviderStateStore(f.state))
        resources = ResourceCatalogService(ResourceCatalogStore(f.state))
        definitions = DefinitionRegistryService(DefinitionRegistryStore(f.state))
        authority = install_authority_roles(definitions,resources)
        delivery = BotDeliveryService(connections=f.connections,
            thread_project_id=lambda thread:"fixture")
        send = AsyncMock(side_effect=TimeoutError("controlled provider unknown outcome"))
        delivery.send_details = send
        registry.register(ConversationDetailsActionProvider(delivery,f.connections,
            SimpleNamespace(get=f.connections._project)))
        registry.bind(ActionProviderBindingCreate(provider_type="conversation-channel",provider_instance="slack",
            project_id="fixture",credential_ref=f.connection.bot_token_secret_id),actor=f.admin,resources=resources)
        executor = ActionExecutionService(registry,resources,secret_broker=f.connections.secret_broker)
        intents = ActionIntentService(ActionIntentStore(f.state),executor,authority=authority,identity=f.identity)
        delivery.action_registry = registry
        delivery.action_intents = intents
        f.routing.delivery = delivery
        receipt,event,actor = await self.retain_and_route(f,"details")
        self.assertEqual(receipt.outcome.value,"routed",receipt.reason)
        intent_id = receipt.routing_result["actionIntentId"]
        self.assertEqual(f.service.public_receipt(receipt)["delivery_state"],"action_admitted")
        await f.service.ingest(event,actor=actor)
        self.assertEqual(len(intents.list(actor)),1)
        send.assert_not_awaited()
        worker = AuthenticationActor(identity_id="system-bot-runtime",principal_kind=PrincipalKind.SERVICE,
            organization_id=actor.organization_id,workspace_id=actor.workspace_id,
            assurance=AuthenticationAssurance.SERVICE_TOKEN,service_scopes=("action-intent:worker","secret:use"))
        claimed = intents.claim(ActionIntentClaimRequest(worker_id="fixture-worker"),actor=worker,intent_id=intent_id)
        self.assertIsNotNone(claimed)
        result = await intents.execute_claimed(intent_id,"fixture-worker",actor=worker)
        self.assertEqual(result.status.value,"uncertain")
        send.assert_awaited_once()
        self.assertIsNone(intents.claim(ActionIntentClaimRequest(worker_id="restart-worker"),actor=worker,intent_id=intent_id))
        self.assertEqual(len(intents.store.load().intents),1)
        send.assert_awaited_once()

    async def test_definition_drift_refuses_before_native_boundary(self):
        from codex_web.runtime.execution import ExecutionPrestartRefusal
        from codex_web.definitions import DefinitionReference
        f = routing_fixture(self.root)
        from codex_web.runtime.execution import TurnExecutionService
        f.execution.start_thread_turn_now = TurnExecutionService.start_thread_turn_now.__get__(f.execution)
        expected = DefinitionReference.model_validate(f.captured["definition"])
        changed = expected.model_copy(update={"revision":2,"record_id":"changed-record"})
        f.execution._require_worker_routing = lambda:(SimpleNamespace(execution_profiles=
            SimpleNamespace(resolve=lambda *args,**kwargs:(None,changed))),None)
        with self.assertRaises(ExecutionPrestartRefusal) as failure:
            await f.execution.start_thread_turn_now("fixture-thread",project=f.connections._project("fixture"),
                message="original",sandbox="read-only",approval_policy="on-request",
                execution_profile_id="restricted-execution",ingress_execution_profile_definition=expected)
        self.assertEqual(failure.exception.detail["code"],"ingress_execution_definition_changed")
        f.execution.host.codex.request.assert_not_awaited()

    async def test_unknown_auto_binding_bootstrap_keeps_claim_and_never_restarts(self):
        f = routing_fixture(self.root, bound=False)
        f.lifecycle.start.side_effect = TimeoutError("controlled bootstrap uncertain")
        receipt,event,actor = await self.retain_and_route(f,"first original")
        self.assertEqual(receipt.outcome.value,"requires_reconciliation")
        self.assertEqual(f.queue.get("fixture-thread"),[])
        restarted = routing_fixture(self.root,bound=False)
        await restarted.service.ingest(event,actor=actor)
        restarted.lifecycle.start.assert_not_awaited()
        restarted.execution.start_thread_turn_now.assert_not_awaited()

    async def test_active_ordinary_message_queues_without_interrupting(self):
        f = routing_fixture(self.root)
        f.execution.mark_thread_active("fixture-thread",turn_id="active-original-owner")
        receipt,event,actor = await self.retain_and_route(f,"ordinary original")
        queued = f.queue.get("fixture-thread")
        self.assertEqual(len(queued),1)
        self.assertEqual(queued[0].source,"slack")
        self.assertEqual(queued[0].ingress_execution_profile_definition.record_id,"fixture-execution-record")
        f.execution.request_for_thread.assert_not_awaited()
        f.execution.start_thread_turn_now.assert_not_awaited()
        await f.service.ingest(event,actor=actor)
        self.assertEqual(len(f.queue.get("fixture-thread")),1)

    async def test_steer_runs_real_start_pipeline_before_controlled_native_rpc(self):
        await self.real_native_steer(False)

    async def test_real_native_unknown_steer_never_issues_second_turn(self):
        await self.real_native_steer(True)

    async def real_native_steer(self, unknown):
        from codex_web.agent_profiles import AgentProfileExecutionBinding
        from codex_web.execution_workers import ExecutionRuntimeBinding
        from codex_web.definitions import DefinitionReference
        from codex_web.runtime.execution import TurnExecutionService
        from tests.test_turn_execution import _BindingService, _SessionManager
        f = routing_fixture(self.root)
        execution = f.execution
        execution.start_thread_turn_now = TurnExecutionService.start_thread_turn_now.__get__(execution)
        runtime = ExecutionRuntimeBinding(provider_id="openai",runtime_id="codex",capability_revision=1)
        profile = AgentProfileExecutionBinding(profile_id="restricted-agent",profile_revision=7,
            profile_record_id="restricted-agent-r7",role_id="quinn",execution_profile_id="restricted-execution",
            selected_provider_id="openai",selected_runtime_id="codex")
        definition = DefinitionReference.model_validate(f.captured["definition"])
        binding = _BindingService()
        binding.execution_profiles = SimpleNamespace(resolve=lambda *args,**kwargs:(None,definition))
        execution.binding_service = binding
        sessions = _SessionManager()
        binding.session = sessions.session
        execution.session_manager = sessions
        execution.session_managers[("openai","codex")] = sessions
        execution.control_actor = f.admin
        assignment = sessions.session.validate_current()
        assignment.project_id = "fixture"
        assignment.execution_profile_id = "restricted-execution"
        assignment.execution_profile_definition = definition
        assignment.agent_profile = profile
        assignment.runtime_binding = runtime
        assignment.repository_target = SimpleNamespace(mutable_repository_id="restricted-repo")
        assignment.repository_scope = SimpleNamespace(writable_repository_ids=("restricted-repo",),
            read_only_repository_ids=("read-only-repo",))
        sessions.session.validate_current = Mock(return_value=assignment)
        if unknown:
            native_request = sessions.session.request
            async def uncertain_request(method,params=None):
                if method=="turn/start":
                    sessions.session.requests.append((method,params))
                    raise TimeoutError("controlled native start outcome unknown")
                return await native_request(method,params)
            sessions.session.request = uncertain_request
        original_prepare = binding.prepare
        def prepare(**kwargs):
            result = original_prepare(**kwargs)
            result.execution_profile_definition = definition
            result.repository_scope = assignment.repository_scope
            result.agent_profile = profile
            return result
        binding.prepare = prepare
        execution._select_runtime_binding = AsyncMock(return_value=(runtime,profile))
        execution.mark_thread_active("fixture-thread",turn_id="broad-active-owner")
        receipt,event,actor = await self.retain_and_route(f,"steer original narrow request")
        starts = [params for method,params in sessions.session.requests if method=="turn/start"]
        self.assertEqual(len(starts),1,(receipt.reason,f.preflight.store.for_thread("fixture-thread")))
        self.assertEqual(binding.calls[0]["explicit_repository_id"],"restricted-repo")
        self.assertEqual(binding.calls[0]["writable_repository_ids"],("restricted-repo",))
        self.assertEqual(binding.calls[0]["read_only_repository_ids"],("read-only-repo",))
        self.assertEqual(binding.calls[0]["agent_profile"],profile)
        selected = execution._select_runtime_binding.await_args.kwargs
        self.assertEqual(selected["actor"].identity_id,actor.identity_id)
        self.assertEqual(selected["agent_profile_revision"],7)
        self.assertEqual(starts[0]["approvalPolicy"],"on-request")
        self.assertIn("original narrow request",starts[0]["input"][0]["text"])
        attempt = f.preflight.store.for_thread("fixture-thread")[0]
        self.assertEqual(attempt.status,"reconciliation_required" if unknown else "started")
        await f.service.ingest(event,actor=actor)
        self.assertEqual(sum(method=="turn/start" for method,_ in sessions.session.requests),1)
        restarted = routing_fixture(self.root)
        await restarted.service.ingest(event,actor=actor)
        await restarted.execution.drain_thread_queue("fixture-thread")
        restarted.execution.start_thread_turn_now.assert_not_awaited()
        self.assertEqual(restarted.preflight.store.get(attempt.id).status,attempt.status)

    async def test_auto_binding_captures_real_canonical_project_default_repository(self):
        from codex_web.services.conversation_channels import capture_repository_selection
        from codex_web.services.resources import ResourceCatalogService
        from codex_web.storage.resource_catalog import ResourceCatalogStore
        from codex_web.resources import ResourceCreate,ResourceType
        f = routing_fixture(self.root,bound=False)
        project = f.connections._project("fixture")
        resources = ResourceCatalogService(ResourceCatalogStore(f.state))
        repository = resources.create(ResourceCreate(resource_type=ResourceType.REPOSITORY,name="Original repository"),actor=f.admin)
        resources.bind_project(project=project,resource_id=repository.id,actor=f.admin)
        actor = f.connections.runtime_actor("fixture")
        original = capture_repository_selection(ThreadRunSettings(),project,
            SimpleNamespace(repository_access="selected"),resources,actor)
        self.assertEqual(original.repository_resource_id,repository.id)
        self.assertEqual(original.writable_repository_resource_ids,(repository.id,))
        f.captured["settings"] = original.model_dump(mode="json")
        receipt,event,actor = await self.retain_and_route(f,"first with canonical default")
        self.assertEqual(receipt.outcome.value,"routed",receipt.reason)
        queued = f.queue.get("fixture-thread")[0]
        self.assertEqual(queued.repository_resource_id,repository.id)
        self.assertEqual(queued.writable_repository_resource_ids,(repository.id,))
        f.lifecycle.start.assert_awaited_once()
        # Later default ambiguity cannot rewrite the already captured selection.
        another = resources.create(ResourceCreate(resource_type=ResourceType.REPOSITORY,name="Later repository"),actor=f.admin)
        resources.bind_project(project=project,resource_id=another.id,actor=f.admin)
        self.assertEqual(f.queue.get("fixture-thread")[0].writable_repository_resource_ids,(repository.id,))
        await f.service.ingest(event,actor=actor)
        self.assertEqual(len(f.queue.get("fixture-thread")),1)

    async def test_canonical_coordinated_defaults_and_scratch_remain_exact(self):
        from codex_web.services.conversation_channels import capture_repository_selection
        from codex_web.services.resources import ResourceCatalogService
        from codex_web.storage.resource_catalog import ResourceCatalogStore
        from codex_web.resources import ResourceCreate,ResourceType
        f = routing_fixture(self.root)
        project = f.connections._project("fixture").model_copy(update={"repository_selection_policy":"coordinated"})
        resources = ResourceCatalogService(ResourceCatalogStore(f.state))
        repositories = [resources.create(ResourceCreate(resource_type=ResourceType.REPOSITORY,name=f"Repo {i}"),actor=f.admin) for i in range(2)]
        for repository in repositories:
            resources.bind_project(project=project,resource_id=repository.id,actor=f.admin)
        actor = f.connections.runtime_actor("fixture")
        captured = capture_repository_selection(ThreadRunSettings(),project,
            SimpleNamespace(repository_access="selected"),resources,actor)
        self.assertEqual(set(captured.writable_repository_resource_ids),{row.id for row in repositories})
        self.assertIsNone(captured.repository_resource_id)
        scratch = capture_repository_selection(captured,project,
            SimpleNamespace(repository_access="none"),resources,actor)
        self.assertEqual(scratch.writable_repository_resource_ids,())
        self.assertEqual(scratch.read_only_repository_resource_ids,())
        self.assertIsNone(scratch.repository_resource_id)
        foreign = project.model_copy(update={"organization_id":"other-tenant"})
        with self.assertRaises(Exception):
            capture_repository_selection(ThreadRunSettings(),foreign,
                SimpleNamespace(repository_access="selected"),resources,actor)

    async def test_application_snapshot_does_not_require_unused_default_for_existing_scratch_binding(self):
        from unittest.mock import patch
        import codex_web.application as application
        from codex_web.services.resources import ResourceCatalogService
        from codex_web.storage.resource_catalog import ResourceCatalogStore
        from codex_web.definitions import DefinitionReference
        f = routing_fixture(self.root)
        project = f.connections._project("fixture")
        actor = f.connections.runtime_actor("fixture")
        resources = ResourceCatalogService(ResourceCatalogStore(f.state))
        definition = DefinitionReference.model_validate(f.captured["definition"])
        default = SimpleNamespace(repository_access="selected",model_dump=lambda **kwargs:{"id":"default","repository_access":"selected"})
        scratch = SimpleNamespace(repository_access="none",model_dump=lambda **kwargs:{"id":"scratch","repository_access":"none"})
        profiles = SimpleNamespace(resolve=lambda profile,**kwargs:(scratch if profile=="scratch" else default,definition))
        connections = SimpleNamespace(_project=lambda _:project,ingress_snapshot=lambda *args:{},
            load_bindings=f.connections.load_bindings)
        event = (await f.service.registry.resolve("slack",f.connection.id).normalize_events(
            payload(),connection_id=f.connection.id,project_id="fixture"))[0]
        selected = f.connections.load_bindings()[0]
        with patch.object(application,"bot_connection_service",connections), \
             patch.object(application,"bot_routing_service",SimpleNamespace(preview=lambda *args,**kwargs:{"bindingId":selected.id,"projectId":"fixture"})), \
             patch.object(application,"bot_binding_selection_service",SimpleNamespace(by_id=lambda _:selected)), \
             patch.object(application,"execution_profile_definition_service",profiles), \
             patch.object(application,"resource_catalog_service",resources), \
             patch.object(application,"thread_execution_settings_service",SimpleNamespace(get=lambda _:ThreadRunSettings(execution_profile_id="scratch"))), \
             patch.object(application,"_agent_profile_for_bot_binding",return_value=None):
            snapshot = application._conversation_ingress_authority(event,actor)
        self.assertIsNone(snapshot["new_binding"])
        self.assertNotIn("new_binding_error",snapshot)
        self.assertEqual(snapshot["routing"][0]["settings"]["writable_repository_resource_ids"],[])
        self.assertEqual(snapshot["routing"][0]["execution_profile"]["id"],"scratch")

    async def test_retry_after_auto_binding_preserves_original_pins_without_protection_writes(self):
        from unittest.mock import patch
        f = routing_fixture(self.root,bound=False)
        receipt,event,actor = await self.retain_and_route(f,"first original")
        original = receipt.model_dump(mode="json")
        source = f.service.authority_snapshot(event,actor)
        source.update(routing=[{**f.captured,"settings":ThreadRunSettings(
            writable_repository_resource_ids=("broader-repo",)).model_dump(mode="json")}],new_binding=None)
        f.service.authority_snapshot = lambda *args:source
        raw = payload()
        raw["event"]["text"] = "first original"
        before_keys = f.state.get("crypto_keys")
        before_governance = f.state.get("data_governance")
        with patch.object(f.crypto,"encrypt",side_effect=AssertionError("duplicate encrypted")), \
             patch.object(f.crypto,"decrypt",side_effect=AssertionError("duplicate decrypted")):
            duplicate = (await f.service.retain_raw("slack",f.connection.id,raw,actor=actor,
                connection_id=f.connection.id,project_id="fixture"))[0]
        self.assertEqual(duplicate.model_dump(mode="json"),original)
        self.assertEqual(f.state.get("crypto_keys"),before_keys)
        self.assertEqual(f.state.get("data_governance"),before_governance)
        self.assertEqual(len(f.queue.get("fixture-thread")),1)
        self.assertEqual(f.queue.get("fixture-thread")[0].writable_repository_resource_ids,("restricted-repo",))
        f.lifecycle.start.assert_awaited_once()

    async def test_retained_retry_does_not_reauthorize_pending_routing_under_changed_settings(self):
        f = routing_fixture(self.root)
        actor = f.connections.runtime_actor("fixture")
        raw = payload()
        receipt = (await f.service.retain_raw("slack",f.connection.id,raw,actor=actor,
            connection_id=f.connection.id,project_id="fixture"))[0]
        event = f.service.pending_events(actor=actor,connection_id=f.connection.id,project_id="fixture")[0]
        source = f.service.authority_snapshot(event,actor)
        source["routing"] = []
        f.service.authority_snapshot = lambda *args:source
        duplicate = (await f.service.retain_raw("slack",f.connection.id,raw,actor=actor,
            connection_id=f.connection.id,project_id="fixture"))[0]
        self.assertEqual(duplicate,receipt)
        routed = await f.service.ingest(event,actor=actor)
        self.assertEqual(routed.outcome.value,"requires_reconciliation")
        self.assertEqual(f.queue.get("fixture-thread"),[])
        f.execution.start_thread_turn_now.assert_not_awaited()
