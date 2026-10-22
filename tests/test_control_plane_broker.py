from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
import time
import unittest
import base64
import httpx
from contextlib import ExitStack, suppress
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch
from urllib.parse import quote

from codex_web.code_hosts import (
    CodeHostPullRequestFact,
    CodeHostRevisionMaterializationFact,
)
from codex_web.agent_profiles import AgentProfileExecutionBinding
from codex_web.action_providers import ActionRequest
from codex_web.services.code_host_action_contract import CodeHostActionContract
from codex_web.authority import (
    AUTHORITY_ROLE_CATALOG_ID,
    AUTHORITY_ROLE_CATALOG_KIND,
    AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
    AuthorityGrant,
    AuthorityDecisionOutcome,
    AuthorityLevel,
    AuthorityRoleBinding,
    AuthorityRoleCatalogDefinition,
    AuthorityRoleDefinition,
)
from codex_web.control_plane_broker import (
    ControlPlaneBrokerAuditEvent,
    ControlPlaneBrokerAuditState,
    ControlPlaneBrokerDecision,
    ControlPlaneBrokerLimits,
)
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest, DefinitionReference
from codex_web.execution_subjects import ExecutionSubject, ExecutionSubjectKind
from codex_web.execution_workers import (
    AssignmentLease,
    AssignmentStatus,
    ExecutionAssignment,
    NetworkPolicy,
    WorkerCapability,
    WorkerResourceLimits,
)
from codex_web.identity import TenantScope
from codex_web.resources import RepositoryExecutionScope, RepositoryTargetSource
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.control_plane_broker import (
    AssignmentBoundControlPlaneBroker,
    ControlPlaneBrokerDeniedError,
    ControlPlaneBrokerRequestError,
    ControlPlaneBrokerService,
    DeferredControlPlaneBrokerFactory,
)
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.identity import IdentityService
from codex_web.services.execution_workspaces import (
    ExecutionWorkspaceConflictError,
    ExecutionWorkspaceNotFoundError,
)
from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class ControlPlaneBrokerAuditStoreTests(unittest.TestCase):
    @staticmethod
    def _event(index: int) -> ControlPlaneBrokerAuditEvent:
        return ControlPlaneBrokerAuditEvent(
            id=f"audit-{index}",
            occurred_at=float(index),
            organization_id="local",
            workspace_id="default",
            execution_id="exec-1",
            assignment_id="assignment-1",
            worker_id="worker-1",
            fence=1,
            method="GET",
            path="/api/work-items/item-1",
            decision=ControlPlaneBrokerDecision.ALLOW,
            correlation_id=f"correlation-{index}",
        )

    def test_append_does_not_revalidate_retained_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ControlPlaneBrokerAuditStore(
                SQLiteStateStore(Path(directory) / "state.sqlite3")
            )
            event = self._event(1)

            with patch.object(store, "_decode", side_effect=AssertionError):
                store.append(event)

            self.assertEqual(store.load().events, [event])

    def test_append_migrates_legacy_history_to_keyed_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sqlite = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store = ControlPlaneBrokerAuditStore(sqlite)
            first = self._event(1)
            second = self._event(2)
            sqlite.update(
                store.namespace,
                lambda _current: ControlPlaneBrokerAuditState(
                    events=[first]
                ).model_dump(mode="json"),
                default={},
            )

            store.append(second)

            self.assertEqual(sqlite.get(store.namespace)["events"], [])
            self.assertEqual(sqlite.record_count(store.records_namespace), 2)
            self.assertEqual(store.load().events, [first, second])


class _StateMachine:
    def __init__(self, states):
        self.states = states

    def _work_item_state(self, ref):
        if ref not in self.states:
            raise KeyError(ref)
        return self.states[ref]


class _WorkItems:
    def __init__(self, states):
        self.state_machine = _StateMachine(states)
        self.handoffs = []

    async def list(
        self,
        *,
        project_id,
        owner,
        stage,
        release_gate,
        scope,
        q=None,
        limit=None,
        cursor=None,
    ):
        del owner, stage, release_gate, q, limit, cursor
        items = [
            {
                "ref": state.ref,
                "project_id": state.project_id,
            }
            for state in self.state_machine.states.values()
            if state.project_id == project_id
            and state.organization_id == scope.organization_id
            and state.workspace_id == scope.workspace_id
        ]
        return {"items": items, "count": len(items)}

    async def get(self, ref):
        state = self.state_machine._work_item_state(ref)
        return {"ref": state.ref, "project_id": state.project_id}

    async def handoff(self, ref, payload):
        self.handoffs.append((ref, payload))
        return {
            "ok": True,
            "item": {
                "ref": ref,
                "from_agent": payload.from_agent,
                "to_agent": payload.to_agent,
            },
        }

    async def acknowledge(self, ref, payload):
        return {"ok": True, "item": {"ref": ref, "actor": payload.actor}}

    async def progress(self, ref, payload):
        return {"ok": True, "item": {"ref": ref, "actor": payload.actor}}


class _Operator:
    def __init__(self):
        self.steers = []
        self.owner_dispatch_reads = []
        self.owner_dispatch_threads = []

    async def source_detail(self, ref):
        return {
            "snapshot": {"identity": {"external_id": ref}, "body_text": "Acceptance criteria"},
            "discussion": [{"external_id": "comment-1", "body_text": "Context"}],
        }

    def owner_dispatch_projection(self, state):
        self.owner_dispatch_reads.append(state.ref)
        self.owner_dispatch_threads.append(threading.get_ident())
        return {
            "eligible": True,
            "blocked_reason": None,
            "expected_owner": "james",
            "expected_thread_id": "thread-james",
        }

    async def retry(self, ref, *, actor, reason):
        return {"ok": True, "item": {"ref": ref, "actor": actor, "reason": reason}}

    async def reconcile(self, ref, *, actor, reason):
        return {"ok": True, "item": {"ref": ref, "actor": actor, "reason": reason}}

    async def steer(
        self,
        ref,
        *,
        expected_owner,
        expected_thread_id,
        idempotency_key,
        actor,
    ):
        self.steers.append(
            (
                ref,
                expected_owner,
                expected_thread_id,
                idempotency_key,
                actor,
            )
        )
        return {
            "status": "dispatched",
            "dispatched": True,
            "work_item_ref": ref,
            "owner": expected_owner,
            "thread_id": expected_thread_id,
            "idempotency_key": idempotency_key,
        }


class _CodeHostRegistry:
    def __init__(self) -> None:
        self.binding = None

    def provider(self, provider_type):
        from codex_web.code_hosts import CodeHostCapability
        return SimpleNamespace(capabilities=lambda: tuple(CodeHostCapability))

    def register_binding(self, binding) -> None:
        self.binding = binding


class _CodeHosts:
    def __init__(self) -> None:
        self.registry = _CodeHostRegistry()

    async def pull_request(self, binding_id, resource_id, external_id, *, actor):
        del binding_id, actor
        return CodeHostPullRequestFact(
            external_id="provider-pr-id",
            number=int(external_id),
            title="Brokered PR fact",
            state="open",
            source_ref="fix/example",
            target_ref="main",
            web_url=f"https://github.example/pulls/{external_id}",
        )

    async def pull_requests(self, binding_id, resource_id, *, actor, state="open"):
        del binding_id, resource_id, actor, state
        return (
            CodeHostPullRequestFact(
                external_id="provider-pr-id",
                number=954,
                title="Brokered PR fact",
                state="open",
                source_ref="fix/example",
                target_ref="main",
            ),
        )


class _ActionRegistry:
    def __init__(self, provider_type="github") -> None:
        self.provider_type = provider_type

    def list_bindings(self, actor):
        del actor
        return [
            SimpleNamespace(
                id=f"{self.provider_type}-action-binding",
                enabled=True,
                provider_type=self.provider_type,
                provider_instance=(
                    "github.com"
                    if self.provider_type == "github"
                    else "gitlab.example"
                ),
                project_id="project-a",
                resource_ids=("repository-a",),
                credential_ref=f"secret-{self.provider_type}",
            )
        ]

    def provider(self, provider_type, provider_instance, *, actor):
        del provider_instance, actor
        from codex_web.services.code_host_action_contract import CodeHostActionContract
        return SimpleNamespace(
            actions=lambda: CodeHostActionContract(None, None, provider_type=provider_type,
                provider_label="fixture", credential_purpose="fixture", alias_namespaces=()).actions(),
            api_base=(
                "https://api.github.com"
                if provider_type == "github"
                else "https://gitlab.example/api/v4"
            )
        )


class _ExecutingActionIntents:
    def __init__(self, registry) -> None:
        self.execution = SimpleNamespace(registry=registry)
        self.created = []
        self.claimed = []

    def create(self, payload, *, actor, assignment_id=None):
        self.created.append((payload, actor))
        return SimpleNamespace(id="action-intent-gitlab")

    def claim(self, payload, *, actor, intent_id):
        self.claimed.append((payload, actor, intent_id))
        return SimpleNamespace(id=intent_id)

    async def execute_claimed(self, intent_id, worker_id, *, actor):
        del worker_id, actor
        return SimpleNamespace(
            model_dump=lambda mode: {"id": intent_id, "status": "succeeded"}
        )


class _ActionIntents:
    def __init__(self, intent) -> None:
        self.intent = intent
        self.reconciled = []

    def get(self, intent_id, actor):
        del actor
        if intent_id != self.intent.id:
            raise KeyError(intent_id)
        return self.intent

    async def reconcile(self, intent_id, payload, *, actor):
        self.reconciled.append((intent_id, payload, actor.identity_id))
        return SimpleNamespace(
            status="pending" if payload.retry_if_idempotent else "requires_reconciliation",
            model_dump=lambda mode: {
                "id": intent_id,
                "status": "pending" if payload.retry_if_idempotent else "requires_reconciliation",
            }
        )

    def claim(self, payload, *, actor, intent_id):
        return SimpleNamespace(id=intent_id)

    async def execute_claimed(self, intent_id, worker_id, *, actor):
        return SimpleNamespace(model_dump=lambda mode: {"id": intent_id, "status": "succeeded"})


class ControlPlaneBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self, *, start_broker=True) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        self.identity = IdentityService(IdentityStateStore(self.sqlite))
        self.identity.bootstrap_local()
        self.scope = TenantScope(
            organization_id="local",
            workspace_id="default",
        )
        self.worker_actor = self.identity.bootstrap_service_actor(
            identity_id="execution-worker-test",
            name="Execution Worker Test",
            scope=self.scope,
            service_scopes=(
                "execution-worker:run",
                "action-intent:worker",
                "secret:use",
            ),
        )
        self.registry = DefinitionRegistryService(
            DefinitionRegistryStore(self.sqlite)
        )
        self.authority = install_authority_roles(self.registry)
        self._publish_worker_authority()

        self.ref = "group/app#42"
        self.other_ref = "group/other#7"
        self.states = {
            self.ref: SimpleNamespace(
                ref=self.ref,
                organization_id="local",
                workspace_id="default",
                project_id="project-a",
                resource_ids=(),
            ),
            self.other_ref: SimpleNamespace(
                ref=self.other_ref,
                organization_id="local",
                workspace_id="default",
                project_id="project-b",
                resource_ids=(),
            ),
        }
        self.work_items = _WorkItems(self.states)
        self.operator = _Operator()
        self.service = ControlPlaneBrokerService(
            identity=self.identity,
            authority=self.authority,
            work_items=self.work_items,
            operator=self.operator,
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            limits=ControlPlaneBrokerLimits(
                max_request_bytes=1024,
                # Discovery includes the publication parameter contract. Keep
                # the fixture bounded while allowing that supported response.
                max_response_bytes=32768,
                max_concurrent_requests=2,
                max_requests_per_minute=8,
            ),
        )
        self.assignment = self._assignment()
        self.current_assignment = self.assignment
        if start_broker:
            self.broker = AssignmentBoundControlPlaneBroker(
                self.service,
                assignment=self.assignment,
                worker_id="worker-1",
                worker_actor=self.worker_actor,
                fence=1,
                validator=lambda: self.current_assignment,
            )
            await self.broker.start()

    async def _assert_stop_retires_incomplete_client(self, request: bytes) -> None:
        reader, writer = await asyncio.open_unix_connection(str(self.broker.socket_path))
        writer.write(request)
        await writer.drain()
        await asyncio.sleep(0)
        shutdown = asyncio.create_task(self.broker.stop())
        try:
            await asyncio.wait_for(asyncio.shield(shutdown), 1.0)
            with suppress(ConnectionResetError):
                self.assertEqual(await asyncio.wait_for(reader.read(1), 1.0), b"")
            self.assertFalse(self.broker.socket_path.exists())
            self.assertFalse(self.broker.mount_source.exists())
            await self.broker.stop()  # Repeated cleanup owns no new operation.
        finally:
            writer.close()
            with suppress(ConnectionResetError):
                await writer.wait_closed()
            await asyncio.wait_for(shutdown, 1.0)

    async def test_stop_retires_client_waiting_for_headers(self) -> None:
        await self._assert_stop_retires_incomplete_client(b"GET /")

    async def test_stop_retires_client_waiting_for_request_body(self) -> None:
        await self._assert_stop_retires_incomplete_client(
            b"POST /v1/execute HTTP/1.1\r\nContent-Length: 2\r\n\r\n{"
        )

    async def test_stop_aborts_buffered_response_already_waiting_for_close(self) -> None:
        closing = asyncio.Event()
        accepted = []
        reader, client = await asyncio.open_unix_connection(str(self.broker.socket_path))
        client.transport.pause_reading()
        with ExitStack() as patches:
            async def buffered_response(writer, *args, **kwargs):
                accepted.append(writer)
                writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                writer.transport.set_write_buffer_limits(high=2 * 1024 * 1024)
                wait_closed = writer.wait_closed

                async def observed_close():
                    closing.set()
                    await wait_closed()

                patches.enter_context(patch.object(writer, "wait_closed", side_effect=observed_close))
                patches.enter_context(patch.object(writer.transport, "abort", wraps=writer.transport.abort))
                writer.write(b"x" * (1024 * 1024))
                await writer.drain()

            patches.enter_context(patch.object(self.broker, "_write", side_effect=buffered_response))
            client.write(b"GET /api/work-items/group%2Fapp%2342 HTTP/1.1\r\n\r\n")
            await client.drain()
            handlers = []
            try:
                await asyncio.wait_for(closing.wait(), 1.0)
                writer = accepted[0]
                handlers = list(self.broker._handler_tasks)
                self.assertTrue(writer.is_closing())
                self.assertGreater(writer.transport.get_write_buffer_size(), 0)
                self.assertIn(writer, self.broker._client_writers)
                await asyncio.wait_for(self.broker.stop(), 1.0)
                await asyncio.wait_for(asyncio.gather(*handlers), 1.0)
                writer.transport.abort.assert_called_once()
                self.assertNotIn(writer, self.broker._client_writers)
                await asyncio.sleep(0)  # Run the completed task's ownership callback.
                self.assertFalse(self.broker._handler_tasks)
            finally:
                for writer in accepted:
                    writer.transport.abort()
                client.close()
                with suppress(ConnectionResetError):
                    await client.wait_closed()
                if handlers:
                    await asyncio.wait_for(asyncio.gather(*handlers), 1.0)

    async def test_stop_does_not_cancel_or_repeat_an_inflight_dispatch(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        dispatch = self.service.dispatch
        handlers = []

        async def blocked_dispatch(**kwargs):
            handlers.append(asyncio.current_task())
            entered.set()
            await release.wait()
            return await dispatch(**kwargs)

        with patch.object(self.service, "dispatch", side_effect=blocked_dispatch) as called:
            reader, writer = await asyncio.open_unix_connection(str(self.broker.socket_path))
            writer.write(b"GET /api/work-items/group%2Fapp%2342 HTTP/1.1\r\n\r\n")
            await writer.drain()
            try:
                await asyncio.wait_for(entered.wait(), 1.0)
                await asyncio.wait_for(self.broker.stop(), 1.0)
                with suppress(ConnectionResetError):
                    self.assertEqual(await asyncio.wait_for(reader.read(1), 1.0), b"")
                self.assertFalse(handlers[0].done())
                self.assertIn(handlers[0], self.broker._handler_tasks)
                called.assert_awaited_once()
            finally:
                release.set()
                writer.close()
                with suppress(ConnectionResetError):
                    await writer.wait_closed()
                await asyncio.wait_for(handlers[0], 1.0)
            self.assertFalse(self.broker._handler_tasks)
            called.assert_awaited_once()

    async def _assert_stop_refuses_before_dispatch(self, gate_owner, gate_name) -> None:
        entered, release = threading.Event(), threading.Event()
        audited = asyncio.Event()
        loop = asyncio.get_running_loop()
        validate, audit = getattr(gate_owner, gate_name), self.service._audit

        def blocked_validator(*args, **kwargs):
            entered.set()
            if not release.wait(3.0):
                raise TimeoutError("test validator was not released")
            return validate(*args, **kwargs)

        def observed_audit(**kwargs):
            try:
                return audit(**kwargs)
            finally:
                loop.call_soon_threadsafe(audited.set)

        setattr(gate_owner, gate_name, blocked_validator)
        with (
            patch.object(self.service, "dispatch", wraps=self.service.dispatch) as called,
            patch.object(self.service, "_audit", side_effect=observed_audit),
        ):
            _reader, writer = await asyncio.open_unix_connection(str(self.broker.socket_path))
            writer.write(b"GET /api/work-items/group%2Fapp%2342 HTTP/1.1\r\n\r\n")
            await writer.drain()
            shutdown = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1.0))
                shutdown = asyncio.create_task(self.broker.stop())
                await asyncio.wait_for(asyncio.shield(shutdown), 1.0)
                release.set()
                await asyncio.wait_for(audited.wait(), 1.0)
                called.assert_not_awaited()
            finally:
                release.set()
                writer.close()
                with suppress(ConnectionResetError):
                    await writer.wait_closed()
                await asyncio.wait_for(audited.wait(), 1.0)
                if shutdown is not None:
                    await asyncio.wait_for(shutdown, 1.0)

    async def test_stop_refuses_accepted_request_in_assignment_validation(self) -> None:
        await self._assert_stop_refuses_before_dispatch(self.broker, "validator")

    async def test_stop_refuses_accepted_request_in_actor_resolution(self) -> None:
        await self._assert_stop_refuses_before_dispatch(self.service, "_actor")

    async def test_administrative_issue_creation_requires_published_target_and_preserves_execution(self):
        from codex_web.execution_profile_seed import execution_profile_catalog_seed_payload
        from codex_web.services.execution_profile_definitions import install_execution_profile_definitions
        from codex_web.resources import ResourceCreate, ResourceType
        from codex_web.services.resources import ResourceCatalogService
        from codex_web.storage.resource_catalog import ResourceCatalogStore
        resources = ResourceCatalogService(ResourceCatalogStore(self.sqlite))
        resource_a = resources.create(ResourceCreate(name="Data", resource_type=ResourceType.REPOSITORY),
                                      actor=self.identity.local_trusted_actor()).id
        resource_b = resources.create(ResourceCreate(name="Connector", resource_type=ResourceType.REPOSITORY),
                                      actor=self.identity.local_trusted_actor()).id
        self.authority.resources = resources
        profiles = install_execution_profile_definitions(self.registry)
        payload = execution_profile_catalog_seed_payload()
        payload["profiles"].append({
            "id": "orchestration-admin", "name": "Admin", "description": "Governed escalation",
            "workspace_mode": "scratch", "repository_access": "none",
            "required_worker_capabilities": ["command_execution"],
            "control_plane_operations": ["repository.issue.create", "work_graph.edge.create",
                "work_item.handoff", "work_item.acknowledge", "work_item.progress"],
            "administrative_repository_ids": [resource_a, resource_b],
        })
        record = self.registry.create_draft(DefinitionDraftCreate(
            kind="execution-profile-catalog", definition_id="execution.profiles.default",
            definition_schema_version="1.1", payload=payload, actor="fixture",
            scope_type="project", scope_id="project-a"))
        self.registry.publish(record.record_id, DefinitionPublishRequest(actor="fixture"))
        _, reference = profiles.resolve("orchestration-admin", project_id="project-a")
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "orchestration-admin", "execution_profile_definition": reference,
            "agent_profile": AgentProfileExecutionBinding(
                profile_id="orchestrator", profile_revision=8,
                profile_record_id="orchestrator-record", role_id="orchestrator",
                authority_role_id="local-admin", execution_profile_id="orchestration-admin"),
        })
        profiles.agent_profiles = SimpleNamespace(get=lambda profile_id, actor: SimpleNamespace(
            profile_id=profile_id,
            lifecycle="active", revision=8, record_id="orchestrator-record", role_id="orchestrator",
            authority_role_id="local-admin", execution_profile_id="orchestration-admin"))
        registry = _ActionRegistry("gitlab")
        original = registry.list_bindings
        def bindings(actor):
            result = original(actor)
            result[0].resource_ids = (resource_a, resource_b)
            return result
        registry.list_bindings = bindings
        actions = _ExecutingActionIntents(registry)
        self.service.action_intents = actions
        self.service.execution_profiles = profiles
        self.service.limits = self.service.limits.model_copy(update={"max_requests_per_minute": 20})
        self.broker.limits = self.service.limits
        self.current_assignment = assignment
        request = {"parameters": {"title": "Scoped prerequisite", "body": "Existing task"},
                   "idempotency_key": "admin-create-owned-prerequisite"}
        path = "/api/repository-actions/issue/create"
        for target in (path, path+"?repository_resource_id=resource-foreign"):
            status, _, _ = await self._request("POST", target, payload=request)
            self.assertIn(status, (400, 403))
        self.assertFalse(actions.created)
        status, _, result = await self._request("POST", path+"?repository_resource_id="+resource_b, payload=request)
        self.assertEqual(status, 200, result)
        created, requester = actions.created[0]
        self.assertEqual(created.request.resource_ids, (resource_b,))
        self.assertEqual(created.execution_id, assignment.execution_id)
        self.assertEqual(requester.identity_id, "local-admin")
        self.assertIsNone(assignment.repository_scope)
        self.assertFalse(assignment.resource_ids)
        inspection = self.service.public_assignment_capability(assignment)
        declared = inspection["assignment_context"]["declared_administrative_scope"]
        self.assertEqual(declared["repository_ids"], [resource_a, resource_b])
        self.assertEqual(declared["authority_role_id"], "local-admin")
        self.assertTrue(declared["definition_is_current"])
        self.assertEqual(declared["requester_identity_id"], "local-admin")
        self.assertEqual(declared["current_agent_profile"]["revision"], 8)
        self.assertEqual(declared["configuration_observation"]["status"], "valid")
        self.assertTrue(declared["effective_authority_requires_operation_checks"])
        self.assertNotIn(assignment.lease.lease_token, json.dumps(inspection))
        self.assertEqual(inspection["operations"][0]["id"], "work_graph.edge.create")
        issue_operation = next(item for item in inspection["operations"] if item["id"] == "repository.issue.create")
        self.assertEqual(issue_operation["required_query_parameters"], ["repository_resource_id"])
        status, _, _ = await self._request("POST", "/api/repository-actions/branch/publish?repository_resource_id="+resource_b, payload={})
        self.assertEqual(status, 403)
        self.assertEqual(len(actions.created), 1)
        self.states[self.ref].resource_ids = (resource_a,)
        target_ref = "group/connector#47"
        self.states[target_ref] = SimpleNamespace(ref=target_ref, organization_id="local",
            workspace_id="default", project_id="project-a", resource_ids=(resource_b,))
        calls = []
        def add_edge(payload, **kwargs):
            calls.append((payload, kwargs))
            return SimpleNamespace(model_dump=lambda mode: {"id": "edge-proof"})
        self.service.work_graph = SimpleNamespace(add_edge=add_edge)
        status, _, result = await self._request("POST", "/api/work-graph/edges", payload={
            "relation": "blocks", "source_ref": target_ref, "target_ref": self.ref})
        self.assertEqual(status, 200, result)
        self.assertEqual(calls[0][1]["project_id"], "project-a")
        self.assertEqual(calls[0][1]["actor_id"], "local-admin")
        status, _, _ = await self._request("POST", "/api/work-graph/edges", payload={
            "relation": "blocks", "source_ref": target_ref, "target_ref": self.other_ref})
        self.assertEqual(status, 403)
        self.assertEqual(len(calls), 1)
        for suffix, payload in (
            ("ack", {"actor": "quinn"}),
            ("handoff", {"from_agent": "james", "to_agent": "quinn"}),
            ("progress", {"actor": "dana", "current_owner": "james"}),
        ):
            status, _, result = await self._request("POST", "/api/work-items/"+quote(self.ref, safe="")+"/"+suffix, payload=payload)
            self.assertEqual(status, 403, result)
            self.assertIn("own agent Role attribution", result["error"]["message"])
        status, _, result = await self._request("POST", "/api/work-items/"+quote(self.ref, safe="")+"/progress", payload={})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["item"]["actor"], "orchestrator")
        audited = len(self.service.audit.load().events)
        original_get = profiles.agent_profiles.get
        for update in ({"revision":9}, {"lifecycle":"disabled"}):
            with self.subTest(update=update):
                profiles.agent_profiles.get = lambda profile_id, actor: SimpleNamespace(
                    **(vars(original_get(profile_id, actor)) | update))
                observed = self.service.public_assignment_capability(assignment)["assignment_context"]["declared_administrative_scope"]
                self.assertEqual(observed["configuration_observation"]["status"], "blocked")
                self.assertIn("disabled or its binding changed", observed["configuration_observation"]["reason"])
                for field,value in update.items():
                    self.assertEqual(observed["current_agent_profile"][field], value)
        profiles.agent_profiles.get = original_get
        expired = assignment.model_copy(update={"lease":assignment.lease.model_copy(update={"expires_at":0})})
        observed = self.service.public_assignment_capability(expired)["assignment_context"]["declared_administrative_scope"]
        self.assertIn("live fenced assignment", observed["configuration_observation"]["reason"])
        with patch.object(self.service.authority, "role_ids_for_actor", return_value=()):
            observed = self.service.public_assignment_capability(assignment)["assignment_context"]["declared_administrative_scope"]
            self.assertIn("live authority Role", observed["configuration_observation"]["reason"])
        self.assertEqual(len(self.service.audit.load().events), audited)
        self.assertEqual(len(actions.created), 1)

    async def test_authoritative_mr_read_through_real_operator_adapter_and_scoped_broker(self):
        import httpx
        from codex_web.integrations.gitlab_client import GitLabClient
        from codex_web.models import Project, TaskSourceIdentity, WorkItemState
        from codex_web.services.gitlab_task_source import GitLabTaskSource
        from codex_web.services.task_source_runtime import TaskSourceRegistry
        from codex_web.services.task_source_work_items import TaskSourceWorkItemProjector
        from codex_web.services.work_item_operator import WorkItemOperatorService
        from codex_web.services.work_item_state import WorkItemStateMachine
        from tests.test_work_item_operator import _Host, _WorkItems as OperatorWorkItems
        calls = []
        def respond(request):
            calls.append(request.url.raw_path.decode())
            return httpx.Response(200, json={"iid":42, "title":"actual MR",
                "description":"source body", "references":{"full":"group/app!42"}})
        source = GitLabTaskSource("https://gitlab.example/api/v4", "test", client=
            GitLabClient(transport=httpx.MockTransport(respond)))
        registry = TaskSourceRegistry()
        registry.register("gitlab", lambda state: source)
        host = _Host(Path(self.temp.name), Project(id="project-a", name="A", path=self.temp.name))
        machine = WorkItemStateMachine(host)
        mr_ref = "group/app!42"
        host.states[mr_ref] = WorkItemState(ref=mr_ref, organization_id="local",
            workspace_id="default", project_id="project-a", last_meaningful_update_at=1.0,
            updated_at=1.0, created_at=1.0, source_identity=
            TaskSourceIdentity(source_type="gitlab", source_instance=source.api_base,
                               external_id=mr_ref))
        self.states[mr_ref] = host.states[mr_ref]
        self.service.operator = WorkItemOperatorService(OperatorWorkItems(host, machine,
            registry, TaskSourceWorkItemProjector(host, machine)))
        status, _, body = await self._request("GET", "/api/work-items/"+quote(mr_ref,safe=""))
        self.assertEqual(status, 200)
        self.assertEqual(body["authoritative_source"]["snapshot"]["identity"]["external_id"], mr_ref)
        self.assertEqual(body["assigned_scope"]["body_text"], "source body")
        self.assertEqual(calls, ["/api/v4/projects/group%2Fapp/merge_requests/42"])
        for field, value in (("project_id", "project-b"), ("workspace_id", "foreign"),
                             ("organization_id", "foreign")):
            self.states[mr_ref] = host.states[mr_ref].model_copy(update={field:value})
            status, _, _ = await self._request("GET", "/api/work-items/"+quote(mr_ref,safe=""))
            self.assertEqual(status, 403)
        self.assertEqual(len(calls), 1)

    async def test_operator_audit_actor_is_authenticated_assignment_identity(self):
        for operation in ("retry", "reconcile"):
            with self.subTest(operation=operation):
                path = "/api/work-items/" + quote(self.ref, safe="") + "/" + operation
                status, _, body = await self._request("POST", path,
                    payload={"actor": "spoofed-admin", "reason": "verified operation"})
                self.assertEqual(status, 200)
                self.assertEqual(body["item"]["actor"], self.worker_actor.identity_id)
                self.assertEqual(body["item"]["reason"], "verified operation")
                foreign = "/api/work-items/" + quote(self.other_ref, safe="") + "/" + operation
                status, _, _ = await self._request("POST", foreign, payload={})
                self.assertEqual(status, 403)

    async def test_operator_mutations_still_require_execute_grant(self):
        self._publish_worker_authority(operator_grants=False)
        for operation in ("retry", "reconcile"):
            path = "/api/work-items/" + quote(self.ref, safe="") + "/" + operation
            status, _, body = await self._request("POST", path,
                payload={"actor": "local-admin"})
            self.assertEqual(status, 403)
            self.assertNotIn("item", body)

    async def asyncTearDown(self) -> None:
        await self.broker.stop()
        # stop retires transports without cancelling in-flight dispatch/audit.
        # Settle the existing handlers before deleting their SQLite fixtures.
        await asyncio.wait_for(asyncio.gather(*self.broker._handler_tasks), 3.0)
        self.temp.cleanup()

    def _publish_worker_authority(self, *, operator_grants=True, repository_evidence_read=False) -> None:
        active = self.registry.resolve(
            definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,
        )
        active_catalog = AuthorityRoleCatalogDefinition.model_validate(active.payload)
        catalog = AuthorityRoleCatalogDefinition(
            roles=(
                *(role for role in active_catalog.roles if role.id != "orchestration-worker"),
                AuthorityRoleDefinition(
                    id="orchestration-worker",
                    name="Orchestration worker",
                    description="Scoped broker authority for tests.",
                    grants=(
                        *((AuthorityGrant(id="repository.evidence.read", capability="repository.read",
                            level=AuthorityLevel.READ, project_ids=("project-a",)),)
                          if repository_evidence_read else ()),
                        AuthorityGrant(
                            id="orchestration.read",
                            capability="work_item.read",
                            level=AuthorityLevel.READ,
                            project_ids=("project-a",),
                        ),
                        AuthorityGrant(
                            id="orchestration.handoff",
                            capability="work_item.handoff",
                            level=AuthorityLevel.EXECUTE,
                            project_ids=("project-a",),
                        ),
                        AuthorityGrant(
                            id="orchestration.acknowledge",
                            capability="work_item.acknowledge",
                            level=AuthorityLevel.EXECUTE,
                            project_ids=("project-a",),
                        ),
                        *(
                            (
                                AuthorityGrant(
                                    id="orchestration.reconcile",
                                    capability="work_item.reconcile",
                                    level=AuthorityLevel.EXECUTE,
                                    project_ids=("project-a",),
                                ),
                                AuthorityGrant(
                                    id="orchestration.retry",
                                    capability="work_item.retry",
                                    level=AuthorityLevel.EXECUTE,
                                    project_ids=("project-a",),
                                ),
                            )
                            if operator_grants else ()
                        ),
                        AuthorityGrant(
                            id="orchestration.steer",
                            capability="work_item.steer",
                            level=AuthorityLevel.EXECUTE,
                            project_ids=("project-a",),
                        ),
                        AuthorityGrant(
                            id="repository.checks.read",
                            capability="repository.checks.read",
                            level=AuthorityLevel.READ,
                            project_ids=("project-a",),
                        ),
                        AuthorityGrant(
                            id="repository.pull-request.read",
                            capability="repository.pull-request.read",
                            level=AuthorityLevel.READ,
                            project_ids=("project-a",),
                        ),
                    ),
                ),
            ),
            bindings=(
                *(binding for binding in active_catalog.bindings if binding.id != "worker-binding"),
                AuthorityRoleBinding(
                    id="worker-binding",
                    role_id="orchestration-worker",
                    subject_kind="identity",
                    subject_id=self.worker_actor.identity_id,
                    organization_id="local",
                    workspace_id="default",
                    project_ids=("project-a",),
                ),
            ),
        )
        draft = self.registry.create_draft(
            DefinitionDraftCreate(
                definition_id=AUTHORITY_ROLE_CATALOG_ID,
                kind=AUTHORITY_ROLE_CATALOG_KIND,
                definition_schema_version=AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
                payload=catalog.model_dump(mode="json"),
                actor="test",
                reason="control-plane broker authority fixture",
            )
        )
        self.registry.approve_publication(
            draft.record_id,
            actor="test-approver",
            reference="TEST-APPROVAL",
            reason="control-plane broker authority fixture",
        )
        self.registry.publish(
            draft.record_id,
            DefinitionPublishRequest(
                actor="test",
                reason="activate control-plane broker authority fixture",
                expected_active_revision=active.revision,
            ),
        )

    @staticmethod
    def _assignment() -> ExecutionAssignment:
        now = time.time()
        return ExecutionAssignment(
            id="assignment-broker-test",
            organization_id="local",
            workspace_id="default",
            subject=ExecutionSubject(
                kind=ExecutionSubjectKind.THREAD_BOOTSTRAP,
                ref="bootstrap-broker-test",
            ),
            execution_id="execution-broker-test",
            project_id="project-a",
            resource_ids=(),
            execution_contract_version="thread-bootstrap/1.0",
            required_capabilities=(WorkerCapability.COMMAND_EXECUTION,),
            sandbox="workspace-write",
            approval_policy="on-request",
            network=NetworkPolicy(),
            limits=WorkerResourceLimits(),
            deadline_at=now + 600,
            execution_profile_id="orchestration-only",
            status=AssignmentStatus.RUNNING,
            fence=1,
            assigned_worker_id="worker-1",
            lease=AssignmentLease(
                worker_id="worker-1",
                fence=1,
                lease_token="lease-token-that-is-never-exposed",
                acquired_at=now,
                expires_at=now + 120,
            ),
            created_by="local-admin",
            created_at=now,
            updated_at=now,
            started_at=now,
        )

    async def _request(
        self,
        method: str,
        target: str,
        *,
        payload=None,
        headers=None,
    ) -> tuple[int, dict[str, str], dict]:
        reader, writer = await asyncio.open_unix_connection(
            str(self.broker.socket_path)
        )
        body = (
            json.dumps(payload, separators=(",", ":")).encode()
            if payload is not None
            else b""
        )
        request_headers = {
            "Host": "control-plane.invalid",
            "Content-Length": str(len(body)),
            **(headers or {}),
        }
        raw = (
            f"{method} {target} HTTP/1.1\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
            + "\r\n"
        ).encode() + body
        writer.write(raw)
        await writer.drain()
        try:
            response = await asyncio.wait_for(reader.read(), timeout=getattr(self, 'broker_response_timeout', 2))
        finally:
            writer.close()
            await writer.wait_closed()
        head, raw_body = response.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        parsed_headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                parsed_headers[key.casefold()] = value.strip()
        return status, parsed_headers, json.loads(raw_body or b"{}")

    async def test_history_refusal_survives_http_transport_and_audit_projection(self):
        observation = {"state": "not_ready", "target_revision": "a" * 40,
            "local_revision": "b" * 40, "maximum_depth": 4096, "deadline_seconds": 120,
            "next_action": "inspect_history_bound_or_destination_refusal"}
        failure = ControlPlaneBrokerRequestError("bounded history refusal", status_code=409,
            history_readiness=observation)
        with patch.object(self.service, "dispatch", new=AsyncMock(side_effect=failure)):
            status, _, response = await self._request("GET", "/api/work-items")
        self.assertEqual(status, 409)
        self.assertEqual(response["error"]["history_readiness"], observation)
        self.assertTrue(response["error"]["correlation_id"])
        # The existing 500-character audit reason must retain parseable details;
        # a truncated JSON string must never become a false ready observation.
        events = self.service.audit.load().events
        self.assertEqual(json.loads(events[-1].denial_reason)["history_readiness"], observation)

    async def test_work_item_list_continues_cursor_with_unchanged_filters(self) -> None:
        pages = [
            {"items": [{"ref": "group/app#42"}], "nextCursor": "opaque-cursor"},
            {"items": [{"ref": "group/app#43"}], "nextCursor": None},
        ]
        with patch.object(self.work_items, "list", new=AsyncMock(side_effect=pages)) as listing:
            target = "/api/work-items?owner=james&stage=implementation_active&q=scope%20proof&limit=1"
            first_status, _, first = await self._request("GET", target)
            second_status, _, second = await self._request(
                "GET", target + "&cursor=" + first["nextCursor"]
            )
            self.assertEqual((first_status, second_status), (200, 200))
            self.assertNotEqual(first["items"], second["items"])
            for call in listing.await_args_list:
                self.assertEqual(call.kwargs["project_id"], "project-a")
                self.assertEqual(call.kwargs["scope"], self.scope)
                self.assertEqual(call.kwargs["owner"], "james")
                self.assertEqual(call.kwargs["stage"], "implementation_active")
                self.assertEqual(call.kwargs["q"], "scope proof")
                self.assertEqual(call.kwargs["limit"], 1)
            self.assertIsNone(listing.await_args_list[0].kwargs["cursor"])
            self.assertEqual(listing.await_args_list[1].kwargs["cursor"], "opaque-cursor")

    async def test_work_item_list_rejects_non_integer_limit_before_listing(self) -> None:
        with patch.object(self.work_items, "list", new=AsyncMock()) as listing:
            status, _, response = await self._request(
                "GET", "/api/work-items?limit=invalid"
            )
            self.assertEqual(status, 400)
            self.assertIn("limit must be an integer", response["error"]["message"])
            listing.assert_not_awaited()

    async def test_merge_discovery_describes_the_canonical_merge_contract(self) -> None:
        status, _, payload = await self._request("GET", "/api/control-plane-broker/operations")
        self.assertEqual(status, 200, payload)
        operation = next(item for item in payload["operations"]
                         if item["id"] == "repository.pull-request.merge")
        schema = operation["request_body"]["parameters"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["pull_request_number"])
        self.assertEqual(set(schema["properties"]),
                         {"pull_request_number", "merge_method", "expected_head_sha"})
        self.assertEqual(schema["properties"]["pull_request_number"]["minimum"], 1)

        def parse(parameters):
            return CodeHostActionContract.pull_request_merge(ActionRequest(
                action_id="code-host.pull-request.merge", organization_id="local",
                workspace_id="default", parameters=parameters))

        minimal = {"pull_request_number": 36}
        self.assertEqual(parse(minimal)["merge_method"],
                         schema["properties"]["merge_method"]["default"])
        self.assertIsNone(parse(minimal)["expected_head_sha"])
        for method in schema["properties"]["merge_method"]["enum"]:
            with self.subTest(method=method):
                parameters = {**minimal, "merge_method": method, "expected_head_sha": "a" * 40}
                self.assertRegex(parameters["expected_head_sha"],
                                 schema["properties"]["expected_head_sha"]["pattern"])
                parsed = parse(parameters)
                self.assertEqual(parsed["number"], 36)
                self.assertEqual(parsed["merge_method"], method)
                self.assertEqual(parsed["expected_head_sha"], "a" * 40)
        for field, value in (("expected_base_sha", "b" * 40), ("expected_base_ref", "main")):
            with self.subTest(ready_only_field=field):
                self.assertNotIn(field, schema["properties"])
                with self.assertRaisesRegex(ValueError, "unsupported pull request merge parameters"):
                    parse({**minimal, "expected_head_sha": "a" * 40, field: value})

    async def test_assignment_can_list_its_broker_operation_catalog(self) -> None:
        with patch.object(
            self.service,
            "_requester_actor",
            wraps=self.service._requester_actor,
        ) as requester_actor:
            status, headers, payload = await self._request(
                "GET",
                "/api/control-plane-broker/operations",
            )

        self.assertEqual(status, 200, payload)
        requester_actor.assert_called_once_with(self.assignment)
        self.assertIn("x-correlation-id", headers)
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["project_id"], "project-a")
        self.assertEqual(payload["transport"], "assignment-bound-unix-socket")
        context = payload["assignment_context"]
        self.assertEqual(context["assignment_id"], self.assignment.id)
        self.assertEqual(context["execution_id"], self.assignment.execution_id)
        self.assertEqual(context["execution_workspace_id"], self.assignment.execution_workspace_id)
        self.assertEqual(context["execution_profile_id"], "orchestration-only")
        self.assertTrue(context["effective_authority_requires_operation_checks"])
        publication = next(item for item in payload["operations"] if item["id"] == "repository.branch.publish")
        self.assertEqual(publication["request_body"]["allowed_fields"], ["parameters", "idempotency_key"])
        self.assertEqual(publication["request_body"]["assignment_bound_parameters"], ["execution_workspace_id"])
        parameters = publication["request_body"]["parameters"]
        self.assertEqual(parameters["required"], ["branch", "head_revision"])
        self.assertEqual(parameters["properties"]["expected_remote_absent"], {"type": "boolean"})
        self.assertIn("expected_remote_revision", parameters["properties"])
        self.assertEqual(parameters["not"]["required"], ["expected_remote_revision", "expected_remote_absent"])
        self.assertIn("confirmed absent", parameters["recovery_precondition"])
        operation_ids = {item["id"] for item in payload["operations"]}
        self.assertIn("control_plane.operations.list", operation_ids)
        self.assertIn("repository.branch.publish", operation_ids)
        self.assertIn("repository.read.job-logs", operation_ids)
        self.assertIn("repository.read.artifacts", operation_ids)
        self.assertIn("repository.read.artifact-download", operation_ids)
        artifact_download = next(item for item in payload["operations"] if item["id"] == "repository.read.artifact-download")
        evidence = next(item for item in payload["operations"] if item["id"] == "repository.read.delivery-evidence")
        self.assertEqual(evidence['supported_action_ids'], [
            'code-host.branch.publish', 'code-host.pull-request.merge', 'code-host.pull-request.ready', 'code-host.pull-request.upsert'])
        self.assertTrue(evidence['inspection']['read_only'])
        self.assertTrue(evidence['inspection']['superseded_execution_receipts'])
        self.assertIn("member_path", artifact_download["optional_query_parameters"])
        self.assertIn("repository_resource_id", artifact_download["optional_query_parameters"])
        for operation in payload["operations"]:
            if operation["id"] != "repository.read.artifact-download":
                self.assertNotIn("member_path", operation.get("optional_query_parameters", []))
        self.assertIn("repository.check.rerun", operation_ids)
        self.assertIn("repository.workspace.refresh", operation_ids)
        self.assertIn("repository.workspace.publication-recovery", operation_ids)
        recovery = next(
            item for item in payload["operations"]
            if item["id"] == "repository.workspace.publication-recovery"
        )
        self.assertEqual(recovery["request_body"]["required"], ["work_item_ref"])
        self.assertIn("without changing", recovery["request_body"]["provenance"])
        self.assertIn("action_intent.reconcile", operation_ids)
        self.assertIn("work_item.steer", operation_ids)
        self.assertIn("deployment.local.status", operation_ids)
        self.assertIn("deployment.local.install", operation_ids)
        self.assertNotIn("lease_token", json.dumps(payload))
        self.assertEqual(
            payload["_broker"]["operation"],
            "control_plane.operations.list",
        )

    async def test_local_deployment_qualification_binds_exact_canonical_inputs(self) -> None:
        calls = []

        class Upgrades:
            def qualify_local_deployment(inner, plan_id, **kwargs):
                calls.append((plan_id, kwargs))
                return {"upgrade_plan_id": plan_id, **{
                    key: kwargs[key]
                    for key in (
                        "service_instance_id",
                        "source_revision",
                        "target_revision",
                        "preflight_evidence_id",
                    )
                }}

        self.service.upgrades = Upgrades()
        self.service.service_instance_id = "control-plane-test"
        actor = self.identity.local_trusted_actor()
        payload = {
            "candidate_revision": "b" * 40,
            "upgrade_plan_id": "upgrade-1",
            "preflight_evidence_id": "evidence-1",
            "source_revision": "a" * 40,
            "service_instance_id": "control-plane-test",
        }

        result = self.service._qualify_local_deployment(payload, actor=actor)

        self.assertEqual(result["target_revision"], "b" * 40)
        self.assertEqual(calls[0][0], "upgrade-1")
        self.assertIs(calls[0][1]["actor"], actor)
        stale = dict(payload, service_instance_id="old-instance")
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError, "stale"):
            self.service._qualify_local_deployment(stale, actor=actor)

    async def test_dispatch_scope_reads_yield_with_context_and_original_order(self):
        context = ContextVar("broker_scope_authority")
        marker = object()
        loop_thread = threading.get_ident()
        names = ("_actor", "_requester_actor", "_state_for_target", "_authorize")
        for blocked_name in names:
            with self.subTest(blocked_name=blocked_name):
                entered, release = threading.Event(), threading.Event()
                observations, timeouts = [], []
                originals = {name: getattr(self.service, name) for name in names}
                def wrapper(name):
                    def read(*args, **kwargs):
                        observations.append((name, threading.get_ident(), context.get()))
                        if name == blocked_name:
                            entered.set()
                            if not release.wait(2):
                                timeouts.append(name)
                        return originals[name](*args, **kwargs)
                    return read
                target = (
                    "/api/control-plane-broker/operations"
                    if blocked_name == "_requester_actor"
                    else "/api/work-items/" + quote(self.ref, safe="")
                )
                token = context.set(marker)
                with ExitStack() as patches:
                    for name in names:
                        patches.enter_context(patch.object(self.service, name, wrapper(name)))
                    task = asyncio.create_task(self.service.dispatch(
                        assignment=self.assignment, worker_actor=self.worker_actor,
                        method="GET", raw_target=target, body=b"",
                    ))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                        await asyncio.sleep(0)
                        self.assertFalse(task.done())
                        self.assertEqual(timeouts, [])
                    finally:
                        release.set()
                        result = await task
                        context.reset(token)
                self.assertEqual(result[0], 200)
                expected = (
                    ["_actor", "_requester_actor", "_authorize"]
                    if blocked_name == "_requester_actor"
                    else ["_actor", "_state_for_target", "_authorize"]
                )
                self.assertEqual([row[0] for row in observations], expected)
                for _, thread, value in observations:
                    self.assertNotEqual(thread, loop_thread)
                    self.assertIs(value, marker)

    async def test_http_actor_lookup_runs_off_event_loop(self):
        loop_thread = threading.get_ident()
        threads = []
        original = self.service._actor
        def actor(*args, **kwargs):
            threads.append(threading.get_ident())
            return original(*args, **kwargs)
        with patch.object(self.service, "_actor", actor):
            status, _, payload = await self._request("GET", "/api/work-items")
        self.assertEqual(status, 200, payload)
        # Both HTTP audit attribution and canonical dispatch resolve identities.
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(thread != loop_thread for thread in threads))

    async def test_scope_denial_precedes_authority_and_work_item_read(self):
        with patch.object(self.service, "_authorize", wraps=self.service._authorize) as authority:
            with patch.object(self.work_items, "get", new=AsyncMock()) as read:
                with self.assertRaises(ControlPlaneBrokerDeniedError):
                    await self.service.dispatch(
                        assignment=self.assignment, worker_actor=self.worker_actor,
                        method="GET", raw_target="/api/work-items/" + quote(self.other_ref, safe=""), body=b"",
                    )
                authority.assert_not_called()
                read.assert_not_awaited()
                self.assertEqual(self.operator.owner_dispatch_reads, [])

    async def test_assignment_validation_runs_off_event_loop(self) -> None:
        event_loop_thread = threading.get_ident()
        validation_threads: list[int] = []

        def validate():
            validation_threads.append(threading.get_ident())
            return self.current_assignment

        self.broker.validator = validate
        status, _, payload = await self._request(
            "GET",
            "/api/control-plane-broker/operations",
        )

        self.assertEqual(status, 200, payload)
        self.assertEqual(len(validation_threads), 1)
        self.assertNotEqual(validation_threads[0], event_loop_thread)

    def test_ci_diagnostic_routes_resolve_only_bounded_allowlisted_shapes(self) -> None:
        cases = {
            "/api/repository-facts/jobs/91/logs?max_bytes=4096": "repository.read.job-logs",
            "/api/repository-facts/runs/81/artifacts": "repository.read.artifacts",
            "/api/repository-facts/artifacts/71/download?max_bytes=4096": "repository.read.artifact-download",
        }
        for target, expected in cases.items():
            with self.subTest(target=target):
                resolved = self.service._resolve_operation("GET", target)
                self.assertEqual(resolved.operation.id, expected)
        self.assertEqual(
            self.service._resolve_operation(
                "POST", "/api/repository-actions/check/rerun"
            ).operation.id,
            "repository.check.rerun",
        )
        self.assertEqual(
            self.service._resolve_operation(
                "POST", "/api/repository-workspace/refresh"
            ).operation.id,
            "repository.workspace.refresh",
        )
        self.assertEqual(
            self.service._resolve_operation(
                "POST", "/api/repository-workspace/publication-recovery"
            ).operation.id,
            "repository.workspace.publication-recovery",
        )
        for target in (
            "/api/repository-facts/jobs/91",
            "/api/repository-facts/runs/81",
            "/api/repository-facts/artifacts/71",
        ):
            with self.subTest(target=target):
                with self.assertRaises(ControlPlaneBrokerDeniedError):
                    self.service._resolve_operation("GET", target)

    async def test_bound_worker_scopes_survive_canonical_revalidation(self) -> None:
        actor = self.service._actor(self.assignment, self.worker_actor)

        self.assertEqual(actor.identity_id, self.worker_actor.identity_id)
        self.assertIn("action-intent:worker", actor.service_scopes)
        self.assertEqual(actor.tenant, self.worker_actor.tenant)

    async def test_publication_recovery_requires_owned_issue_and_exact_repository(self) -> None:
        calls = []

        class Workspaces:
            def authorize_publication_recovery(inner, workspace_id, **kwargs):
                calls.append((workspace_id, kwargs))
                return SimpleNamespace(
                    model_dump=lambda mode: {
                        "id": workspace_id,
                        "subject": kwargs["source_subject"].model_dump(mode="json"),
                        "publication_recovery": {
                            "work_item_ref": kwargs["work_item_ref"],
                            "repository_resource_id": kwargs["repository_resource_id"],
                        },
                    }
                )

        self.service.execution_workspaces = Workspaces()
        self.service._authorize = lambda *args, **kwargs: SimpleNamespace(
            id="authority-decision-publication-recovery-test",
            definition_ref=None,
        )
        self.states[self.ref].resource_ids = ("worker-repository",)
        self.states[self.ref].current_owner = "james"
        self.states[self.ref].current_stage = "implementation_active"
        self.states[self.ref].closed_at = None
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "execution_workspace_id": "workspace-own",
            "resource_ids": ("worker-repository",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=("worker-repository",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref="worker-repository",
            ),
            "agent_profile": AgentProfileExecutionBinding(
                profile_id="james", profile_revision=6,
                profile_record_id="profile-record-james6", role_id="james",
            ),
        })
        self.current_assignment = assignment

        status, _, payload = await self._request(
            "POST", "/api/repository-workspace/publication-recovery",
            payload={"work_item_ref": self.ref},
        )

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["item"]["subject"]["kind"], "thread_bootstrap")
        workspace_id, kwargs = calls[0]
        self.assertEqual(workspace_id, "workspace-own")
        self.assertEqual(kwargs["repository_resource_id"], "worker-repository")
        self.assertEqual(kwargs["assignment_id"], assignment.id)
        self.assertEqual(kwargs["assignment_fence"], assignment.fence)
        self.assertEqual(kwargs["source_execution_id"], assignment.execution_id)
        self.assertEqual(kwargs["source_subject"], assignment.subject)

        self.states[self.ref].current_owner = "other-owner"
        status, _, denied = await self._request(
            "POST", "/api/repository-workspace/publication-recovery",
            payload={"work_item_ref": self.ref},
        )
        self.assertEqual(status, 403, denied)
        self.assertEqual(len(calls), 1)

        self.states[self.ref].current_owner = "james"
        self.states[self.ref].resource_ids = ("other-repository",)
        status, _, denied = await self._request(
            "POST", "/api/repository-workspace/publication-recovery",
            payload={"work_item_ref": self.ref},
        )
        self.assertEqual(status, 403, denied)
        self.assertEqual(len(calls), 1)

    async def _blocking_refresh(self, *, cancel=False, revoke=False):
        entered = threading.Event()
        release = threading.Event()
        mutations = []
        current = [True]
        main_thread = threading.get_ident()
        observed_threads = []

        class Workspaces:
            def get(inner, workspace_id, actor):
                return SimpleNamespace(execution_id=self.assignment.execution_id,
                    project_id=self.assignment.project_id,
                    writable_repository_ids=("repository-a",))

            def refresh_revision(inner, workspace_id, revision, **kwargs):
                observed_threads.append(threading.get_ident())
                entered.set()
                release.wait(2)  # Bounded fallback also makes old blocking code fail.
                kwargs["validate_current"]()
                mutations.append(revision)
                return SimpleNamespace(model_dump=lambda mode: {"head_revision": revision})

        self.service.execution_workspaces = Workspaces()
        self.service._authorize = lambda *a, **k: SimpleNamespace(id="allowed", definition_ref=None)
        assignment = self.assignment.model_copy(update={
            "execution_workspace_id":"workspace-a", "execution_profile_id":"repository-write",
            "repository_scope":RepositoryExecutionScope(organization_id="local",
                workspace_id="default",project_id=self.assignment.project_id,
                writable_repository_ids=("repository-a",),source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref="repository-a")})
        def validate():
            if not current[0]:
                raise ControlPlaneBrokerDeniedError("assignment revoked")
            return assignment
        task = asyncio.create_task(self.service.dispatch(assignment=assignment,
            worker_actor=self.worker_actor,method="POST",raw_target="/api/repository-workspace/refresh",
            body=json.dumps({"revision":"a"*40}).encode(),assignment_validator=validate))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertFalse(task.done(), "blocking refresh must leave the loop responsive")
            self.assertNotEqual(observed_threads, [main_thread])
            if cancel:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done(), "cancel must join this exact mutation")
            if revoke:
                current[0] = False
            release.set()
            if cancel:
                with self.assertRaises(asyncio.CancelledError):
                    await task
            elif revoke:
                with self.assertRaisesRegex(ControlPlaneBrokerDeniedError,"revoked"):
                    await task
            else:
                result = await task
                self.assertEqual(result[0],200)
            self.assertEqual(mutations, [] if cancel or revoke else ["a"*40])
        finally:
            release.set()
            with suppress(BaseException):
                await task

    async def test_blocking_workspace_refresh_keeps_broker_loop_responsive(self):
        await self._blocking_refresh()

    async def test_cancelled_workspace_refresh_joins_exact_operation_without_mutating(self):
        await self._blocking_refresh(cancel=True)

    async def test_revoked_workspace_refresh_cannot_mutate_after_wait(self):
        await self._blocking_refresh(revoke=True)

    async def test_blocking_broker_audit_does_not_block_loop_or_return_early(self):
        for denied in (False, True):
            with self.subTest(denied=denied):
                entered = threading.Event()
                release = threading.Event()
                original = self.service._audit
                main_thread = threading.get_ident()
                observed_threads = []
                def audit(**kwargs):
                    observed_threads.append(threading.get_ident())
                    entered.set()
                    release.wait(2)
                    return original(**kwargs)
                self.service._audit = audit
                task = asyncio.create_task(self._request("GET", "/not-allowlisted" if denied
                    else "/api/control-plane-broker/operations"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    self.assertNotEqual(observed_threads,[main_thread])
                    self.assertFalse(task.done(),"response must await durable audit")
                    release.set()
                    status, _, _ = await task
                    self.assertEqual(status,403 if denied else 200)
                finally:
                    release.set()
                    with suppress(BaseException):
                        await task
                    self.service._audit = original

    async def test_workspace_refresh_materializes_only_missing_exact_attested_revision(self) -> None:
        revision = "a" * 40
        checkout = Path(self.temp.name) / "workspace-refresh"
        checkout.mkdir()
        calls = []

        class Workspaces:
            def get(inner, workspace_id, actor):
                del actor
                return SimpleNamespace(
                    id=workspace_id,
                    actual_disk_bytes=0,
                    head_revision="c" * 40,
                    execution_id="execution-broker-test",
                    project_id="project-a",
                    writable_repository_ids=("repository-a",),
                    repository_members=(SimpleNamespace(
                        resource_id="repository-a",
                        access_mode=SimpleNamespace(value="write"),
                        workspace_path=str(checkout),
                    ),),
                )

            def refresh_revision(inner, workspace_id, requested, **kwargs):
                kwargs["validate_current"]()
                calls.append((workspace_id, requested, kwargs))
                if len(calls) == 1:
                    raise ExecutionWorkspaceNotFoundError(
                        "revision does not resolve to a commit"
                    )
                self.assertEqual(kwargs["materialization"].revision, revision)
                return SimpleNamespace(model_dump=lambda mode: {
                    "id": workspace_id, "head_revision": requested,
                })

        code_hosts = _CodeHosts()
        code_hosts.materialize_default_branch_revision = AsyncMock(
            return_value=CodeHostRevisionMaterializationFact(
                resource_id="repository-a",
                provider_type="github",
                provider_instance="github.com",
                repository_full_name="org/repo",
                default_branch="main",
                revision=revision,
            )
        )
        self.service.execution_workspaces = Workspaces()
        self.service.code_hosts = code_hosts
        self.service.action_intents = SimpleNamespace(
            execution=SimpleNamespace(registry=_ActionRegistry("github"))
        )
        self.service._authorize = lambda *args, **kwargs: SimpleNamespace(
            id="authority-workspace-refresh-test", definition_ref=None
        )
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "execution_workspace_id": "workspace-a",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref="repository-a",
            ),
        })
        validated = []

        status, payload, *_ = await self.service.dispatch(
            assignment=assignment,
            worker_actor=self.worker_actor,
            method="POST",
            raw_target="/api/repository-workspace/refresh",
            body=json.dumps({"revision": revision}).encode(),
            assignment_validator=lambda: validated.append(True) or assignment,
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["item"]["head_revision"], revision)
        self.assertEqual(payload["materialization"]["outcome"], "materialized")
        self.assertEqual(validated, [True, True, True])
        code_hosts.materialize_default_branch_revision.assert_awaited_once_with(
            "control-plane-read:github-action-binding",
            "repository-a",
            revision,
            checkout=checkout,
            actor=ANY,
            disk_budget_bytes=assignment.limits.disk_bytes,
            disk_quota_bytes=assignment.limits.disk_bytes,
        )

    async def test_workspace_refresh_refuses_unknown_disk_usage_before_materialization(self) -> None:
        revision = "a" * 40
        checkout = Path(self.temp.name) / "workspace-refresh-unknown-usage"
        checkout.mkdir()
        calls = []

        class Workspaces:
            def get(inner, workspace_id, actor):
                del actor
                return SimpleNamespace(
                    id=workspace_id,
                    actual_disk_bytes=None,
                    head_revision="c" * 40,
                    execution_id="execution-broker-test",
                    project_id="project-a",
                    writable_repository_ids=("repository-a",),
                    repository_members=(SimpleNamespace(
                        resource_id="repository-a",
                        access_mode=SimpleNamespace(value="write"),
                        workspace_path=str(checkout),
                    ),),
                )

            def refresh_revision(inner, workspace_id, requested, **kwargs):
                calls.append((workspace_id, requested, kwargs))
                if len(calls) == 1:
                    raise ExecutionWorkspaceNotFoundError(
                        "revision does not resolve to a commit"
                    )
                self.assertEqual(kwargs["materialization"].revision, revision)
                return SimpleNamespace(model_dump=lambda mode: {
                    "id": workspace_id, "head_revision": requested,
                })

        code_hosts = _CodeHosts()
        code_hosts.materialize_default_branch_revision = AsyncMock(
            return_value=CodeHostRevisionMaterializationFact(
                resource_id="repository-a",
                provider_type="github",
                provider_instance="github.com",
                repository_full_name="org/repo",
                default_branch="main",
                revision=revision,
            )
        )
        self.service.execution_workspaces = Workspaces()
        self.service.code_hosts = code_hosts
        self.service.action_intents = SimpleNamespace(
            execution=SimpleNamespace(registry=_ActionRegistry("github"))
        )
        self.service._authorize = lambda *args, **kwargs: SimpleNamespace(
            id="authority-workspace-refresh-test", definition_ref=None
        )
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "execution_workspace_id": "workspace-a",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref="repository-a",
            ),
        })
        validated = []

        with self.assertRaises(ControlPlaneBrokerRequestError) as refused:
            await self.service.dispatch(
                assignment=assignment,
                worker_actor=self.worker_actor,
                method="POST",
                raw_target="/api/repository-workspace/refresh",
                body=json.dumps({"revision": revision}).encode(),
                assignment_validator=lambda: validated.append(True) or assignment,
            )

        self.assertEqual(refused.exception.status_code, 409)
        self.assertIn("disk usage unavailable", str(refused.exception))
        self.assertEqual(refused.exception.history_readiness["state"], "not_ready")
        self.assertEqual(len(calls), 1, "unknown usage must not permit a final reset")
        code_hosts.materialize_default_branch_revision.assert_not_awaited()
        self.assertEqual(validated, [])

    async def test_workspace_refresh_preserves_dirty_and_stale_fence_guards(self) -> None:
        revision = "b" * 40
        checkout = Path(self.temp.name) / "workspace-refresh-denied"
        checkout.mkdir()

        class Workspaces:
            conflict = False
            calls = 0

            def get(inner, workspace_id, actor):
                del actor
                return SimpleNamespace(
                    id=workspace_id,
                    actual_disk_bytes=0,
                    head_revision="c" * 40,
                    execution_id="execution-broker-test",
                    project_id="project-a",
                    writable_repository_ids=("repository-a",),
                    repository_members=(SimpleNamespace(
                        resource_id="repository-a",
                        access_mode=SimpleNamespace(value="write"),
                        workspace_path=str(checkout),
                    ),),
                )

            def refresh_revision(inner, workspace_id, requested, **kwargs):
                inner.calls += 1
                if inner.conflict:
                    raise ExecutionWorkspaceConflictError(
                        "execution workspace has uncommitted changes"
                    )
                if inner.calls == 1:
                    raise ExecutionWorkspaceNotFoundError(
                        "revision does not resolve to a commit"
                    )
                raise AssertionError("stale assignment must block final refresh")

        workspaces = Workspaces()
        code_hosts = _CodeHosts()
        code_hosts.materialize_default_branch_revision = AsyncMock(
            return_value=CodeHostRevisionMaterializationFact(
                resource_id="repository-a",
                provider_type="github",
                provider_instance="github.com",
                repository_full_name="org/repo",
                default_branch="main",
                revision=revision,
            )
        )
        self.service.execution_workspaces = workspaces
        self.service.code_hosts = code_hosts
        self.service.action_intents = SimpleNamespace(
            execution=SimpleNamespace(registry=_ActionRegistry("github"))
        )
        self.service._authorize = lambda *args, **kwargs: SimpleNamespace(
            id="authority-workspace-refresh-denials", definition_ref=None
        )
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "execution_workspace_id": "workspace-a",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref="repository-a",
            ),
        })

        # Actual mutable admitted assignment facts change while the awaited
        # materializer runs. The post-await validator must reject both cases
        # before reset; credential/provider/Resource cases live in facade tests.
        for invalidation in ("fence", "lease"):
            with self.subTest(invalidation=invalidation):
                workspaces.calls = 0
                current = assignment.model_copy(deep=True)
                async def materialize(*args, **kwargs):
                    nonlocal current
                    if invalidation == "fence":
                        current = current.model_copy(update={"fence": assignment.fence + 1})
                    else:
                        current = current.model_copy(update={"lease": current.lease.model_copy(update={"expires_at": time.time() - 1})})
                    return CodeHostRevisionMaterializationFact(resource_id="repository-a",provider_type="github",
                        provider_instance="github.com",repository_full_name="org/repo",default_branch="main",revision=revision)
                # Reuse the real broker snapshot/fence/lease validator rather
                # than a test-specific permission predicate.
                def validate():
                    with (patch.object(self.broker, "validator", return_value=current),
                            patch.object(self.broker, "snapshot", assignment)):
                        return self.broker._validate_current()
                code_hosts.materialize_default_branch_revision.side_effect = materialize
                with self.assertRaisesRegex(ControlPlaneBrokerDeniedError, "fence|lease"):
                    await self.service.dispatch(assignment=assignment,worker_actor=self.worker_actor,
                        method="POST",raw_target="/api/repository-workspace/refresh",
                        body=json.dumps({"revision": revision}).encode(),assignment_validator=validate)
                self.assertEqual(workspaces.calls, 1)

        workspaces.conflict = True
        observed_materializations = code_hosts.materialize_default_branch_revision.await_count
        with self.assertRaisesRegex(ControlPlaneBrokerRequestError, "uncommitted"):
            await self.service.dispatch(
                assignment=assignment,
                worker_actor=self.worker_actor,
                method="POST",
                raw_target="/api/repository-workspace/refresh",
                body=json.dumps({"revision": revision}).encode(),
            )
        self.assertEqual(
            code_hosts.materialize_default_branch_revision.await_count,
            observed_materializations,
        )

    async def test_authorized_read_and_handoff_use_exact_role_authority(self) -> None:
        event_loop_thread = threading.get_ident()
        encoded = quote(self.ref, safe="")
        status, headers, payload = await self._request(
            "GET",
            f"/api/work-items/{encoded}",
            headers={
                "X-Correlation-ID": "corr-read-1",
                "X-Causation-ID": "cause-root-1",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["ref"], self.ref)
        self.assertEqual(
            payload["assigned_scope"]["source"],
            "authoritative_task_source",
        )
        self.assertEqual(
            payload["assigned_scope"]["body_text"],
            "Acceptance criteria",
        )
        self.assertEqual(
            payload["owner_dispatch"],
            {
                "eligible": True,
                "blocked_reason": None,
                "expected_owner": "james",
                "expected_thread_id": "thread-james",
            },
        )
        self.assertEqual(self.operator.owner_dispatch_reads, [self.ref])
        self.assertNotEqual(
            self.operator.owner_dispatch_threads[0],
            event_loop_thread,
        )
        self.assertEqual(headers["x-correlation-id"], "corr-read-1")
        self.assertEqual(payload["_broker"]["operation"], "work_item.read")

        handoff_status, _, handoff = await self._request(
            "POST",
            f"/api/work-items/{encoded}/handoff",
            payload={
                "from_agent": "Orchestrator",
                "to_agent": "development",
                "reason": "implementation ready",
            },
        )
        self.assertEqual(handoff_status, 200)
        self.assertTrue(handoff["ok"])
        self.assertEqual(len(self.work_items.handoffs), 1)

        audit = self.service.audit.load().events
        self.assertEqual([item.decision.value for item in audit], ["allow", "allow"])
        self.assertEqual(audit[0].correlation_id, "corr-read-1")
        self.assertEqual(audit[0].causation_id, "cause-root-1")
        self.assertEqual(audit[0].actor_identity_id, self.worker_actor.identity_id)
        self.assertIsNotNone(audit[0].authority_definition)
        self.assertEqual(audit[0].authority_decision_id.startswith("authority-"), True)

    async def test_large_read_keeps_live_coordination_and_owner_dispatch(self) -> None:
        current = {
            "ref": self.ref,
            "current_owner": "nora",
            "implementation_owner": "nora",
            "validation_owner": "quinn",
            "handoff": {"status": "pending", "to_agent": "orchestrator"},
            "handoff_history": [{"status": "accepted", "acknowledged_at": 123}],
            "notes": ["historical context" * 40000],
            "execution": {"latest_checkpoint": {"context": "x" * 600000}},
        }
        with patch.object(self.work_items, "get", AsyncMock(return_value=current)):
            status, _, payload = await self._request(
                "GET", f"/api/work-items/{quote(self.ref, safe='')}"
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["handoff"], current["handoff"])
        self.assertEqual(payload["implementation_owner"], "nora")
        self.assertEqual(payload["validation_owner"], "quinn")
        self.assertEqual(payload["recent_handoffs"][0]["acknowledged_at"], 123)
        self.assertEqual(payload["owner_dispatch"]["expected_thread_id"], "thread-james")
        self.assertIn("assigned_scope", payload)
        self.assertIn("execution", payload["_projection"]["omitted_fields"])
        self.assertNotIn("execution", payload)
        self.assertLess(len(json.dumps(payload).encode()), self.service.limits.max_response_bytes)
        # Projection does not delete canonical history.
        self.assertIn("notes", current)

    async def test_coordination_view_omits_small_history_explicitly(self) -> None:
        current = {"ref": self.ref, "notes": ["old note"], "handoff": None}
        with patch.object(self.work_items, "get", AsyncMock(return_value=current)):
            status, _, payload = await self._request(
                "GET", f"/api/work-items/{quote(self.ref, safe='')}?view=coordination"
            )
        self.assertEqual(status, 200)
        self.assertNotIn("notes", payload)
        self.assertIsNone(payload["handoff"])
        self.assertEqual(payload["_projection"]["view"], "coordination")

    async def test_large_committed_ack_preserves_success_and_actual_receipt(self) -> None:
        receipt = {
            "ok": True,
            "item": {
                "ref": self.ref,
                "current_owner": "orchestrator",
                "handoff": {"status": "accepted", "acknowledged_at": 456},
                "notes": ["x" * 600000],
            },
        }
        ack = AsyncMock(return_value=receipt)
        with patch.object(self.work_items, "acknowledge", ack):
            status, _, payload = await self._request(
                "POST", f"/api/work-items/{quote(self.ref, safe='')}/ack",
                payload={"actor": "orchestrator", "accepted": True},
            )
        self.assertEqual(status, 200)
        ack.assert_awaited_once()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["item"]["handoff"]["acknowledged_at"], 456)
        self.assertEqual(payload["_broker"]["operation"], "work_item.acknowledge")
        self.assertNotIn("notes", payload["item"])

    async def test_coordination_view_preserves_scope_denial_and_byte_limit(self) -> None:
        denied, _, _ = await self._request(
            "GET", f"/api/work-items/{quote(self.other_ref, safe='')}?view=coordination"
        )
        self.assertEqual(denied, 403)
        current = {"ref": self.ref, "next_action": "x" * 20000}
        with patch.object(self.work_items, "get", AsyncMock(return_value=current)):
            status, _, payload = await self._request(
                "GET", f"/api/work-items/{quote(self.ref, safe='')}?view=coordination"
            )
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["code"], "broker_response_too_large")

    async def test_projection_retry_uses_real_service_and_rechecks_live_assignment_fence(self):
        from tests.test_github_progress_projection import GitHubProgressProjectionTests
        from codex_web.models import WorkItemProgressUpdate
        from codex_web.services.work_item_operator import WorkItemOperatorService
        fixture = GitHubProgressProjectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        state = fixture.repo.get(fixture.state.ref)
        state.project_id = "project-a"
        fixture.repo.put(state.ref, state)
        state = fixture.machine._structured_progress(state.ref,
            WorkItemProgressUpdate(next_action="qualified next action"))
        self.service.work_items = fixture.service
        self.service.operator = WorkItemOperatorService(fixture.service)
        fixture.hook = lambda: setattr(self, "current_assignment",
            self.current_assignment.model_copy(update={"fence": 2}))
        status, _, response = await self._request("POST",
            "/api/work-items/" + quote(state.ref, safe="") + "/projection-retry",
            payload={"expected_revision": state.source_projection.desired_revision})
        self.assertEqual(status, 200, response)
        self.assertEqual(response["item"]["source_projection"]["status"], "conflict")
        self.assertEqual(response["item"]["source_projection"]["reason"], "projection_authority_changed")
        self.assertFalse(any(method != "GET" for method, _, _ in fixture.calls))
        self.assertEqual(fixture.repo.get(state.ref).current_owner, "vp-engineering")
        self.assertEqual(self.service.audit.load().events[-1].operation_id, "work_item.projection_retry")

    async def test_owner_steer_uses_bounded_preconditions_and_exact_authority(self) -> None:
        encoded = quote(self.ref, safe="")
        status, _, payload = await self._request(
            "POST",
            f"/api/work-items/{encoded}/steer",
            payload={
                "expected_owner": "james",
                "expected_thread_id": "thread-james",
                "idempotency_key": "owner-wakeup:42:1",
            },
        )

        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["dispatched"])
        self.assertEqual(payload["_broker"]["operation"], "work_item.steer")
        self.assertEqual(
            self.operator.steers,
            [
                (
                    self.ref,
                    "james",
                    "thread-james",
                    "owner-wakeup:42:1",
                    self.worker_actor.identity_id,
                )
            ],
        )

    async def test_owner_steer_rejects_unbounded_payload_before_dispatch(self) -> None:
        status, _, payload = await self._request(
            "POST",
            f"/api/work-items/{quote(self.ref, safe='')}/steer",
            payload={
                "expected_owner": "james",
                "expected_thread_id": "thread-james",
                "idempotency_key": "owner-wakeup:42:1",
                "message": "mutate an arbitrary thread",
            },
        )

        self.assertEqual(status, 422, payload)
        self.assertEqual(self.operator.steers, [])

    async def test_non_allowlisted_arbitrary_localhost_and_method_mismatch_fail_closed(self) -> None:
        status, _, payload = await self._request(
            "GET",
            "http://127.0.0.1:8765/api/identity/me",
        )
        self.assertEqual(status, 403)
        self.assertIn("not allowed", payload["error"]["message"])

        status, _, payload = await self._request(
            "DELETE",
            f"/api/work-items/{quote(self.ref, safe='')}",
        )
        self.assertEqual(status, 405)
        self.assertIn("not allowed", payload["error"]["message"])

    async def test_project_scope_stale_fence_and_revoked_identity_fail_closed(self) -> None:
        status, _, payload = await self._request(
            "GET",
            f"/api/work-items/{quote(self.other_ref, safe='')}",
        )
        self.assertEqual(status, 403)
        self.assertIn("project scope", payload["error"]["message"])

        self.current_assignment = self.assignment.model_copy(
            update={"fence": 2}
        )
        status, _, payload = await self._request(
            "GET",
            f"/api/work-items/{quote(self.ref, safe='')}",
        )
        self.assertEqual(status, 403)
        self.assertIn("fence changed", payload["error"]["message"])

        self.current_assignment = self.assignment

        def disable(state):
            state.services = [
                item.model_copy(update={"disabled_at": time.time()})
                if item.id == self.worker_actor.identity_id
                else item
                for item in state.services
            ]
            return state

        self.identity.store.update(disable)
        status, _, payload = await self._request(
            "GET",
            f"/api/work-items/{quote(self.ref, safe='')}",
        )
        self.assertEqual(status, 403)
        self.assertIn("service identity", payload["error"]["message"])

    async def test_rate_and_payload_limits_are_deterministic(self) -> None:
        service = ControlPlaneBrokerService(
            identity=self.identity,
            authority=self.authority,
            work_items=self.work_items,
            operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            limits=ControlPlaneBrokerLimits(
                max_request_bytes=1024,
                max_response_bytes=4096,
                max_concurrent_requests=1,
                max_requests_per_minute=1,
            ),
        )
        limited = AssignmentBoundControlPlaneBroker(
            service,
            assignment=self.assignment,
            worker_id="worker-1",
            worker_actor=self.worker_actor,
            fence=1,
            validator=lambda: self.assignment,
        )
        await limited.start()
        original = self.broker
        self.broker = limited
        try:
            status, _, _ = await self._request(
                "GET",
                f"/api/work-items/{quote(self.ref, safe='')}",
            )
            self.assertEqual(status, 200)
            status, _, payload = await self._request(
                "GET",
                f"/api/work-items/{quote(self.ref, safe='')}",
            )
            self.assertEqual(status, 429)
            self.assertIn("rate limit", payload["error"]["message"])
        finally:
            self.broker = original
            await limited.stop()

        reader, writer = await asyncio.open_unix_connection(
            str(self.broker.socket_path)
        )
        writer.write(
            (
                f"POST /api/work-items/{quote(self.ref, safe='')}/handoff HTTP/1.1\r\n"
                "Host: control-plane.invalid\r\n"
                "Content-Length: 1025\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), timeout=2)
        writer.close()
        await writer.wait_closed()
        self.assertIn(b" 413 ", response)

    async def test_no_reusable_broker_or_admin_credential_is_exposed(self) -> None:
        public = self.service.public_assignment_capability(self.assignment)
        self.assertTrue(public["enabled"])
        self.assertFalse(public["credential_exposed"])
        serialized = json.dumps(public)
        self.assertNotIn("lease-token-that-is-never-exposed", serialized)
        self.assertNotIn("secret", serialized.casefold())

        status, _, payload = await self._request(
            "GET",
            f"/api/work-items/{quote(self.ref, safe='')}",
        )
        self.assertEqual(status, 200)
        self.assertNotIn("lease-token-that-is-never-exposed", json.dumps(payload))
        self.assertNotIn(
            "lease-token-that-is-never-exposed",
            json.dumps(
                [
                    item.model_dump(mode="json")
                    for item in self.service.audit.load().events
                ]
            ),
        )

    async def test_discovery_exposes_declared_scope_and_pinned_profile_without_authority(self):
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "execution_workspace_id": "workspace-own",
            "resource_ids": ("worker-repository", "core-repository"),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=("worker-repository",),
                read_only_repository_ids=("core-repository",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref="worker-repository",
            ),
            "agent_profile": AgentProfileExecutionBinding(
                profile_id="james", profile_revision=6,
                profile_record_id="profile-record-james6", role_id="james",
            ),
            "secret_refs": ("must-never-be-disclosed",),
        })
        context = self.service.public_assignment_capability(assignment)["assignment_context"]
        self.assertEqual(context["execution_workspace_id"], "workspace-own")
        self.assertEqual(context["agent_profile"], {
            "profile_id": "james", "profile_revision": 6,
            "profile_record_id": "profile-record-james6", "role_id": "james",
        })
        self.assertEqual(context["declared_repository_scope"], {
            "writable_repository_ids": ["worker-repository"],
            "read_only_repository_ids": ["core-repository"],
        })
        self.assertTrue(context["effective_authority_requires_operation_checks"])
        public = self.service.public_assignment_capability(assignment)
        self.assertNotIn("must-never-be-disclosed", json.dumps(public))
        self.assertNotIn(assignment.lease.lease_token, json.dumps(public))
        disabled = self.service.public_assignment_capability(assignment.model_copy(
            update={"execution_profile_id": "unsupported-profile"}))
        self.assertFalse(disabled["enabled"])
        self.assertIsNone(disabled["assignment_context"])
        self.assertEqual(disabled["operations"], [])

    async def test_catalog_reports_exact_pinned_execution_profile_definition_without_lookup(self):
        for profile_id in ('repository-write', 'orchestration-only'):
            for revision in (1, 3):
                with self.subTest(profile=profile_id, revision=revision):
                    reference = DefinitionReference(
                        definition_id='execution.profiles.project', kind='execution-profile-catalog',
                        definition_schema_version='1.0', revision=revision,
                        record_id=f'profile-definition-{revision}', checksum=str(revision) * 64)
                    assignment = self.assignment.model_copy(update={
                        'execution_profile_id': profile_id,
                        'execution_profile_definition': reference,
                    })
                    with patch.object(self.registry, 'get_record', side_effect=AssertionError('must not resolve current state')):
                        context = self.service.public_assignment_capability(assignment)['assignment_context']
                    self.assertEqual(context['execution_profile_definition'], reference.model_dump(mode='json'))
                    self.assertTrue(context['effective_authority_requires_operation_checks'])
                    disabled = self.service.public_assignment_capability(assignment.model_copy(
                        update={'execution_profile_id': 'unsupported-profile'}))
                    self.assertIsNone(disabled['assignment_context'])
        legacy = self.assignment.model_copy(update={'execution_profile_definition': None})
        self.assertIsNone(self.service.public_assignment_capability(legacy)
                          ['assignment_context']['execution_profile_definition'])

    async def test_catalog_guided_work_item_ack_reaches_existing_service(self):
        status, _, catalog = await self._request("GET", "/api/control-plane-broker/operations")
        self.assertEqual(status, 200)
        operations = {item["id"]: item for item in catalog["operations"]}
        for operation, required in (("work_item.handoff", {"from_agent", "to_agent"}),
                                    ("work_item.acknowledge", {"actor"}),
                                    ("work_item.progress", set())):
            schema = operations[operation]["request_body"]
            self.assertEqual(set(schema.get("required", [])), required)
            self.assertIn("current_stage", schema["properties"])
        schema = operations["work_item.acknowledge"]["request_body"]
        self.assertIn("expected_handoff_requested_at", schema["properties"])
        self.assertTrue(schema["properties"]["accepted"]["default"])
        stages = next(item["enum"] for item in schema["properties"]["current_stage"]["anyOf"]
                      if "enum" in item)
        self.assertIn("validation_running", stages)
        self.assertNotIn("validation_active", stages)
        request = {"actor": "quinn", "accepted": True,
                   "expected_handoff_requested_at": 123.0, "current_stage": "validation_running"}
        with patch.object(self.work_items, "acknowledge", wraps=self.work_items.acknowledge) as ack:
            status, _, response = await self._request(
                "POST", f"/api/work-items/{quote(self.ref, safe='')}/ack", payload=request)
        self.assertEqual(status, 200, response)
        ack.assert_awaited_once()
        actual = ack.call_args.args[1]
        self.assertEqual(actual.actor, "quinn")
        self.assertEqual(actual.expected_handoff_requested_at, 123.0)
        self.assertEqual(actual.current_stage, "validation_running")

    async def test_work_item_schema_errors_still_refuse_before_service(self):
        with patch.object(self.work_items, "acknowledge", new=AsyncMock()) as ack:
            for request in ({"agent": "quinn", "handoff_requested_at": 123.0},
                            {"actor": "quinn", "current_stage": "validation_active"}):
                with self.subTest(request=request):
                    status, _, _ = await self._request(
                        "POST", f"/api/work-items/{quote(self.ref, safe='')}/ack", payload=request)
                    self.assertEqual(status, 422)
            ack.assert_not_awaited()

    async def test_operator_and_worker_catalogs_share_repository_request_metadata(self):
        from fastapi import FastAPI
        from codex_web.api.control_plane_broker import build_control_plane_broker_router
        app = FastAPI()
        app.include_router(build_control_plane_broker_router(self.service))
        with patch("codex_web.api.control_plane_broker._operator_actor", return_value=self.worker_actor):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                result = await client.get("/api/control-plane-broker/operations")
        self.assertEqual(result.status_code, 200)
        self.assertEqual([item for item in result.json()["items"]
                          if not item["id"].startswith("project.")
                          and item["id"] not in {"repository.pull-request.ready", "operator.host.inspect"}],
                         self.service.public_assignment_capability(self.assignment)["operations"])
        # Engine recognition in the operator catalog is not an opt-in declaration.
        self.assertTrue(any(item["id"] == "operator.host.inspect" for item in result.json()["items"]))
        self.assertFalse(any(item["id"] == "operator.host.inspect" for item in
                             self.service.public_assignment_capability(self.assignment)["operations"]))
        self.assertFalse(any(item["id"] == "repository.pull-request.ready" for item in
                             self.service.public_assignment_capability(self.assignment)["operations"]))
        self.assertFalse(any(item["id"].startswith("project.") for item in
                             self.service.public_assignment_capability(self.assignment)["operations"]))
        self.assertNotIn("assignment_context", result.json())

    async def test_repository_write_assignment_receives_same_scoped_broker(self) -> None:
        assignment = self.assignment.model_copy(
            update={"execution_profile_id": "repository-write"}
        )
        public = self.service.public_assignment_capability(assignment)
        self.assertTrue(public["enabled"])
        self.assertFalse(public["credential_exposed"])

        status, payload, operation, target_ref, _decision = await self.service.dispatch(
            assignment=assignment,
            worker_actor=self.worker_actor,
            method="GET",
            raw_target=f"/api/work-items/{quote(self.ref, safe='')}",
            body=b"",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["ref"], self.ref)
        self.assertEqual(operation.id, "work_item.read")
        self.assertEqual(target_ref, self.ref)

        factory = DeferredControlPlaneBrokerFactory()
        factory.configure(self.service)
        broker = await factory.start(
            assignment=assignment,
            worker_id="worker-1",
            worker_actor=self.worker_actor,
            fence=1,
            validator=lambda: assignment,
        )
        self.assertIsNotNone(broker)
        assert broker is not None
        try:
            self.assertEqual(broker.sandbox_url, "http://127.0.0.1:8788")
        finally:
            await broker.stop()

    async def test_repository_write_assignment_can_read_pull_request_fact(self) -> None:
        code_hosts = _CodeHosts()
        self.authority.resources = SimpleNamespace(
            get=lambda resource_id, actor: SimpleNamespace(
                id=resource_id,
                lifecycle="active",
                resource_type="repository",
                risk="medium",
                sensitivity="internal",
            )
        )
        service = ControlPlaneBrokerService(
            identity=self.identity,
            authority=self.authority,
            work_items=self.work_items,
            operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            action_intents=SimpleNamespace(
                execution=SimpleNamespace(registry=_ActionRegistry())
            ),
            code_hosts=code_hosts,
        )
        assignment = self.assignment.model_copy(
            update={
                "execution_profile_id": "repository-write",
                "resource_ids": ("repository-a",),
                "repository_scope": RepositoryExecutionScope(
                    organization_id="local",
                    workspace_id="default",
                    project_id="project-a",
                    writable_repository_ids=("repository-a",),
                    source=RepositoryTargetSource.SINGLE_REPOSITORY,
                    source_ref="repository-a",
                ),
            }
        )

        status, payload, operation, target_ref, decision = await service.dispatch(
            assignment=assignment,
            worker_actor=self.worker_actor,
            method="GET",
            raw_target="/api/repository-facts/pull-requests/954",
            body=b"",
        )

        self.assertEqual(status, 200)
        self.assertEqual(operation.id, "repository.read.pull-request")
        self.assertEqual(target_ref, "954")
        self.assertEqual(payload["item"]["number"], 954)
        self.assertEqual(payload["item"]["state"], "open")
        self.assertEqual(decision.outcome, AuthorityDecisionOutcome.ALLOW)
        self.assertEqual(
            code_hosts.registry.binding.credential_ref,
            "secret-github",
        )

    async def test_gitlab_artifacts_use_assignment_resource_and_credential_boundary(self) -> None:
        from codex_web.code_hosts import CodeHostError, CodeHostUnsupportedCapabilityError
        from codex_web.resources import ResourceCreate, ResourceProvenance, ResourceType
        from codex_web.services.resources import ResourceCatalogService
        from codex_web.storage.resource_catalog import ResourceCatalogStore
        from codex_web.services.code_hosts import CodeHostService, CodeHostRegistry
        from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
        from codex_web.services.github_code_host import GitHubCodeHostProvider
        from codex_web.integrations.gitlab_client import GitLabClient
        from codex_web.integrations.github_client import GitHubClient
        from tests.test_code_hosts import _SecretBroker

        resources = ResourceCatalogService(ResourceCatalogStore(self.sqlite))
        resource = resources.create(
            ResourceCreate(
                name="acme/widgets", resource_type=ResourceType.REPOSITORY,
                provenance=ResourceProvenance(
                    provider="gitlab", provider_instance="gitlab.example", external_id="42",
                ),
            ), actor=self.identity.local_trusted_actor(),
        )
        self.authority.resources = resources
        calls = []
        job = {
            "id": 19025, "pipeline": {"id": 3394, "project_id": 42},
            "artifacts_file": {"filename": "preview.zip", "size": 400000},
        }
        def transport(request):
            calls.append(request)
            self.assertEqual(request.headers["PRIVATE-TOKEN"], "provider-secret")
            path = request.url.path
            if path == "/api/v4/projects/99":
                return httpx.Response(200, json={"id": 99, "name": "core"})
            if path == "/api/v4/projects/99/jobs/19025":
                return httpx.Response(200, json={**job, "pipeline": {"id": 3394, "project_id": 99}})
            if path == "/api/v4/projects/99/jobs/19025/artifacts":
                return httpx.Response(200, content=b"immutable-core-wheel", headers={"content-type": "application/zip"})
            if path == "/api/v4/projects/42":
                return httpx.Response(200, json={"id": 42, "name": "widgets"})
            if path == "/api/v4/projects/42/pipelines/3394/jobs":
                return httpx.Response(200, json=[job])
            if path == "/api/v4/projects/42/jobs/19025":
                return httpx.Response(200, json=job)
            if path == "/api/v4/projects/42/jobs/19025/artifacts":
                return httpx.Response(200, content=b"PK\x03\x04archive", headers={"content-type": "application/zip"})
            if path == "/api/v4/projects/42/jobs/19025/artifacts/html/index.html":
                return httpx.Response(200, content=b"<html>preview</html>", headers={"content-type": "text/html"})
            return httpx.Response(404)
        registry = CodeHostRegistry()
        registry.register_provider(GitLabCodeHostProvider(GitLabClient(transport=httpx.MockTransport(transport))))
        secret_broker = _SecretBroker()
        code_hosts = CodeHostService(registry, resources, secrets=secret_broker)
        action_registry = _ActionRegistry("gitlab")
        original_bindings = action_registry.list_bindings
        def bindings(actor):
            items = original_bindings(actor)
            items[0].resource_ids = (resource.id,)
            return items
        action_registry.list_bindings = bindings
        service = ControlPlaneBrokerService(
            identity=self.identity, authority=self.authority, work_items=self.work_items,
            operator=_Operator(), audit=ControlPlaneBrokerAuditStore(self.sqlite),
            action_intents=SimpleNamespace(execution=SimpleNamespace(registry=action_registry)),
            code_hosts=code_hosts,
        )
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write", "resource_ids": (resource.id,),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=(resource.id,), source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref=resource.id,
            ),
        })
        async def read(path, selected=assignment):
            return await service.dispatch(
                assignment=selected, worker_actor=self.worker_actor,
                method="GET", raw_target=path, body=b"",
            )
        status, payload, *_ = await read("/api/repository-facts/runs/3394/artifacts")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["external_id"], "19025")
        status, payload, *_ = await read("/api/repository-facts/artifacts/19025/download?max_bytes=327680")
        self.assertEqual(base64.b64decode(payload["item"]["content_base64"]), b"PK\x03\x04archive")
        status, payload, *_ = await read("/api/repository-facts/artifacts/19025/download?max_bytes=8&member_path=html%2Findex.html")
        self.assertEqual(base64.b64decode(payload["item"]["content_base64"]), b"<html>pr")
        self.assertEqual(payload["item"]["media_type"], "text/html")
        self.assertTrue(payload["item"]["truncated"])
        self.assertEqual([call[0] for call in secret_broker.calls], ["secret-gitlab"] * 3)
        self.assertTrue(all(call[1] == self.worker_actor.identity_id for call in secret_broker.calls))
        self.assertNotIn("provider-secret", json.dumps(payload))
        self.assertNotIn("provider-secret", json.dumps(service.audit.load().model_dump(mode="json")))
        observed = len(calls)
        for path, error in (
            ("?member_path=../index.html", CodeHostError),
            ("?member_path=%2Findex.html", CodeHostError),
            ("?member_path=html%5Cindex.html", CodeHostError),
            ("?member_path=html%00index.html", CodeHostError),
            ("?member_path=", CodeHostError),
            ("?member_path=a&member_path=b", ControlPlaneBrokerRequestError),
            ("?max_bytes=327681", ControlPlaneBrokerRequestError),
        ):
            with self.assertRaises(error):
                await read("/api/repository-facts/artifacts/19025/download" + path)
        self.assertEqual(len(calls), observed)
        denied = assignment.model_copy(update={"execution_profile_id": "orchestration-only"})
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await read("/api/repository-facts/artifacts/19025/download", denied)
        cross_tenant = assignment.model_copy(update={"workspace_id": "other"})
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await read("/api/repository-facts/artifacts/19025/download", cross_tenant)
        dependency = resources.create(
            ResourceCreate(
                name="acme/core", resource_type=ResourceType.REPOSITORY,
                provenance=ResourceProvenance(
                    provider="gitlab", provider_instance="gitlab.example", external_id="99",
                ),
            ), actor=self.identity.local_trusted_actor(),
        )
        # The dependency has its own provider binding; primary repository metadata
        # cannot establish its package provenance.
        def scoped_bindings(actor):
            items = bindings(actor)
            items.append(SimpleNamespace(
                **{**vars(items[0]), "id": "gitlab-core-action-binding",
                   "resource_ids": (dependency.id,)},
            ))
            return items
        action_registry.list_bindings = scoped_bindings
        consumer = assignment.model_copy(update={
            "resource_ids": (resource.id, dependency.id),
            "repository_scope": assignment.repository_scope.model_copy(update={
                "read_only_repository_ids": (dependency.id,),
            }),
        })
        dependency_start = len(calls)
        with patch.object(self.authority, "evaluate", wraps=self.authority.evaluate) as authorize:
            status, payload, *_ = await read(
                "/api/repository-facts/artifacts/19025/download?repository_resource_id=" + dependency.id,
                consumer,
            )
        self.assertEqual(status, 200)
        self.assertEqual(base64.b64decode(payload["item"]["content_base64"]), b"immutable-core-wheel")
        self.assertEqual(authorize.call_args.args[0].resource_ids, (dependency.id,))
        self.assertTrue(all(c.url.path == "/api/v4/projects/99" or c.url.path.startswith("/api/v4/projects/99/") for c in calls[dependency_start:]))
        self.assertNotIn("provider-secret", json.dumps(payload))
        # Selection never widens the assignment's immutable scope, even when a
        # resource is otherwise visible to the requester.
        count = len(calls)
        for selected in [assignment, consumer.model_copy(update={
            "repository_scope": consumer.repository_scope.model_copy(update={"workspace_id": "other"}),
        })]:
            with self.assertRaises(ControlPlaneBrokerDeniedError):
                await read("/api/repository-facts/artifacts/19025/download?repository_resource_id=" + dependency.id, selected)
        for value in ["", "a&repository_resource_id=b"]:
            with self.assertRaises(ControlPlaneBrokerRequestError):
                await read("/api/repository-facts/artifacts/19025/download?repository_resource_id=" + value, consumer)
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await read("/api/repository-facts/artifacts/19025/download?repository_resource_id=" + dependency.id,
                       consumer.model_copy(update={"execution_profile_id": "orchestration-only"}))
        with self.assertRaises(ControlPlaneBrokerRequestError):
            await service.dispatch(
                assignment=consumer, worker_actor=self.worker_actor, method="POST",
                raw_target="/api/repository-actions/branch/publish?repository_resource_id=" + dependency.id,
                body=b"{}",
            )
        self.assertEqual(len(calls), count)
        # No selector still targets the existing writable repository.
        status, payload, *_ = await read("/api/repository-facts/artifacts/19025/download", consumer)
        self.assertEqual(base64.b64decode(payload["item"]["content_base64"]), b"PK\x03\x04archive")
        self.assertEqual(secret_broker.calls[-1][0], "secret-gitlab")
        secret_count = len(secret_broker.calls)
        # Unsupported optional member reads fail before secret resolution; whole-archive
        # behavior remains exercised by the existing GitHub adapter regressions.
        github = GitHubCodeHostProvider(GitHubClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
        registry.register_provider(github)
        from codex_web.code_hosts import CodeHostProviderBinding, CodeHostCapability
        github_resource = resources.create(
            ResourceCreate(
                name="acme/widgets", resource_type=ResourceType.REPOSITORY,
                provenance=ResourceProvenance(provider="github", provider_instance="github.com", external_id="42"),
            ), actor=self.identity.local_trusted_actor(),
        )
        registry.register_binding(CodeHostProviderBinding(
            id="github-read", organization_id="local", workspace_id="default",
            provider_type="github", provider_instance="github.com", base_url="https://api.github.com",
            credential_ref="secret-github", capabilities=tuple(CodeHostCapability),
        ))
        with self.assertRaises(CodeHostUnsupportedCapabilityError):
            await code_hosts.artifact_download(
                "github-read", github_resource.id, 19025, actor=self.worker_actor,
                max_bytes=327680, member_path="html/index.html",
            )
        self.assertEqual(len(secret_broker.calls), secret_count)

    async def test_gitlab_assignment_can_read_merge_request_fact(self) -> None:
        code_hosts = _CodeHosts()
        self.authority.resources = SimpleNamespace(
            get=lambda resource_id, actor: SimpleNamespace(
                id=resource_id,
                lifecycle="active",
                resource_type="repository",
                risk="medium",
                sensitivity="internal",
            )
        )
        service = ControlPlaneBrokerService(
            identity=self.identity,
            authority=self.authority,
            work_items=self.work_items,
            operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            action_intents=SimpleNamespace(
                execution=SimpleNamespace(registry=_ActionRegistry("gitlab"))
            ),
            code_hosts=code_hosts,
        )
        assignment = self.assignment.model_copy(
            update={
                "execution_profile_id": "repository-write",
                "resource_ids": ("repository-a",),
                "repository_scope": RepositoryExecutionScope(
                    organization_id="local",
                    workspace_id="default",
                    project_id="project-a",
                    writable_repository_ids=("repository-a",),
                    source=RepositoryTargetSource.SINGLE_REPOSITORY,
                    source_ref="repository-a",
                ),
            }
        )

        status, payload, *_ = await service.dispatch(
            assignment=assignment,
            worker_actor=self.worker_actor,
            method="GET",
            raw_target="/api/repository-facts/pull-requests/33",
            body=b"",
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["item"]["number"], 33)
        self.assertEqual(code_hosts.registry.binding.provider_type, "gitlab")
        self.assertEqual(
            code_hosts.registry.binding.credential_ref,
            "secret-gitlab",
        )

    async def test_gitlab_assignment_uses_governed_merge_action_binding(self) -> None:
        registry = _ActionRegistry("gitlab")
        action_intents = _ExecutingActionIntents(registry)
        service = ControlPlaneBrokerService(
            identity=self.identity,
            authority=self.authority,
            work_items=self.work_items,
            operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            action_intents=action_intents,
        )
        assignment = self.assignment.model_copy(
            update={
                "execution_profile_id": "repository-write",
                "resource_ids": ("repository-a",),
                "repository_scope": RepositoryExecutionScope(
                    organization_id="local",
                    workspace_id="default",
                    project_id="project-a",
                    writable_repository_ids=("repository-a",),
                    source=RepositoryTargetSource.SINGLE_REPOSITORY,
                    source_ref="repository-a",
                ),
            }
        )
        operation = self.service._resolve_operation(
            "POST", "/api/repository-actions/pull-request/merge"
        ).operation

        result = await service._execute_repository_action(
            assignment=assignment,
            worker_actor=self.worker_actor,
            requester_actor=self.identity.actor_for_identity(
                assignment.created_by,
                scope=self.scope,
            ),
            operation=operation,
            payload={
                "parameters": {
                    "number": 33,
                    "expected_head_sha": "a" * 40,
                },
                "idempotency_key": "gitlab-mr-33-merge",
            },
        )

        self.assertEqual(result["item"]["status"], "succeeded")
        created, _actor = action_intents.created[0]
        self.assertEqual(created.binding_id, "gitlab-action-binding")
        self.assertEqual(created.request.resource_ids, ("repository-a",))
        self.assertEqual(created.timeout_seconds, 120)
        self.assertEqual(action_intents.claimed[0][0].lease_seconds, 180)

    async def test_repository_action_deadlines_are_operation_bound(self) -> None:
        action_intents = _ExecutingActionIntents(_ActionRegistry("gitlab"))
        service = ControlPlaneBrokerService(
            identity=self.identity,
            authority=self.authority,
            work_items=self.work_items,
            operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            action_intents=action_intents,
        )
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "execution_workspace_id": "workspace-a",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default",
                project_id="project-a", writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref="repository-a",
            ),
        })
        actor = self.identity.actor_for_identity(assignment.created_by, scope=self.scope)
        cases = (
            ("branch/publish", {"branch": "work", "head_revision": "a" * 40}, 120, 180),
            ("pull-request/merge", {"pull_request_number": 33, "expected_head_sha": "a" * 40}, 120, 180),
            ("pull-request/upsert", {"head": "work", "base": "main", "title": "Work"}, 120, 180),
            ("issue/update", {"issue_number": 33, "labels": ["progress"]}, None, 120),
        )
        for path, parameters, timeout, lease in cases:
            with self.subTest(operation=path):
                operation = service._resolve_operation(
                    "POST", f"/api/repository-actions/{path}"
                ).operation
                await service._execute_repository_action(
                    assignment=assignment, worker_actor=self.worker_actor,
                    requester_actor=actor, operation=operation,
                    payload={"parameters": parameters},
                )
                created = action_intents.created[-1][0]
                self.assertEqual(created.timeout_seconds, timeout)
                self.assertEqual(action_intents.claimed[-1][0].lease_seconds, lease)
                self.assertEqual(
                    created.policy_decision.source,
                    "policy:assignment-control-plane",
                )
                self.assertEqual(created.request.resource_ids, ("repository-a",))
                for field, value in parameters.items():
                    self.assertEqual(created.request.parameters[field], value)
                if path == "branch/publish":
                    self.assertEqual(
                        created.request.parameters["execution_workspace_id"],
                        "workspace-a",
                    )

                for override in (
                    "timeout_seconds", "lease_seconds", "branch", "head_revision"
                ):
                    before = len(action_intents.created)
                    with self.assertRaisesRegex(
                        ControlPlaneBrokerRequestError,
                        "Put action fields inside the parameters JSON object",
                    ):
                        await service._execute_repository_action(
                            assignment=assignment,
                            worker_actor=self.worker_actor,
                            requester_actor=actor,
                            operation=operation,
                            payload={"parameters": parameters, override: 3600},
                        )
                    self.assertEqual(len(action_intents.created), before)

    async def test_existing_change_request_publication_uses_fenced_recovery_attribution(self) -> None:
        action_intents = _ExecutingActionIntents(_ActionRegistry("gitlab"))
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "execution_workspace_id": "workspace-a",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default",
                project_id="project-a", writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref="repository-a",
            ),
        })
        actor = self.identity.actor_for_identity(assignment.created_by, scope=self.scope)
        recovery = SimpleNamespace(
            work_item_ref="group/app#1331",
            assignment_id=assignment.id,
            assignment_fence=assignment.fence,
            source_execution_id=assignment.execution_id,
            source_subject=assignment.subject,
            authorized_by=actor.identity_id,
        )

        class Workspaces:
            def __init__(inner):
                inner.calls = []

            def publication_recovery(inner, workspace_id, repository_id, *, actor):
                inner.calls.append((workspace_id, repository_id, actor.identity_id))
                return recovery

        workspaces = Workspaces()
        service = ControlPlaneBrokerService(
            identity=self.identity, authority=self.authority,
            work_items=self.work_items, operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            action_intents=action_intents, execution_workspaces=workspaces,
        )
        operation = service._resolve_operation(
            "POST", "/api/repository-actions/branch/publish"
        ).operation

        await service._execute_repository_action(
            assignment=assignment, worker_actor=self.worker_actor,
            requester_actor=actor, operation=operation,
            payload={"parameters": {
                "branch": "codex/recovery",
                "head_revision": "a" * 40,
                "expected_remote_revision": "b" * 40,
                "existing_change_request_number": 39,
            }},
        )

        created = action_intents.created[-1][0]
        self.assertEqual(created.work_item_ref, "group/app#1331")
        self.assertEqual(
            created.request.parameters["execution_workspace_id"], "workspace-a"
        )
        self.assertEqual(
            workspaces.calls,
            [("workspace-a", "repository-a", actor.identity_id)],
        )

        recovery.assignment_fence += 1
        with self.assertRaisesRegex(
            ControlPlaneBrokerDeniedError, "live assignment fence"
        ):
            await service._execute_repository_action(
                assignment=assignment, worker_actor=self.worker_actor,
                requester_actor=actor, operation=operation,
                payload={"parameters": {
                    "branch": "codex/recovery",
                    "head_revision": "a" * 40,
                    "expected_remote_revision": "b" * 40,
                    "existing_change_request_number": 39,
                }},
            )
        self.assertEqual(len(action_intents.created), 1)

    async def test_work_item_publication_does_not_require_recovery_provenance(self) -> None:
        action_intents = _ExecutingActionIntents(_ActionRegistry("gitlab"))

        class Workspaces:
            def publication_recovery(inner, *args, **kwargs):
                raise AssertionError("legacy Work Item publication must not use recovery")

        assignment = self.assignment.model_copy(update={
            "subject": ExecutionSubject(
                kind=ExecutionSubjectKind.WORK_ITEM,
                ref="group/app#42",
            ),
            "work_item_ref": "group/app#42",
            "execution_profile_id": "repository-write",
            "execution_workspace_id": "workspace-work-item",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default",
                project_id="project-a", writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref="repository-a",
            ),
        })
        actor = self.identity.actor_for_identity(assignment.created_by, scope=self.scope)
        service = ControlPlaneBrokerService(
            identity=self.identity, authority=self.authority,
            work_items=self.work_items, operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            action_intents=action_intents, execution_workspaces=Workspaces(),
        )
        operation = service._resolve_operation(
            "POST", "/api/repository-actions/branch/publish"
        ).operation

        await service._execute_repository_action(
            assignment=assignment, worker_actor=self.worker_actor,
            requester_actor=actor, operation=operation,
            payload={"parameters": {
                "branch": "codex/work-item",
                "head_revision": "a" * 40,
                "expected_remote_revision": "b" * 40,
                "existing_change_request_number": 39,
            }},
        )

        self.assertEqual(
            action_intents.created[-1][0].work_item_ref, "group/app#42"
        )

    async def test_repository_intent_catalog_calls_do_not_block_event_loop(self) -> None:
        context = ContextVar("broker_request_context")
        marker = object()
        loop_thread = threading.get_ident()
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "execution_workspace_id": "workspace-a",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default",
                project_id="project-a", writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY,
                source_ref="repository-a",
            ),
        })
        actor = self.identity.actor_for_identity(assignment.created_by, scope=self.scope)

        for blocked_call in ("create", "claim", "get"):
            with self.subTest(blocked_call=blocked_call):
                entered = threading.Event()
                release = threading.Event()
                calls = []
                observations = []
                timed_out = []

                class BlockingIntents(_ExecutingActionIntents):
                    def observe(inner, name, call_actor):
                        calls.append(name)
                        observations.append((name, threading.get_ident(), context.get(), call_actor))
                        if name == blocked_call:
                            entered.set()
                            if not release.wait(timeout=2):
                                timed_out.append(name)

                    def create(inner, payload, *, actor, assignment_id=None):
                        inner.observe("create", actor)
                        return super().create(payload, actor=actor, assignment_id=assignment_id)

                    def claim(inner, payload, *, actor, intent_id):
                        inner.observe("claim", actor)
                        result = super().claim(payload, actor=actor, intent_id=intent_id)
                        return None if blocked_call == "get" else result

                    def get(inner, intent_id, actor):
                        inner.observe("get", actor)
                        return SimpleNamespace(model_dump=lambda mode: {
                            "id": intent_id, "status": "succeeded",
                        })

                    async def execute_claimed(inner, intent_id, worker_id, *, actor):
                        calls.append("execute")
                        return await super().execute_claimed(intent_id, worker_id, actor=actor)

                intents = BlockingIntents(_ActionRegistry("gitlab"))
                service = ControlPlaneBrokerService(
                    identity=self.identity, authority=self.authority,
                    work_items=self.work_items, operator=_Operator(),
                    audit=ControlPlaneBrokerAuditStore(self.sqlite), action_intents=intents,
                )
                operation = service._resolve_operation(
                    "POST", "/api/repository-actions/pull-request/merge"
                ).operation
                token = context.set(marker)
                task = asyncio.create_task(service._execute_repository_action(
                    assignment=assignment, worker_actor=self.worker_actor,
                    requester_actor=actor, operation=operation,
                    payload={"parameters": {"pull_request_number": 33,
                        "expected_head_sha": "a" * 40}},
                ))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    # This checkpoint must run while the catalog call is still
                    # waiting at its barrier; no latency threshold is involved.
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertEqual(timed_out, [])
                finally:
                    release.set()
                    result = await task
                    context.reset(token)
                self.assertEqual(result["item"]["status"], "succeeded")
                self.assertEqual(calls, ["create", "claim", "get" if blocked_call == "get" else "execute"])
                for name, thread, value, call_actor in observations:
                    self.assertNotEqual(thread, loop_thread)
                    self.assertIs(value, marker)
                    self.assertIs(call_actor, self.worker_actor if name == "claim" else actor)

    def test_repository_binding_resolution_fails_closed_when_ambiguous(self) -> None:
        registry = _ActionRegistry()
        original = registry.list_bindings
        registry.list_bindings = lambda actor: [*original(actor), *original(actor)]

        with self.assertRaisesRegex(
            ControlPlaneBrokerDeniedError,
            "exactly one eligible enabled provider binding",
        ):
            self.service._repository_action_binding(
                action_registry=registry,
                actor=self.identity.actor_for_identity(
                    "local-admin",
                    scope=self.scope,
                ),
                project_id="project-a",
                repository_id="repository-a",
                action_id="code-host.branch.publish",
            )

    async def test_repository_write_assignment_can_discover_open_pull_requests(self) -> None:
        code_hosts = _CodeHosts()
        self.authority.resources = SimpleNamespace(
            get=lambda resource_id, actor: SimpleNamespace(
                id=resource_id,
                lifecycle="active",
                resource_type="repository",
                risk="medium",
                sensitivity="internal",
            )
        )
        service = ControlPlaneBrokerService(
            identity=self.identity,
            authority=self.authority,
            work_items=self.work_items,
            operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            action_intents=SimpleNamespace(execution=SimpleNamespace(registry=_ActionRegistry())),
            code_hosts=code_hosts,
        )
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY, source_ref="repository-a",
            ),
        })
        status, payload, operation, target_ref, decision = await service.dispatch(
            assignment=assignment, worker_actor=self.worker_actor, method="GET",
            raw_target="/api/repository-facts/pull-requests?state=open", body=b"",
        )
        self.assertEqual(status, 200)
        self.assertEqual(operation.id, "repository.read.pull-requests")
        self.assertIsNone(target_ref)
        self.assertEqual(payload["items"][0]["number"], 954)
        self.assertEqual(decision.outcome, AuthorityDecisionOutcome.ALLOW)

    async def test_repository_write_assignment_can_reconcile_its_publish_intent(self) -> None:
        self.authority.resources = SimpleNamespace(
            get=lambda resource_id, actor: SimpleNamespace(
                id=resource_id,
                lifecycle="active",
                resource_type="repository",
                risk="medium",
                sensitivity="internal",
            )
        )
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY, source_ref="repository-a",
            ),
        })
        intent = SimpleNamespace(
            id="action-intent-deadbeef",
            action_id="code-host.branch.publish",
            execution_id=assignment.execution_id,
            organization_id=assignment.organization_id,
            workspace_id=assignment.workspace_id,
            project_id=assignment.project_id,
            resource_ids=("repository-a",),
            requested_by=assignment.created_by,
        )
        action_intents = _ActionIntents(intent)
        service = ControlPlaneBrokerService(
            identity=self.identity,
            authority=self.authority,
            work_items=self.work_items,
            operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            action_intents=action_intents,
        )

        status, payload, operation, target_ref, decision = await service.dispatch(
            assignment=assignment,
            worker_actor=self.worker_actor,
            method="POST",
            raw_target=f"/api/action-intents/{intent.id}/reconcile",
            body=b'{"retry_if_idempotent":true}',
        )

        self.assertEqual(status, 200)
        self.assertEqual(operation.id, "action_intent.reconcile")
        self.assertEqual(target_ref, intent.id)
        self.assertEqual(payload["item"]["status"], "succeeded")
        self.assertEqual(action_intents.reconciled[0][0], intent.id)
        self.assertEqual(decision.outcome, AuthorityDecisionOutcome.ALLOW)

    async def test_recovered_assignment_reads_delivery_evidence_without_mutation(self) -> None:
        self._publish_worker_authority(repository_evidence_read=True)
        self.authority.resources = SimpleNamespace(get=lambda resource_id, actor:
            SimpleNamespace(id=resource_id, lifecycle="active", resource_type="repository",
                            risk="medium", sensitivity="internal"))
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY, source_ref="repository-a"),
        })
        intent = SimpleNamespace(id="action-intent-evidence", action_id="code-host.branch.publish",
            organization_id="local", workspace_id="default", project_id="project-a",
            resource_ids=("repository-a",), requested_by=assignment.created_by)
        actions = _ActionIntents(intent)
        history = {"intent": {"id":intent.id, "status":"succeeded", "work_item_ref":None,
            "execution_started_at":100, "completed_at":110,
            "execution_id":"superseded", "request":{"parameters":{"head_revision":"head"},
                                                       "credential_ref":"private-credential"}},
            "receipts":[{"id":"receipt", "provider_external_id":"head", "outcome":"completed",
                          "result":{"output":{"secret":"private-provider-output"}}}],
            "verifications":[{"id":"verification", "verified":True}],
            "inbox":[{"body":"private-inbox"}]}
        actions.history = lambda intent_id, actor: history
        service = ControlPlaneBrokerService(identity=self.identity, authority=self.authority,
            work_items=self.work_items, operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite), action_intents=actions)
        status, payload, operation, _, decision = await service.dispatch(
            assignment=assignment, worker_actor=self.worker_actor, method="GET",
            raw_target=f"/api/action-intents/{intent.id}/delivery-evidence", body=b"")
        self.assertEqual(status, 200)
        self.assertEqual(operation.authority_level, AuthorityLevel.READ)
        self.assertEqual(payload["intent"]["execution_id"], "superseded")
        self.assertEqual(payload['intent']['execution_started_at'], 100)
        self.assertEqual(payload['intent']['completed_at'], 110)
        self.assertIsNone(payload["intent"]["work_item_ref"])
        self.assertTrue(payload["verifications"][0]["verified"])
        self.assertNotIn("private-", json.dumps(payload))
        self.assertFalse(actions.reconciled)
        # MR promotion uses an upsert receipt. Reading it must not authorize
        # mutation reconciliation or reveal request/provider bodies.
        intent.action_id = "code-host.pull-request.upsert"
        status, upsert_payload, _, _, _ = await service.dispatch(
            assignment=assignment, worker_actor=self.worker_actor, method="GET",
            raw_target=f"/api/action-intents/{intent.id}/delivery-evidence", body=b"")
        self.assertEqual(status, 200)
        self.assertTrue(upsert_payload["verifications"][0]["verified"])
        self.assertEqual(upsert_payload['intent']['execution_started_at'], 100)
        self.assertEqual(upsert_payload['intent']['completed_at'], 110)
        self.assertNotIn("private-", json.dumps(upsert_payload))
        original_receipts, original_verifications = history['receipts'], history['verifications']
        history['intent'].update(status='failed', attempt=0, execution_started_at=None, completed_at=120)
        history['receipts'] = []
        history['verifications'] = []
        status, refused_payload, _, _, _ = await service.dispatch(
            assignment=assignment, worker_actor=self.worker_actor, method='GET',
            raw_target=f'/api/action-intents/{intent.id}/delivery-evidence', body=b'')
        self.assertEqual(status, 200)
        self.assertEqual(refused_payload['intent']['status'], 'failed')
        self.assertEqual(refused_payload['intent']['attempt'], 0)
        self.assertIsNone(refused_payload['intent']['execution_started_at'])
        self.assertEqual(refused_payload['intent']['completed_at'], 120)
        self.assertEqual(refused_payload['receipts'], [])
        self.assertNotIn('never_started', refused_payload)
        history['receipts'], history['verifications'] = original_receipts, original_verifications
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError, "outside the assignment delivery scope"):
            await service.dispatch(
                assignment=assignment, worker_actor=self.worker_actor, method="POST",
                raw_target=f"/api/action-intents/{intent.id}/reconcile",
                body=b'{"retry_if_idempotent":true}')
        self.assertFalse(actions.reconciled)
        for field, value in (("organization_id", "other"), ("workspace_id", "other"),
                             ("project_id", "other"), ("resource_ids", ("repository-b",)),
                             ("requested_by", "someone-else"), ("action_id", "secret.rotate")):
            with self.subTest(field=field):
                previous = getattr(intent, field)
                setattr(intent, field, value)
                try:
                    with self.assertRaisesRegex(ControlPlaneBrokerDeniedError, "outside assignment scope"):
                        await service.dispatch(assignment=assignment, worker_actor=self.worker_actor,
                            method="GET", raw_target=f"/api/action-intents/{intent.id}/delivery-evidence", body=b"")
                finally:
                    setattr(intent, field, previous)
        with self.assertRaises(ControlPlaneBrokerDeniedError):
            await service.dispatch(assignment=assignment, worker_actor=self.worker_actor,
                method="POST", raw_target=f"/api/action-intents/{intent.id}/delivery-evidence", body=b"{}")
        history["receipts"] *= 20
        history["verifications"] *= 20
        _, bounded, *_ = await service.dispatch(assignment=assignment, worker_actor=self.worker_actor,
            method="GET", raw_target=f"/api/action-intents/{intent.id}/delivery-evidence", body=b"")
        self.assertEqual(len(bounded["receipts"]), 8)
        self.assertEqual(bounded["omitted_receipts"], 12)
        self.assertEqual(len(bounded["verifications"]), 8)
        self.assertEqual(bounded["omitted_verifications"], 12)
        self.assertFalse(actions.reconciled)
        self._publish_worker_authority(repository_evidence_read=False)
        with self.assertRaisesRegex(ControlPlaneBrokerDeniedError, "no assigned Role"):
            await service.dispatch(assignment=assignment, worker_actor=self.worker_actor,
                method="GET", raw_target=f"/api/action-intents/{intent.id}/delivery-evidence", body=b"")

    async def test_recovered_assignment_can_reconcile_historical_publish_without_retry(self) -> None:
        self.authority.resources = SimpleNamespace(
            get=lambda resource_id, actor: SimpleNamespace(
                id=resource_id,
                lifecycle="active",
                resource_type="repository",
                risk="medium",
                sensitivity="internal",
            )
        )
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY, source_ref="repository-a",
            ),
        })
        intent = SimpleNamespace(
            id="action-intent-historical",
            action_id="code-host.branch.publish",
            execution_id="superseded-thread-bootstrap",
            organization_id=assignment.organization_id,
            workspace_id=assignment.workspace_id,
            project_id=assignment.project_id,
            resource_ids=("repository-a",),
            requested_by=assignment.created_by,
        )
        action_intents = _ActionIntents(intent)
        service = ControlPlaneBrokerService(
            identity=self.identity,
            authority=self.authority,
            work_items=self.work_items,
            operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            action_intents=action_intents,
        )

        status, payload, *_ = await service.dispatch(
            assignment=assignment,
            worker_actor=self.worker_actor,
            method="POST",
            raw_target=f"/api/action-intents/{intent.id}/reconcile",
            body=b'{"retry_if_idempotent":false}',
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["item"]["status"], "requires_reconciliation")
        self.assertFalse(action_intents.reconciled[0][1].retry_if_idempotent)

        with self.assertRaisesRegex(
            ControlPlaneBrokerDeniedError,
            "may reconcile but not retry",
        ):
            await service.dispatch(
                assignment=assignment,
                worker_actor=self.worker_actor,
                method="POST",
                raw_target=f"/api/action-intents/{intent.id}/reconcile",
                body=b'{"retry_if_idempotent":true}',
            )

    async def test_recovered_assignment_can_verify_historical_merge_without_retry(self) -> None:
        self.authority.resources = SimpleNamespace(
            get=lambda resource_id, actor: SimpleNamespace(
                id=resource_id,
                lifecycle="active",
                resource_type="repository",
                risk="medium",
                sensitivity="internal",
            )
        )
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY, source_ref="repository-a",
            ),
        })
        intent = SimpleNamespace(
            id="action-intent-00000000000000000000000000000001",
            action_id="code-host.pull-request.merge",
            execution_id="superseded-thread-bootstrap",
            organization_id=assignment.organization_id,
            workspace_id=assignment.workspace_id,
            project_id=assignment.project_id,
            resource_ids=("repository-a",),
            requested_by=assignment.created_by,
        )
        action_intents = _ActionIntents(intent)
        service = ControlPlaneBrokerService(
            identity=self.identity,
            authority=self.authority,
            work_items=self.work_items,
            operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            action_intents=action_intents,
        )

        status, payload, *_ = await service.dispatch(
            assignment=assignment,
            worker_actor=self.worker_actor,
            method="POST",
            raw_target=f"/api/action-intents/{intent.id}/reconcile",
            body=b'{"retry_if_idempotent":false}',
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["item"]["status"], "requires_reconciliation")
        self.assertFalse(action_intents.reconciled[0][1].retry_if_idempotent)

        with self.assertRaisesRegex(
            ControlPlaneBrokerDeniedError,
            "may reconcile but not retry",
        ):
            await service.dispatch(
                assignment=assignment,
                worker_actor=self.worker_actor,
                method="POST",
                raw_target=f"/api/action-intents/{intent.id}/reconcile",
                body=b'{"retry_if_idempotent":true}',
            )

    async def test_repository_reconciliation_scope_mismatches_fail_closed(self) -> None:
        self.authority.resources = SimpleNamespace(
            get=lambda resource_id, actor: SimpleNamespace(
                id=resource_id,
                lifecycle="active",
                resource_type="repository",
                risk="medium",
                sensitivity="internal",
            )
        )
        assignment = self.assignment.model_copy(update={
            "execution_profile_id": "repository-write",
            "resource_ids": ("repository-a",),
            "repository_scope": RepositoryExecutionScope(
                organization_id="local", workspace_id="default", project_id="project-a",
                writable_repository_ids=("repository-a",),
                source=RepositoryTargetSource.SINGLE_REPOSITORY, source_ref="repository-a",
            ),
        })
        baseline = {
            "id": "action-intent-00000000000000000000000000000002",
            "action_id": "code-host.pull-request.merge",
            "execution_id": assignment.execution_id,
            "organization_id": assignment.organization_id,
            "workspace_id": assignment.workspace_id,
            "project_id": assignment.project_id,
            "resource_ids": ("repository-a",),
            "requested_by": assignment.created_by,
        }
        mismatches = {
            "organization_id": "other-org",
            "workspace_id": "other-workspace",
            "project_id": "project-b",
            "resource_ids": ("repository-b",),
            "requested_by": "other-requester",
            "action_id": "code-host.issue.comment",
        }

        for field, value in mismatches.items():
            with self.subTest(field=field):
                intent = SimpleNamespace(**{**baseline, field: value})
                service = ControlPlaneBrokerService(
                    identity=self.identity,
                    authority=self.authority,
                    work_items=self.work_items,
                    operator=_Operator(),
                    audit=ControlPlaneBrokerAuditStore(self.sqlite),
                    action_intents=_ActionIntents(intent),
                )
                with self.assertRaisesRegex(
                    ControlPlaneBrokerDeniedError,
                    "outside the assignment delivery scope",
                ):
                    await service.dispatch(
                        assignment=assignment,
                        worker_actor=self.worker_actor,
                        method="POST",
                        raw_target=f"/api/action-intents/{intent.id}/reconcile",
                        body=b'{"retry_if_idempotent":false}',
                    )


if __name__ == "__main__":
    unittest.main()
