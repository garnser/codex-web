from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

import httpx

from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.gitlab import GitLabService


class GitLabClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_issues_uses_async_http_and_private_token(self) -> None:
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json=[{"id": 1, "references": {"full": "group/project#1"}}],
            )

        client = GitLabClient(transport=httpx.MockTransport(handler))
        issues = await client.group_issues(
            "https://gitlab.example/api/v4",
            "group/subgroup",
            token="secret",
            labels=["owner::dana"],
        )

        self.assertEqual(issues[0]["id"], 1)
        self.assertEqual(seen[0].headers["PRIVATE-TOKEN"], "secret")
        self.assertEqual(seen[0].url.params["state"], "opened")
        self.assertEqual(seen[0].url.params["labels"], "owner::dana")
        self.assertIn("/groups/group%2Fsubgroup/issues", str(seen[0].url))

    async def test_http_error_becomes_contextual_runtime_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"message": "unavailable"})

        client = GitLabClient(transport=httpx.MockTransport(handler))
        with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
            await client.project(
                "https://gitlab.example/api/v4",
                "group/project",
                token="secret",
            )


class _ServiceDeskGitLab:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []

    async def project(self, api_base, project, *, token):
        self.thread_ids.append(threading.get_ident())
        return {"id": 5, "path_with_namespace": "group/support"}

    async def project_issues(self, api_base, project, *, token, params=None):
        self.thread_ids.append(threading.get_ident())
        return [{"id": 100, "iid": 7, "title": "Ticket", "state": "opened", "labels": []}]


class _ServiceDeskHost:
    def __init__(self) -> None:
        self.state = {"tickets": {}, "last_sweep_at": None}
        self.dispatched: list[dict] = []

    def _gitlab_api_token(self):
        return "secret"

    def _support_servicedesk_sweep_project(self):
        return "group/support"

    def _gitlab_api_base_url(self):
        return "https://gitlab.example"

    def _support_servicedesk_sweep_lookback_hours(self):
        return 0

    def _issue_to_support_servicedesk_payload(self, issue, project_path, project_id):
        return {
            "project": {"id": project_id, "path_with_namespace": project_path},
            "object_attributes": {"iid": issue["iid"], "state": "opened", "action": "sweep"},
        }

    def _load_gitlab_routing_settings(self):
        return SimpleNamespace(enabled=True)

    async def _dispatch_support_servicedesk_ticket(self, payload, *, source, settings):
        self.dispatched.append(payload)
        return {"ok": True, "accepted": True}

    def _load_support_servicedesk_state(self):
        return dict(self.state)

    def _save_support_servicedesk_state(self, state):
        self.state = state


class GitLabServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_servicedesk_fetch_is_async_and_existing_dispatch_is_reused(self) -> None:
        host = _ServiceDeskHost()
        gitlab = _ServiceDeskGitLab()
        service = GitLabService(host, gitlab)
        event_loop_thread = threading.get_ident()

        result = await service.sweep_support_servicedesk()

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(gitlab.thread_ids, [event_loop_thread, event_loop_thread])
        self.assertEqual(len(host.dispatched), 1)
        self.assertIsNotNone(host.state["last_sweep_at"])


if __name__ == "__main__":
    unittest.main()
