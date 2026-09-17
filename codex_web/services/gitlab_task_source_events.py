from __future__ import annotations

from codex_web.integrations.gitlab_client import GitLabClient
from codex_web.services.gitlab_task_source import GitLabTaskSource
from codex_web.services.task_sources import TaskSourceCapabilities, TaskSourceCapability


class GitLabWebhookTaskSource(GitLabTaskSource):
    """GitLab adapter instance limited to signed webhook normalization.

    Webhook normalization and canonical projection need no GitLab API token.
    API-backed discovery/read/write use the normal ``GitLabTaskSource`` and
    therefore continue to require credentials.
    """

    capabilities = TaskSourceCapabilities(frozenset({TaskSourceCapability.EVENTS}))

    def __init__(
        self,
        api_base: str,
        *,
        client: GitLabClient | None = None,
    ) -> None:
        api_base = str(api_base or "").strip().rstrip("/")
        if not api_base:
            raise ValueError("GitLab task source api_base must not be empty")
        self.source_instance = api_base
        self.api_base = api_base
        self.token = ""
        self.client = client or GitLabClient()
