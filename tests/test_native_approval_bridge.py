from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from codex_web.approval_requests import ApprovalRequestStatus
from codex_web.services.approval_requests import ApprovalRequestService
from codex_web.services.approvals import ApprovalService
from codex_web.services.canonical_events import (
    CanonicalEventBus,
    CanonicalEventIngestionService,
)
from codex_web.services.identity import IdentityService
from codex_web.storage.approval_requests import ApprovalRequestStore
from codex_web.storage.canonical_events import CanonicalEventStore
from codex_web.storage.identity_state import IdentityStateStore
from codex_web.storage.sqlite_state import SQLiteStateStore


class _Runtime:
    def __init__(self, pending=None) -> None:
        self.pending_approvals = dict(pending or {})
        self.responses = []

    async def respond_to_server_request(self, request_id, result) -> None:
        self.responses.append((request_id, result))
        self.pending_approvals.pop(request_id, None)


class _Host:
    def __init__(self, runtime: _Runtime) -> None:
        self.codex = runtime
        self._messages = {}

    @staticmethod
    def _request_id_value(value):
        text = str(value)
        return int(text) if text.isdigit() else value

    @staticmethod
    def _approval_result(method: str, decision: str):
        return {"decision": decision, "method": method}

    @staticmethod
    def _approval_summary(request):
        return f"{request.get('method')} approval"

    def _load_approval_messages(self):
        return self._messages

    def _save_approval_messages(self, value):
        self._messages = value


class NativeApprovalBridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        sqlite = SQLiteStateStore(Path(self.temp.name) / "state.sqlite3")
        identity = IdentityService(IdentityStateStore(sqlite))
        identity.bootstrap_local()
        self.approver = identity.local_trusted_actor()
        self.requester = identity.bootstrap_service_actor(
            identity_id="service-codex-approval-test",
            name="Codex approval requester",
            scope=self.approver.tenant,
            service_scopes=("approvals:request",),
        )

        event_store = CanonicalEventStore(sqlite)
        ingestion = CanonicalEventIngestionService(
            CanonicalEventBus(event_store)
        )
        self.canonical = ApprovalRequestService(
            ApprovalRequestStore(sqlite),
            identity,
            ingestion,
        )
        self.runtime = _Runtime()
        self.host = _Host(self.runtime)
        self.bridge = ApprovalService(
            self.host,
            canonical=self.canonical,
            canonical_requester=self.requester,
            compatibility_actor=self.approver,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def message(request_id=7, command="echo safe"):
        return {
            "id": request_id,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "thread-1",
                "command": command,
            },
        }

    async def test_native_prompt_becomes_canonical_and_is_consumed_before_rpc_response(self) -> None:
        message = self.message()
        self.runtime.pending_approvals[7] = message
        canonical = await self.bridge.register_native_request(message)

        self.assertIsNotNone(canonical)
        self.assertEqual(
            canonical.status,
            ApprovalRequestStatus.PENDING,
        )
        self.assertEqual(
            canonical.target.object_type,
            "codex_server_request",
        )

        result = await self.bridge.decide(
            "7",
            "accept",
            actor=self.approver,
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            self.runtime.responses,
            [
                (
                    7,
                    {
                        "decision": "accept",
                        "method": "item/commandExecution/requestApproval",
                    },
                )
            ],
        )
        consumed = self.canonical.store.get(canonical.id)
        self.assertEqual(
            consumed.status,
            ApprovalRequestStatus.CONSUMED,
        )
        self.assertEqual(
            consumed.resulting_operation_reference,
            "codex-rpc:7",
        )
        self.assertEqual(
            consumed.consumed_by_identity_id,
            self.approver.identity_id,
        )

    async def test_same_native_public_id_cannot_change_target_after_registration(self) -> None:
        original = self.message()
        await self.bridge.register_native_request(original)

        with self.assertRaisesRegex(
            RuntimeError,
            "reused for a different operation",
        ):
            await self.bridge.register_native_request(
                self.message(command="rm -rf changed")
            )

    async def test_historical_native_resolution_still_passes_canonical_rejection(self) -> None:
        message = self.message()
        self.runtime.pending_approvals[7] = message

        await self.bridge.respond_compatibility(
            7,
            {
                "decision": "decline",
                "method": "item/commandExecution/requestApproval",
            },
        )

        canonical_id = self.bridge._canonical_request_id(7)
        rejected = self.canonical.store.get(canonical_id)
        self.assertEqual(
            rejected.status,
            ApprovalRequestStatus.REJECTED,
        )
        self.assertEqual(
            self.runtime.responses[0][1]["decision"],
            "decline",
        )


if __name__ == "__main__":
    unittest.main()
