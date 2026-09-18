from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from codex_web.runtime.execution import TurnExecutionService
from codex_web.services.approvals import ApprovalService
from codex_web.services.identity import IdentityService
from codex_web.services.thread_bootstrap_bindings import (
    ThreadBootstrapBindingService,
)
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore
from codex_web.storage.thread_bootstrap_bindings import (
    ThreadBootstrapBindingStore,
)


class _ApprovalRuntime:
    def __init__(self, pending=None) -> None:
        self.pending_approvals = dict(pending or {})
        self.respond_to_server_request = AsyncMock()


class _ApprovalHost:
    def __init__(self) -> None:
        self.codex = _ApprovalRuntime()


class _BootstrapRoutingHost:
    def __init__(self) -> None:
        self.codex = SimpleNamespace(request=AsyncMock())

    @staticmethod
    def _load_active_turns():
        return {}


class _EmptySessionManager:
    sessions = {}

    @staticmethod
    def get(_assignment_id):
        return None


class IsolatedTurnAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_approval_response_routes_to_assignment_runtime_not_global_codex(self) -> None:
        host = _ApprovalHost()
        worker_runtime = _ApprovalRuntime(
            {
                "assignment-1::request-42": {
                    "id": "assignment-1::request-42",
                    "method": "item/commandExecution/requestApproval",
                }
            }
        )
        manager = SimpleNamespace(
            sessions={
                "assignment-1": SimpleNamespace(runtime=worker_runtime),
            }
        )
        service = ApprovalService(host, assignment_sessions=manager)

        pending = service.pending()
        self.assertIn("assignment-1::request-42", pending)

        await service.respond(
            "assignment-1::request-42",
            {"decision": "approved"},
        )

        host.codex.respond_to_server_request.assert_not_awaited()
        worker_runtime.respond_to_server_request.assert_awaited_once_with(
            "assignment-1::request-42",
            {"decision": "approved"},
        )

    async def test_restart_with_durable_bootstrap_binding_and_no_session_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sqlite = SQLiteStateStore(Path(temp) / "state.sqlite3")
            identity = IdentityService(IdentityStateStore(sqlite))
            identity.bootstrap_local()
            actor = identity.local_trusted_actor()
            bindings = ThreadBootstrapBindingService(
                ThreadBootstrapBindingStore(sqlite)
            )
            bindings.bind(
                bootstrap_id="bootstrap-restart",
                thread_id="thread-isolated",
                execution_id="bootstrap-exec",
                assignment_id="assignment-bootstrap",
                execution_workspace_id="execws-bootstrap",
                actor=actor,
            )

            host = _BootstrapRoutingHost()
            restarted_service = TurnExecutionService(
                host,
                session_manager=_EmptySessionManager(),
                bootstrap_bindings=ThreadBootstrapBindingService(
                    ThreadBootstrapBindingStore(sqlite)
                ),
                control_actor=actor,
            )

            with self.assertRaises(HTTPException) as caught:
                await restarted_service.request_for_thread(
                    "thread-isolated",
                    "thread/read",
                    {
                        "threadId": "thread-isolated",
                        "includeTurns": True,
                    },
                )

            self.assertEqual(caught.exception.status_code, 503)
            self.assertIn("no live Codex session", caught.exception.detail)
            host.codex.request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
