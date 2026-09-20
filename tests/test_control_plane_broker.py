from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from codex_web.authority import (
    AUTHORITY_ROLE_CATALOG_ID,
    AUTHORITY_ROLE_CATALOG_KIND,
    AUTHORITY_ROLE_CATALOG_SCHEMA_VERSION,
    AuthorityGrant,
    AuthorityLevel,
    AuthorityRoleBinding,
    AuthorityRoleCatalogDefinition,
    AuthorityRoleDefinition,
)
from codex_web.control_plane_broker import ControlPlaneBrokerLimits
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
from codex_web.services.authority_roles import install_authority_roles
from codex_web.services.control_plane_broker import (
    AssignmentBoundControlPlaneBroker,
    ControlPlaneBrokerService,
)
from codex_web.services.definitions import DefinitionRegistryService
from codex_web.services.identity import IdentityService
from codex_web.storage.control_plane_broker import ControlPlaneBrokerAuditStore
from codex_web.storage.definition_registry import DefinitionRegistryStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


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
    ):
        del owner, stage, release_gate
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
    async def retry(self, ref, *, actor, reason):
        return {"ok": True, "item": {"ref": ref, "actor": actor, "reason": reason}}

    async def reconcile(self, ref, *, actor, reason):
        return {"ok": True, "item": {"ref": ref, "actor": actor, "reason": reason}}


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
            service_scopes=("execution-worker:run", "secret:use"),
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
        self.service = ControlPlaneBrokerService(
            identity=self.identity,
            authority=self.authority,
            work_items=self.work_items,
            operator=_Operator(),
            audit=ControlPlaneBrokerAuditStore(self.sqlite),
            limits=ControlPlaneBrokerLimits(
                max_request_bytes=1024,
                max_response_bytes=4096,
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
            service_identity_id=self.worker_actor.identity_id,
            fence=1,
            validator=lambda: self.current_assignment,
        )
        await self.broker.start()

    async def asyncTearDown(self) -> None:
        await self.broker.stop()
        self.temp.cleanup()

    def _publish_worker_authority(self) -> None:
        active = self.registry.resolve(
            definition_id=AUTHORITY_ROLE_CATALOG_ID,
            kind=AUTHORITY_ROLE_CATALOG_KIND,
        )
        catalog = AuthorityRoleCatalogDefinition(
            roles=(
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
                    ),
                ),
            ),
            bindings=(
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
            service_identity_id=self.worker_actor.identity_id,
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


if __name__ == "__main__":
    unittest.main()
