from __future__ import annotations

import unittest

from codex_web.models import TaskSourceIdentity
from codex_web.services.github_task_source import GitHubTaskSource
from codex_web.services.task_sources import TaskSourceCreateRequest


class Client:
    def __init__(self): self.calls = []
    async def list_issues(self, api, repo, *, token, state="open"):
        self.calls.append(("list", repo, token)); return [{"number": 4, "title": "Fix", "state": "open", "html_url": "https://github.com/acme/app/issues/4", "updated_at": "2026-01-01T00:00:00Z", "assignees": [{"login": "dana"}], "labels": [{"name": "bug"}]}]
    async def issue(self, api, repo, number, *, token): return {"number": number, "title": "Read", "state": "open", "repository": {"full_name": repo}}
    async def create_issue(self, api, repo, *, token, payload): return {"number": 9, **payload, "state": "open", "repository": {"full_name": repo}}
    async def update_issue(self, api, repo, number, *, token, payload): return {"number": number, **payload, "repository": {"full_name": repo}}
    async def create_comment(self, api, repo, number, *, token, body): self.calls.append(("comment", body)); return {}


class GitHubTaskSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_create_and_identity(self):
        client = Client(); source = GitHubTaskSource("https://api.github.com", "token", client=client)
        found = await source.discover(scope="acme/app")
        self.assertEqual(found[0].identity.external_id, "acme/app#4")
        created = await source.create(TaskSourceCreateRequest(title="New", labels=("bug",)), scope="acme/app")
        self.assertEqual(created.identity.external_id, "acme/app#9")
        await source.add_comment(found[0].identity, "hello")
        self.assertEqual(client.calls[-1], ("comment", "hello"))

    async def test_event_and_projection(self):
        source = GitHubTaskSource("https://api.github.com", "token", client=Client())
        event = await source.normalize_event({"action": "opened", "repository": {"full_name": "acme/app"}, "issue": {"number": 4, "title": "Fix", "state": "open"}})
        self.assertEqual(event.identity, TaskSourceIdentity(source_type="github", source_instance="https://api.github.com", external_id="acme/app#4"))
        self.assertEqual(source.project(event.snapshot).stage, "implementation_active")


if __name__ == "__main__": unittest.main()
