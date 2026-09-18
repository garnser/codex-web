from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException

from codex_web.definitions import DefinitionReference
from codex_web.execution_contract_seed import execution_role_catalog_seed_payload
from codex_web.execution_contracts import (
    execution_role_for_work_item,
    install_execution_role_catalog_provider,
)
from codex_web.execution_role_models import ExecutionRoleCatalogDefinition
from codex_web.executive import DelegateRequest, ExecutiveService
from codex_web.models import WorkItemHandoff, WorkItemState
from codex_web.services.work_item_contracts import install_work_item_contract_service


CATALOG = ExecutionRoleCatalogDefinition.model_validate(execution_role_catalog_seed_payload())
DEFINITION_REF = DefinitionReference(
    definition_id="execution-roles.default",
    kind="execution-role-catalog",
    revision=1,
    record_id="definition-record-1",
    checksum="0" * 64,
    definition_schema_version="1.0",
)
install_execution_role_catalog_provider(lambda: CATALOG)


class _ExecutionRoles:
    def catalog(self, **kwargs):
        return CATALOG

    def reference(self, **kwargs):
        return DEFINITION_REF


def _state(**overrides) -> WorkItemState:
    payload = {
        "ref": "gitlab:acme/codex-web#42",
        "project_id": "home",
        "project_path": "acme/codex-web",
        "title": "Canonical GitLab work item",
        "current_owner": "james",
        "current_stage": "implementation_active",
        "artifact_state": "branch",
        "last_meaningful_update_at": 1.0,
        "updated_at": 1.0,
        "created_at": 1.0,
    }
    payload.update(overrides)
    return WorkItemState(**payload)


class ExecutionRoleForWorkItemTests(unittest.TestCase):
    def test_derives_role_from_canonical_owner_and_stage(self) -> None:
        implementation = _state(current_owner="james")
        validation = _state(
            current_owner=None,
            next_owner=None,
            current_stage="ready_for_validation",
            validation_owner="quinn",
        )
        release = _state(
            current_owner=None,
            next_owner=None,
            current_stage="ready_to_close",
            release_gate=True,
            release_owner="release manager",
            artifact_state="merged_main",
        )

        self.assertEqual(execution_role_for_work_item(implementation).id, "james")
        self.assertEqual(execution_role_for_work_item(validation).id, "quinn")
        self.assertEqual(execution_role_for_work_item(release).id, "release-manager")

    def test_pending_handoff_routes_to_recipient_before_current_owner(self) -> None:
        state = _state(
            current_owner="james",
            handoff=WorkItemHandoff(
                from_agent="james",
                to_agent="quinn",
                requested_at=2.0,
                status="pending",
                artifact_state="merge_request",
                stage="ready_for_validation",
            ),
        )

        role = execution_role_for_work_item(state)

        self.assertEqual(role.id, "quinn")


class _CanonicalHost:
    HANDOFF_COORDINATION_CHANNEL = "handoff-coordination"

    def __init__(self, data_dir: Path, state: WorkItemState) -> None:
        self.DATA_DIR = data_dir
        self.state = state
        self.project = SimpleNamespace(
            id="home",
            sandbox="workspace-write",
            approval_policy="on-request",
            model=None,
        )
        self.binding = SimpleNamespace(
            thread_id="thread-existing-james",
            sandbox="workspace-write",
            approval_policy="on-request",
        )
        self.binding_requests: list[tuple[str, str, str | None]] = []
        self.dispatched: list[tuple[object, str, str]] = []

    def _work_item_state(self, ref: str) -> WorkItemState:
        if ref != self.state.ref:
            raise AssertionError(f"unexpected work item ref: {ref}")
        return self.state

    def _work_item_split_brain_findings(self, state: WorkItemState) -> list[str]:
        return []

    def _project(self, project_id: str):
        if project_id != self.project.id:
            raise AssertionError(f"unexpected project id: {project_id}")
        return self.project

    def _binding_for_agent(
        self,
        agent_key: str,
        project_id: str,
        preferred_conversation_id: str | None = None,
    ):
        self.binding_requests.append((agent_key, project_id, preferred_conversation_id))
        return self.binding

    def _work_item_dispatch_text(self, state: WorkItemState) -> str:
        return f"Canonical GitLab dispatch for {state.ref}"

    async def _dispatch_event_to_binding(self, binding, message: str, source: str):
        self.dispatched.append((binding, message, source))
        return {"threadId": binding.thread_id, "ok": True}

    def _work_item_state_public(self, state: WorkItemState) -> dict:
        return state.model_dump()


class ExecutiveCanonicalDelegationTests(unittest.TestCase):
    def test_conflicting_requested_role_is_rejected_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            host = _CanonicalHost(Path(temp_dir), _state(current_owner="james"))
            service = ExecutiveService(host)
            request = DelegateRequest(
                task="Validate this work item",
                agent_id="cto",
                execution_role_id="quinn",
                work_item_ref=host.state.ref,
                project_id="home",
            )

            with self.assertRaises(HTTPException) as raised:
                asyncio.run(service.delegate(request))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("would bypass canonical ownership", str(raised.exception.detail))
        self.assertEqual(host.binding_requests, [])
        self.assertEqual(host.dispatched, [])

    def test_existing_binding_is_reused_for_canonical_work_item(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            host = _CanonicalHost(Path(temp_dir), _state(current_owner="james"))
            service = ExecutiveService(host)
            result = asyncio.run(
                service.delegate(
                    DelegateRequest(
                        task="Implement the canonical issue",
                        executive_reply="Keep the change narrowly scoped.",
                        agent_id="cto",
                        work_item_ref=host.state.ref,
                        project_id="home",
                        change_classification="localized functional",
                    )
                )
            )

        self.assertEqual(
            host.binding_requests,
            [("james", "home", host.HANDOFF_COORDINATION_CHANNEL)],
        )
        self.assertEqual(len(host.dispatched), 1)
        binding, message, source = host.dispatched[0]
        self.assertIs(binding, host.binding)
        self.assertEqual(source, "executive-work-item")
        self.assertIn("Canonical GitLab dispatch", message)
        self.assertIn("Canonical execution role: James", message)
        self.assertEqual(result["threadId"], host.binding.thread_id)
        self.assertFalse(result["createdNewThread"])
        self.assertEqual(result["dispatchMode"], "canonical-work-item")
        self.assertEqual(result["executionRole"]["id"], "james")


class GitLabContractInjectionTests(unittest.TestCase):
    def test_installed_dispatcher_injects_contract_derived_from_gitlab_state(self) -> None:
        app = FastAPI()
        host = SimpleNamespace()
        host._work_item_split_brain_findings = lambda state: []
        host._work_item_dispatch_text = lambda state: f"GitLab canonical dispatch for {state.ref}"
        state = _state(
            current_owner=None,
            next_owner=None,
            current_stage="ready_for_validation",
            validation_owner="quinn",
            artifact_state="merge_request",
        )

        service = install_work_item_contract_service(app, host, _ExecutionRoles())
        text = host._work_item_dispatch_text(state)

        self.assertIs(service, app.state.work_item_contract_service)
        self.assertIn("GitLab canonical dispatch", text)
        self.assertIn("CANONICAL EXECUTION CONTRACT", text)
        self.assertIn("Name: Quinn", text)
        self.assertIn("Lane: validation", text)
        self.assertIn("do not create parallel ownership", text)
        self.assertIn("execution-roles.default@1", text)


if __name__ == "__main__":
    unittest.main()
