from __future__ import annotations

import unittest
from types import SimpleNamespace

from codex_web.services.approvals import ApprovalService


class _Runtime:
    def __init__(self, pending=None) -> None:
        self.pending_approvals = dict(pending or {})
        self.responses = []

    async def respond_to_server_request(self, request_id, result) -> None:
        self.responses.append((request_id, result))
        self.pending_approvals.pop(request_id, None)


class AssignmentBoundApprovalRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_view_aggregates_global_and_assignment_bound_runtimes(self) -> None:
        global_runtime = _Runtime(
            {7: {"id": 7, "method": "global/approval"}}
        )
        worker_runtime = _Runtime(
            {
                "assignment-1:7": {
                    "id": "assignment-1:7",
                    "method": "worker/approval",
                }
            }
        )
        host = SimpleNamespace(codex=global_runtime)
        manager = SimpleNamespace(
            sessions={
                "assignment-1": SimpleNamespace(runtime=worker_runtime),
            }
        )

        service = ApprovalService(host, assignment_sessions=manager)

        pending = service.pending()
        self.assertEqual(set(pending), {7, "assignment-1:7"})
        self.assertEqual(
            {item["id"] for item in service.list()},
            {7, "assignment-1:7"},
        )

    async def test_response_is_sent_only_to_runtime_that_owns_canonical_request_id(self) -> None:
        global_runtime = _Runtime(
            {7: {"id": 7, "method": "global/approval"}}
        )
        worker_runtime = _Runtime(
            {
                "assignment-1:7": {
                    "id": "assignment-1:7",
                    "method": "worker/approval",
                }
            }
        )
        host = SimpleNamespace(codex=global_runtime)
        manager = SimpleNamespace(
            sessions={
                "assignment-1": SimpleNamespace(runtime=worker_runtime),
            }
        )
        service = ApprovalService(host, assignment_sessions=manager)

        await service.respond(
            "assignment-1:7",
            {"decision": "acceptForSession"},
        )

        self.assertEqual(global_runtime.responses, [])
        self.assertEqual(
            worker_runtime.responses,
            [
                (
                    "assignment-1:7",
                    {"decision": "acceptForSession"},
                )
            ],
        )

    async def test_duplicate_canonical_approval_id_fails_closed(self) -> None:
        global_runtime = _Runtime(
            {"collision": {"id": "collision", "method": "global"}}
        )
        worker_runtime = _Runtime(
            {"collision": {"id": "collision", "method": "worker"}}
        )
        host = SimpleNamespace(codex=global_runtime)
        manager = SimpleNamespace(
            sessions={"assignment-1": SimpleNamespace(runtime=worker_runtime)}
        )
        service = ApprovalService(host, assignment_sessions=manager)

        with self.assertRaisesRegex(
            RuntimeError,
            "duplicate canonical approval request id",
        ):
            service.pending()


if __name__ == "__main__":
    unittest.main()
