from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
import base64
import httpx
from contextlib import ExitStack
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

from codex_web.code_hosts import CodeHostPullRequestFact
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
from codex_web.definitions import DefinitionDraftCreate, DefinitionPublishRequest
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

    async def source_detail(self, ref):
        return {
            "snapshot": {"identity": {"external_id": ref}, "body_text": "Acceptance criteria"},
            "discussion": [{"external_id": "comment-1", "body_text": "Context"}],
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
        return SimpleNamespace(
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

    def create(self, payload, *, actor):
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
            model_dump=lambda mode: {
                "id": intent_id,
                "status": "pending" if payload.retry_if_idempotent else "requires_reconciliation",
            }
        )


class ControlPlaneBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
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
                max_response_bytes=8192,
                max_concurrent_requests=2,
                max_requests_per_minute=8,
            ),
        )
        self.assignment = self._assignment()
        self.current_assignment = self.assignment
        self.broker = AssignmentBoundControlPlaneBroker(
            self.service,
            assignment=self.assignment,
            worker_id="worker-1",
            worker_actor=self.worker_actor,
            fence=1,
            validator=lambda: self.current_assignment,
        )
        await self.broker.start()

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
        self.temp.cleanup()

    def _publish_worker_authority(self, *, operator_grants=True) -> None:
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
        response = await asyncio.wait_for(reader.read(), timeout=2)
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
        operation_ids = {item["id"] for item in payload["operations"]}
        self.assertIn("control_plane.operations.list", operation_ids)
        self.assertIn("repository.branch.publish", operation_ids)
        self.assertIn("repository.read.job-logs", operation_ids)
        self.assertIn("repository.read.artifacts", operation_ids)
        self.assertIn("repository.read.artifact-download", operation_ids)
        self.assertIn("repository.check.rerun", operation_ids)
        self.assertIn("repository.workspace.refresh", operation_ids)
        self.assertIn("action_intent.reconcile", operation_ids)
        self.assertIn("work_item.steer", operation_ids)
        self.assertIn("deployment.local.status", operation_ids)
        self.assertIn("deployment.local.install", operation_ids)
        self.assertNotIn("lease_token", json.dumps(payload))
        self.assertEqual(
            payload["_broker"]["operation"],
            "control_plane.operations.list",
        )

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

    async def test_authorized_read_and_handoff_use_exact_role_authority(self) -> None:
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
        self.assertEqual(len(secret_broker.calls), 3)

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
                self.assertEqual(created.policy_decision.source, "policy:assignment-control-plane")
                self.assertEqual(created.request.resource_ids, ("repository-a",))
                for field, value in parameters.items():
                    self.assertEqual(created.request.parameters[field], value)
                if path == "branch/publish":
                    self.assertEqual(created.request.parameters["execution_workspace_id"], "workspace-a")

                for override in ("timeout_seconds", "lease_seconds"):
                    with self.assertRaisesRegex(ControlPlaneBrokerRequestError, "unsupported fields"):
                        await service._execute_repository_action(
                            assignment=assignment, worker_actor=self.worker_actor,
                            requester_actor=actor, operation=operation,
                            payload={"parameters": parameters, override: 3600},
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

                    def create(inner, payload, *, actor):
                        inner.observe("create", actor)
                        return super().create(payload, actor=actor)

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
            "exactly one enabled provider binding",
        ):
            self.service._repository_action_binding(
                action_registry=registry,
                actor=self.identity.actor_for_identity(
                    "local-admin",
                    scope=self.scope,
                ),
                project_id="project-a",
                repository_id="repository-a",
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
        self.assertEqual(payload["item"]["status"], "pending")
        self.assertEqual(action_intents.reconciled[0][0], intent.id)
        self.assertEqual(decision.outcome, AuthorityDecisionOutcome.ALLOW)

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
