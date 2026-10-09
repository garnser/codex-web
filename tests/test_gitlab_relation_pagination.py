from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import httpx

from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.gitlab_task_source import GitLabTaskSource
from tests import test_task_source_work_item_projection as projection


class GitLabRelationPaginationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = projection.TaskSourceWorkItemProjectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.test_verified_open_mr_relation_survives_subsequent_issue_refresh()
        self.ref = "group/project#42"
        self.before = self.fixture.host.states[self.ref].model_copy(deep=True)
        self.requests: list[httpx.Request] = []

    @staticmethod
    def mr(iid, *, state="merged", **extra):
        return {
            "iid": iid, "source_project_id": 501, "target_project_id": 501,
            "sha": "a" * 40, "updated_at": "2026-09-17T20:00:00Z",
            "state": state, **extra,
        }

    def source(self, pages):
        async def handler(request):
            self.requests.append(request)
            self.assertEqual(request.url.host, "gitlab.example")
            self.assertEqual(request.headers["PRIVATE-TOKEN"], "test-only")
            if request.url.path.endswith("related_merge_requests"):
                page = int(request.url.params.get("page", "1"))
                return pages(page)
            return httpx.Response(200, json={
                "project_id": 501, "iid": 42, "state": "opened",
                "updated_at": "2026-09-17T20:00:00Z",
                "references": {"full": self.ref},
                "labels": ["owner::carl", "status::in progress"],
            })
        return GitLabTaskSource(
            "https://gitlab.example/api/v4", "test-only",
            client=GitLabClient(transport=httpx.MockTransport(handler)),
        )

    async def refresh(self, pages):
        source = self.source(pages)
        snapshot = await source.read(self.before.source_identity)
        return self.fixture.projector.upsert(source, snapshot, project_id="home")

    async def assert_failed_refresh_preserves_lane(self, pages, error="GitLab"):
        self.requests.clear()
        events = self.fixture.host.WORK_ITEM_EVENTS_FILE.read_text()
        with self.assertRaisesRegex(RuntimeError, error):
            await self.refresh(pages)
        self.assertEqual(self.fixture.host.states[self.ref], self.before)
        self.assertEqual(self.fixture.host.WORK_ITEM_EVENTS_FILE.read_text(), events)

    async def test_known_open_mr_on_later_page_retains_actual_reviewer_projection(self):
        def pages(page):
            if page == 1:
                return httpx.Response(200, json=[self.mr(i) for i in range(1, 21)],
                                      headers={"X-Next-Page": "2", "X-Total": "21"})
            return httpx.Response(200, json=[self.mr(287, state="opened")],
                                  headers={"X-Next-Page": "", "X-Total": "21"})
        result = await self.refresh(pages)
        self.assertEqual(result.current_stage, "ready_for_validation")
        self.assertEqual(result.current_owner, "carl")
        self.assertEqual(result.implementation_owner, "carl")
        self.assertEqual(result.next_owner, "quinn")
        self.assertEqual(result.mr_refs, ["group/project!287"])
        self.assertEqual(result.artifact_state, "merge_request")
        self.assertEqual(len(result.verified_artifact_relations), 21)
        relation = next(r for r in result.verified_artifact_relations if r.ref == "group/project!287")
        self.assertEqual(relation.source_type, "gitlab")
        self.assertEqual(relation.source_instance, "https://gitlab.example/api/v4")
        self.assertEqual(relation.ref, "group/project!287")
        self.assertEqual(relation.head_revision, "a" * 40)
        self.assertEqual(relation.source_revision, "2026-09-17T20:00:00Z")
        self.assertEqual(relation.state, "opened")
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(r.url.params["per_page"] == "100" for r in self.requests[1:]))
        self.assertEqual([r.url.params["page"] for r in self.requests[1:]], ["1", "2"])

    async def test_missing_headers_require_empty_page_even_after_short_page(self):
        def pages(page):
            return httpx.Response(200, json=[self.mr(287, state="opened")] if page == 1 else [])
        result = await self.refresh(pages)
        self.assertEqual(result.mr_refs, ["group/project!287"])
        self.assertEqual(len(self.requests), 3)

    async def test_complete_absence_removes_relation_without_claiming_closure(self):
        result = await self.refresh(lambda page: httpx.Response(
            200, json=[], headers={"X-Next-Page": "", "X-Total": "0"},
        ))
        self.assertEqual(result.mr_refs, [])
        self.assertIsNone(result.next_owner)
        self.assertEqual(result.current_stage, "implementation_active")
        self.assertEqual(result.implementation_owner, "carl")
        self.assertIsNone(result.closed_at)

    async def test_later_http_json_and_page_errors_never_demote_known_relation(self):
        failures = [httpx.Response(503), httpx.Response(200, content=b"{"),
                    httpx.Response(200, json={}), httpx.Response(200, json=[None]),
                    httpx.Response(200, json=[{"iid": True}])]
        for failure in failures:
            with self.subTest(response=failure.status_code, body=failure.content):
                await self.assert_failed_refresh_preserves_lane(lambda page: (
                    httpx.Response(200, json=[self.mr(1)], headers={"X-Next-Page": "2"})
                    if page == 1 else failure
                ))

    async def test_invalid_continuations_are_not_followed_and_preserve_lane(self):
        for value in ["1", "3", "02x", " 2", "٢", "https://evil.example/?page=2"]:
            with self.subTest(next_page=value):
                await self.assert_failed_refresh_preserves_lane(lambda page: httpx.Response(
                    200, json=[self.mr(1)], headers={b"X-Next-Page": value.encode("utf-8")},
                ), "next page")
                self.assertEqual(len(self.requests), 2)

    async def test_redirect_is_not_followed_or_forwarded_and_preserves_lane(self):
        await self.assert_failed_refresh_preserves_lane(lambda page: httpx.Response(
            302, headers={"Location": "https://foreign.example/related_merge_requests"},
        ), "HTTP 302")
        # One issue read and one relation request, both to the configured origin.
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(r.url.host == "gitlab.example" for r in self.requests))

    async def test_repeated_item_and_empty_continuation_page_fail_closed(self):
        for payload in [[self.mr(1)], []]:
            with self.subTest(payload=payload):
                await self.assert_failed_refresh_preserves_lane(lambda page: httpx.Response(
                    200, json=[self.mr(1)] if page == 1 else payload,
                    headers={"X-Next-Page": str(page + 1)},
                ))

    async def test_page_item_and_total_caps_preserve_known_relation(self):
        await self.assert_failed_refresh_preserves_lane(lambda page: httpx.Response(
            200, json=[self.mr(page)], headers={"X-Next-Page": str(page + 1)},
        ), "page limit")
        self.assertEqual(len(self.requests), 11)  # issue plus ten bounded pages
        await self.assert_failed_refresh_preserves_lane(lambda page: httpx.Response(
            200, json=[self.mr(1)], headers={"X-Next-Page": "2", "X-Total": "1001"},
        ), "total exceeds bounds")
        await self.assert_failed_refresh_preserves_lane(lambda page: httpx.Response(
            200, json=[self.mr(i) for i in range(1, 102)], headers={"X-Next-Page": ""},
        ), "page is invalid")

    async def test_inconsistent_total_does_not_turn_omission_into_absence(self):
        for total in ["21", "invalid"]:
            with self.subTest(total=total):
                await self.assert_failed_refresh_preserves_lane(lambda page: httpx.Response(
                    200, json=[self.mr(1)], headers={"X-Next-Page": "", "X-Total": total},
                ))
        await self.assert_failed_refresh_preserves_lane(lambda page: httpx.Response(
            200, json=[self.mr(page)],
            headers={"X-Next-Page": "2" if page == 1 else "", "X-Total": str(page)},
        ), "total exceeds bounds or changed")

    async def test_page_and_aggregate_byte_caps_preserve_known_relation(self):
        await self.assert_failed_refresh_preserves_lane(lambda page: httpx.Response(
            200, json=[self.mr(1, description="x" * (512 * 1024))],
        ), "byte limit")
        await self.assert_failed_refresh_preserves_lane(lambda page: httpx.Response(
            200, json=[self.mr(page, description="x" * 500000)],
            headers={"X-Next-Page": str(page + 1)},
        ), "byte limit")
        self.assertLessEqual(len(self.requests), 10)

    async def test_overall_deadline_is_visible_without_partial_projection(self):
        original_timeout = asyncio.timeout
        source = self.source(lambda page: httpx.Response(200, json=[]))
        async def slow_handler(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json=[])
        source.client.transport = httpx.MockTransport(slow_handler)
        # Exercise the real enumeration deadline without waiting sixty seconds.
        with patch("codex_web.integrations.gitlab_client.asyncio.timeout",
                   side_effect=lambda seconds: original_timeout(0.01)):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await source.client.issue_related_merge_requests(
                    source.api_base, "group/project", 42, token="test-only",
                )
        self.assertEqual(self.fixture.host.states[self.ref], self.before)
