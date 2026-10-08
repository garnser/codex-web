from __future__ import annotations

import unittest
import threading
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

from codex_web import application
from codex_web.services.gitlab import GitLabService


class _Request:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def json(self) -> dict:
        return self.payload


class _Host:
    def __init__(self, *, enabled: bool = True, remember_event: bool = True) -> None:
        self.enabled = enabled
        self.remember_event = remember_event
        self.verified = False
        self.event_ids: list[str] = []

    def _verify_gitlab_webhook(self, request) -> None:
        self.verified = True

    def _load_gitlab_routing_settings(self):
        return SimpleNamespace(enabled=self.enabled, ignored_event_kinds=[])

    def _gitlab_event_id(self, request, payload) -> str:
        self.event_ids.append("evt-1")
        return "evt-1"

    def _remember_gitlab_event(self, event_id: str) -> bool:
        return self.remember_event


class GitLabServiceExtractionTests(unittest.IsolatedAsyncioTestCase):
    def test_composed_gitlab_webhook_is_owned_by_integrations_router(self) -> None:
        operations = application.app.openapi()["paths"]["/bots/gitlab/events"]
        tags = {
            tag
            for operation in operations.values()
            if isinstance(operation, dict)
            for tag in operation.get("tags", [])
        }
        self.assertIn("integrations", tags)
        self.assertGreater(application.EXTRACTED_ROUTE_COUNTS["integrations"], 0)

    def test_event_dedupe_state_is_isolated_per_service_instance(self) -> None:
        first = GitLabService(_Host())
        second = GitLabService(_Host())

        self.assertTrue(first.remember_event("evt-shared"))
        self.assertFalse(first.remember_event("evt-shared"))
        self.assertTrue(second.remember_event("evt-shared"))

    async def test_disabled_gitlab_routing_short_circuits_after_verification(self) -> None:
        host = _Host(enabled=False)
        result = await GitLabService(host).handle_event(_Request({"object_kind": "issue"}))

        self.assertTrue(host.verified)
        self.assertEqual(
            result,
            {"ok": True, "ignored": True, "reason": "gitlab_routing_disabled"},
        )
        self.assertEqual(host.event_ids, [])

    async def test_projection_runs_off_loop_and_finishes_before_routing(self) -> None:
        service = GitLabService(_Host())
        service.is_support_servicedesk_ticket_payload = lambda _payload: False
        service.project_settings_for_payload = lambda *_args: (
            "project-a", SimpleNamespace(enabled=True))
        service._ingest_canonical_event = AsyncMock(return_value=None)
        loop_thread = threading.get_ident()
        state = object()
        projected = []
        def project(payload, *, project_id):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            self.assertEqual(project_id, "project-a")
            projected.append(payload)
            return state
        def targets(payload, settings, projected_state):
            self.assertEqual(projected, [payload])
            self.assertIs(projected_state, state)
            return []
        service.work_items = SimpleNamespace(project_event=project)
        service.event_target_agents = targets
        service.routing_bindings_for_master = Mock(return_value=[])
        result = await service.handle_event(_Request({"object_kind": "issue"}))
        self.assertEqual(result["reason"], "no_matching_binding")

    async def test_duplicate_event_stops_before_project_routing(self) -> None:
        host = _Host(enabled=True, remember_event=False)
        result = await GitLabService(host).handle_event(_Request({"object_kind": "issue"}))

        self.assertTrue(host.verified)
        self.assertEqual(
            result,
            {"ok": True, "ignored": True, "reason": "duplicate", "eventId": "evt-1"},
        )
        self.assertEqual(host.event_ids, ["evt-1"])


if __name__ == "__main__":
    unittest.main()
