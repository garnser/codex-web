from __future__ import annotations

import base64
import unittest

import httpx

from codex_web.code_hosts import CodeHostError
from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.gitlab_code_host import GitLabCodeHostProvider
from tests import test_code_host_adapters as adapters


class GitLabArtifactTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.binding = adapters.CodeHostAdapterTests._binding("gitlab")
        self.resource = adapters.CodeHostAdapterTests._resource("gitlab")
        self.requests = []
        self.jobs = [self.job(19025)]
        self.status = 200
        self.project_id = 42
        self.body = b"PK\x03\x04\x00\xffarchive"
        self.provider = GitLabCodeHostProvider(GitLabClient(transport=httpx.MockTransport(self.handle)))

    @staticmethod
    def job(job_id, **updates):
        return {
            "id": job_id, "pipeline": {"id": 3394, "project_id": 42},
            "artifacts_file": {"filename": "artifacts.zip", "size": 999999},
            "created_at": "2026-10-01T00:00:00Z",
            "artifacts_expire_at": "2099-10-01T00:00:00Z", **updates,
        }

    def handle(self, request):
        self.requests.append(request)
        self.assertEqual(request.headers["PRIVATE-TOKEN"], "provider-secret")
        path = request.url.path
        self.assertTrue(path.startswith("/api/v4/projects/acme/widgets"))
        if path.endswith("projects/acme/widgets"):
            return httpx.Response(200, json={"id": self.project_id, "name": "widgets"})
        if path.endswith("pipelines/3394/jobs"):
            page = int(request.url.params["page"])
            self.assertEqual(request.url.params["per_page"], "100")
            return httpx.Response(200, json=self.jobs[(page-1)*100:page*100])
        if path.endswith("jobs/19025"):
            return httpx.Response(self.status, json=self.jobs[0])
        if "/artifacts" in path:
            return httpx.Response(self.status, content=self.body, headers={"content-type": "application/zip"})
        return httpx.Response(404)

    async def discover(self, run=3394):
        return await self.provider.artifacts(self.binding, self.resource, run, credential="provider-secret")

    async def download(self, limit=327680, member=None, job=19025):
        if member is not None:
            return await self.provider.artifact_member(
                self.binding, self.resource, job, credential="provider-secret",
                max_bytes=limit, member_path=member,
            )
        return await self.provider.artifact_download(
            self.binding, self.resource, job, credential="provider-secret", max_bytes=limit,
        )

    async def test_native_pipeline_job_identity_and_actual_binary_content(self):
        facts = await self.discover()
        self.assertEqual(facts[0].external_id, "19025")
        self.assertEqual(facts[0].name, "artifacts.zip")
        self.assertEqual(facts[0].size_bytes, 999999)
        self.assertFalse(facts[0].expired)
        result = await self.download()
        self.assertEqual(base64.b64decode(result.content_base64), self.body)
        self.assertEqual(result.media_type, "application/zip")
        self.assertFalse(result.truncated)
        result = await self.download(5)
        self.assertEqual(base64.b64decode(result.content_base64), self.body[:5])
        self.assertEqual(result.byte_count, 5)
        self.assertTrue(result.truncated)

    async def test_native_member_reads_without_archive_download(self):
        self.body = b"<html>generated preview</html>"
        result = await self.download(12, "docs/html/index.html")
        self.assertEqual(base64.b64decode(result.content_base64), self.body[:12])
        self.assertTrue(result.truncated)
        self.assertTrue(self.requests[-1].url.path.endswith("/artifacts/docs/html/index.html"))
        self.assertFalse(any(r.url.path.endswith("/artifacts") for r in self.requests))

    async def test_pagination_covers_jobs_without_artifacts_and_fails_at_bound(self):
        self.jobs = [self.job(index, artifacts_file={}) for index in range(1, 101)] + [self.job(19025)]
        self.assertEqual(len(await self.discover()), 1)
        pages = [r.url.params["page"] for r in self.requests if r.url.path.endswith("/jobs")]
        self.assertEqual(pages, ["1", "2"])
        self.jobs = [self.job(index) for index in range(1, 1001)]
        self.requests.clear()
        with self.assertRaisesRegex(CodeHostError, "pagination limit"):
            await self.discover()
        self.assertEqual(sum(r.url.path.endswith("/jobs") for r in self.requests), 10)

    async def test_missing_expired_wrong_job_or_project_fail_closed(self):
        for updates in (
            {"artifacts_file": {}}, {"artifacts_expire_at": "2000-01-01T00:00:00Z"},
            {"erased_at": "2026-10-01T00:00:00Z"}, {"id": 19026},
            {"pipeline": {"id": 3394, "project_id": 43}},
        ):
            self.jobs = [self.job(19025, **updates)]
            self.requests.clear()
            with self.assertRaises(CodeHostError):
                await self.download()
            self.assertFalse(any("/artifacts" in r.url.path for r in self.requests))
        self.jobs = [self.job(19025, pipeline={"id": 3395, "project_id": 42})]
        with self.assertRaises(CodeHostError):
            await self.discover()
        self.jobs = [self.job(19025)]
        self.status = 404
        with self.assertRaisesRegex(CodeHostError, "HTTP 404"):
            await self.download()

    async def test_project_locator_cannot_replace_canonical_project_identity(self):
        self.project_id = 43
        self.jobs = [self.job(19025, pipeline={"id": 3394, "project_id": 43})]
        with self.assertRaisesRegex(CodeHostError, "repository Resource"):
            await self.discover()
        with self.assertRaisesRegex(CodeHostError, "repository Resource"):
            await self.download()

    async def test_invalid_ids_paths_and_byte_caps_never_reach_transport(self):
        for member in ("", "/index.html", "../index.html", "html/../index.html", "html//index.html",
                       "./index.html", "C:/index.html", "html\\index.html", "html/\x00index.html",
                       "html/\x7findex.html", "%2e%2e/index.html", "x" * 1025):
            with self.assertRaises(CodeHostError):
                await self.download(member=member)
        for job in (0, -1, True, "19025"):
            with self.assertRaises(CodeHostError):
                await self.download(job=job)
        for run in (0, -1, True, "3394"):
            with self.assertRaises(CodeHostError):
                await self.discover(run)
        for limit in (0, -1, 327681, True):
            with self.assertRaises(CodeHostError):
                await self.download(limit)
        self.assertEqual(self.requests, [])

    async def test_redirects_preserve_binary_without_forwarding_credential(self):
        observed = []
        def handle(request):
            observed.append(request)
            if request.url.host == "gitlab.example":
                return httpx.Response(302, headers={"location": "https://cdn.example/signed.zip"})
            self.assertNotIn("PRIVATE-TOKEN", request.headers)
            return httpx.Response(200, content=b"\x00\xff", headers={"content-type": "application/zip"})
        client = GitLabClient(transport=httpx.MockTransport(handle))
        raw, truncated, media = await client.request_bytes(
            "GET", self.binding.base_url, "projects/42/jobs/19025/artifacts",
            token="provider-secret", max_bytes=20,
        )
        self.assertEqual(raw, b"\x00\xff")
        self.assertFalse(truncated)
        self.assertEqual(media, "application/zip")
        self.assertEqual(observed[0].headers["PRIVATE-TOKEN"], "provider-secret")

    async def test_https_downgrade_redirect_stops_before_second_request(self):
        observed = []
        def handle(request):
            observed.append(request)
            return httpx.Response(302, headers={"location": "http://cdn.example/signed.zip"})
        client = GitLabClient(transport=httpx.MockTransport(handle))
        with self.assertRaisesRegex(RuntimeError, "insecure redirect"):
            await client.request_bytes(
                "GET", self.binding.base_url, "projects/42/jobs/19025/artifacts",
                token="provider-secret", max_bytes=20,
            )
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].url.scheme, "https")
